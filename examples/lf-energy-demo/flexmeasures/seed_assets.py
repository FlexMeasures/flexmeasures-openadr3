"""
Seed a realistic three-level asset hierarchy in the walkthrough FlexMeasures instance.

Builds a campus site with a single grid connection point, an EVSE charging hub and a
grid-friendly office beneath it, as real GenericAssets and Sensors with flex-context
and flex-model metadata that the FlexMeasures scheduler can act on.

Follows the pattern of trigger_fetch.py: it runs inside the FlexMeasures server
container, reusing its uv-managed venv (/app/.venv) and talking to the database
through the ORM, so no API token flow is needed:

    docker compose exec server uv run --active --no-project /walkthrough/flexmeasures/seed_assets.py

The script is idempotent: every asset and sensor is fetched by name before it is
created, so re-running it changes nothing but the printed summary.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import timedelta

from flexmeasures.app import create as create_flexmeasures_app
from flexmeasures.data import db
from flexmeasures.data.models.generic_assets import GenericAsset, GenericAssetType
from flexmeasures.data.models.time_series import Sensor
from flexmeasures.data.models.user import Account
from flexmeasures.data.schemas.scheduling import DBFlexContextSchema
from flexmeasures.data.schemas.scheduling.storage import DBStorageFlexModelSchema
from flexmeasures.data.scripts.data_gen import (
    add_default_asset_types,
    add_transmission_zone_asset,
)
from flexmeasures.data.services.utils import get_or_create_model
from hierarchy import (
    ACCOUNT_NAME,
    CAMPUS_BREACH_PRICE,
    CAMPUS_NAME,
    CAMPUS_POWER_CAPACITY,
    CONTRACT_RESOLUTION,
    EVSE_HUB_NAME,
    EVSE_HUB_POWER_CAPACITY,
    EVSE_SPECS,
    FLEXMEASURES_URL,
    OFFICE_BASELOAD_NAME,
    OFFICE_HEAT_PUMP_CHARGING_EFFICIENCY,
    OFFICE_HEAT_PUMP_COP,
    OFFICE_HEAT_PUMP_NAME,
    OFFICE_HEAT_PUMP_POWER_CAPACITY,
    OFFICE_HEAT_PUMP_SOC_MAX,
    OFFICE_HEAT_PUMP_SOC_MIN,
    OFFICE_HEAT_PUMP_STORAGE_EFFICIENCY,
    OFFICE_NAME,
    OFFICE_POWER_CAPACITY,
    OFFICE_PV_NAME,
    OFFICE_PV_PEAK_POWER,
    POLLING_SCHEDULE_NAME,
    POWER_RESOLUTION,
    PRICE_SENSOR_NAME,
    SITE_LATITUDE,
    SITE_LONGITUDE,
    SITE_TIMEZONE,
    SOC_RESOLUTION,
    VEN_CLIENT_NAME,
    EvseSpec,
)
from marshmallow import ValidationError
from sqlalchemy import select
from timely_beliefs.sensors.func_store.knowledge_horizons import x_days_ago_at_y_oclock

from flexmeasures_openadr3.utils.ven_clients import VenClientRepository

POWER_UNIT = "kW"
ENERGY_UNIT = "kWh"
TEMPERATURE_UNIT = "°C"

# Sensor names are reused across assets, so an asset page reads the same everywhere.
POWER_SENSOR_NAME = "power"
SOC_SENSOR_NAME = "state of charge"
GRID_CONNECTION_CAPACITY_SENSOR_NAME = "grid connection capacity"
INDOOR_TEMPERATURE_SENSOR_NAME = "indoor temperature"


@dataclass
class SeedReport:
    """What the run created versus what it found already in place."""

    created_assets: list[str] = field(default_factory=list)
    existing_assets: list[str] = field(default_factory=list)
    created_sensors: list[str] = field(default_factory=list)
    existing_sensors: list[str] = field(default_factory=list)
    openadr_wiring: str | None = None

    def record_asset(self, asset: GenericAsset, *, was_created: bool) -> None:
        """Note an asset as created or pre-existing."""
        target = self.created_assets if was_created else self.existing_assets
        target.append(f"{asset.name} (id {asset.id})")

    def record_sensor(self, sensor: Sensor, *, was_created: bool) -> None:
        """Note a sensor as created or pre-existing."""
        target = self.created_sensors if was_created else self.existing_sensors
        target.append(f"{sensor.generic_asset.name}/{sensor.name} (id {sensor.id})")


def find_account(account_name: str) -> Account:
    """
    Look up the account that owns the demo hierarchy.

    :param account_name:  Name of an existing account, e.g. the walkthrough's toy account.
    :returns:             The matching account.
    :raises ValueError:   When no account with that name exists.
    """
    account = db.session.execute(select(Account).filter_by(name=account_name)).scalar_one_or_none()
    if account is None:
        msg = (
            f"No account named '{account_name}' found. "
            "The walkthrough's docker-compose `server` service creates it with "
            f"`flexmeasures add toy-account --name '{account_name}'`; start the stack first, "
            "or pass --account-name to target another account."
        )
        raise ValueError(msg)
    return account


def get_or_create_day_ahead_price_sensor() -> Sensor:
    """
    Return the public day-ahead price sensor on the NL transmission zone.

    Uses the same specification as `flexmeasures add toy-account`, so this reuses that
    command's sensor instead of adding a second, near-identical price sensor.

    :returns:  The day-ahead price sensor.
    """
    nl_zone = add_transmission_zone_asset("NL", db=db)
    sensor = get_or_create_model(
        Sensor,
        name=PRICE_SENSOR_NAME,
        generic_asset=nl_zone,
        unit="EUR/kWh",
        timezone=SITE_TIMEZONE,
        event_resolution=timedelta(minutes=60),
        knowledge_horizon=(
            x_days_ago_at_y_oclock,
            {"x": 1, "y": 12, "z": "Europe/Paris"},
        ),
    )
    db.session.flush()
    return sensor


class DemoSiteBuilder:
    """
    Builds the demo hierarchy in one account, creating only what is missing.

    Holds the account, the default asset types and the run's report, so the individual
    build steps stay free of plumbing arguments.
    """

    def __init__(self, account: Account) -> None:
        self.account = account
        self.asset_types: dict[str, GenericAssetType] = add_default_asset_types(db=db)
        self.report = SeedReport()
        # Assets this run touched, so validation stays limited to the demo hierarchy.
        self.assets: list[GenericAsset] = []

    def asset(
        self,
        name: str,
        asset_type: str,
        description: str,
        parent: GenericAsset | None = None,
    ) -> GenericAsset:
        """
        Fetch or create one asset of the demo hierarchy.

        Assets are looked up by name within the account, which is what makes re-runs
        of this script leave the hierarchy untouched.

        :param name:         Asset name, unique within the account.
        :param asset_type:   One of the default asset type names, e.g. `building` or `one-way_evse`.
        :param description:  Short explanation shown on the asset page.
        :param parent:       Parent asset, or None for the top-level site.
        :returns:            The existing or freshly created asset.
        """
        # Matching on the parent too keeps a name collision elsewhere in the account from
        # silently adopting a foreign asset into the demo hierarchy.
        has_expected_parent = GenericAsset.parent_asset_id.is_(None) if parent is None else GenericAsset.parent_asset_id == parent.id
        query = select(GenericAsset).filter_by(name=name, account_id=self.account.id).filter(has_expected_parent)
        existing = db.session.execute(query).scalar_one_or_none()

        if existing is not None:
            self.report.record_asset(existing, was_created=False)
            self.assets.append(existing)
            return existing

        asset = GenericAsset(
            name=name,
            generic_asset_type=self.asset_types[asset_type],
            owner=self.account,
            description=description,
            latitude=SITE_LATITUDE,
            longitude=SITE_LONGITUDE,
            parent_asset_id=None if parent is None else parent.id,
        )
        db.session.add(asset)
        db.session.flush()
        self.report.record_asset(asset, was_created=True)
        self.assets.append(asset)
        return asset

    def sensor(
        self,
        asset: GenericAsset,
        name: str,
        unit: str,
        event_resolution: timedelta | str = POWER_RESOLUTION,
        attributes: dict | None = None,
    ) -> Sensor:
        """
        Fetch or create one sensor on an asset of the demo hierarchy.

        :param asset:             Asset the sensor belongs to.
        :param name:              Sensor name, unique within the asset.
        :param unit:              Sensor unit, e.g. `kW` or `kWh`.
        :param event_resolution:  Resolution of the recorded events; a zero timedelta means instantaneous.
        :param attributes:        Extra sensor attributes, e.g. the sign convention.
        :returns:                 The existing or freshly created sensor.
        """
        existing = db.session.execute(select(Sensor).filter_by(name=name, generic_asset_id=asset.id)).scalar_one_or_none()
        if existing is not None:
            self.report.record_sensor(existing, was_created=False)
            return existing

        sensor = Sensor(
            name=name,
            generic_asset=asset,
            unit=unit,
            timezone=SITE_TIMEZONE,
            event_resolution=event_resolution,
            attributes=attributes or {},
        )
        db.session.add(sensor)
        db.session.flush()
        self.report.record_sensor(sensor, was_created=True)
        return sensor

    def power_sensor(self, asset: GenericAsset, *, consumption_is_positive: bool = True) -> Sensor:
        """
        Fetch or create the standard quarter-hourly power sensor of an asset.

        :param asset:                    Asset the sensor belongs to.
        :param consumption_is_positive:  Sign convention; False for a producing asset such as PV.
        :returns:                        The asset's power sensor.
        """
        return self.sensor(
            asset,
            POWER_SENSOR_NAME,
            POWER_UNIT,
            attributes={"consumption_is_positive": consumption_is_positive},
        )


def build_campus(builder: DemoSiteBuilder, price_sensor: Sensor) -> tuple[GenericAsset, Sensor]:
    """
    Create the top-level site and its grid connection point.

    The campus carries the flex-context, because a flex-context is read upwards through
    the asset tree: whatever the campus declares also applies to the hub and the office,
    unless those override it.

    :param builder:       Builder holding the target account.
    :param price_sensor:  Day-ahead price sensor to schedule against.
    :returns:             The campus asset and its power sensor.
    """
    campus = builder.asset(
        CAMPUS_NAME,
        "building",
        "Demo campus with a single grid connection point, through which all demo power flows pass.",
    )
    campus_power = builder.power_sensor(campus)
    builder.sensor(
        campus,
        GRID_CONNECTION_CAPACITY_SENSOR_NAME,
        POWER_UNIT,
        event_resolution=CONTRACT_RESOLUTION,
    )

    campus.flex_context = {
        "consumption-price": {"sensor": price_sensor.id},
        "production-price": {"sensor": price_sensor.id},
        "site-power-capacity": CAMPUS_POWER_CAPACITY,
        "site-consumption-breach-price": CAMPUS_BREACH_PRICE,
        "site-production-breach-price": CAMPUS_BREACH_PRICE,
        # The site's own power sensor doubles as the output for the scheduled aggregate.
        "aggregate-consumption": {"sensor": campus_power.id},
    }
    db.session.flush()
    return campus, campus_power


def set_campus_dashboard(campus: GenericAsset, power_sensors: list[Sensor], capacity_sensors: list[Sensor]) -> None:
    """
    Give the site a dashboard covering prices, power flows and connection capacity.

    Done after the subtrees are built, since it references their aggregate power sensors.

    :param campus:            Top-level site asset.
    :param power_sensors:     Site, hub and office power sensors, in that order.
    :param capacity_sensors:  Any OpenADR capacity-limit sensors wired into the flex-context.
    """
    connection_capacity = next(sensor for sensor in campus.sensors if sensor.name == GRID_CONNECTION_CAPACITY_SENSOR_NAME)
    campus.sensors_to_show = [
        {
            "title": "Day-ahead prices",
            "plots": [{"asset": campus.id, "flex-context": "consumption-price"}],
        },
        {
            "title": "Site power flows",
            "plots": [{"sensors": [sensor.id for sensor in power_sensors]}],
        },
        {
            "title": "Connection capacity",
            "plots": [{"sensors": [connection_capacity.id, *(sensor.id for sensor in capacity_sensors)]}],
        },
    ]
    db.session.flush()


def build_evse_hub(builder: DemoSiteBuilder, campus: GenericAsset) -> tuple[GenericAsset, Sensor]:
    """
    Create the EVSE charging hub and its charge points.

    The hub is a device group rather than a device: its own flex-model entry declares the
    capacity of the sub-connection that all charge points behind it have to share, and each
    charge point points back at it through the flex-model `group` field.

    :param builder:  Builder holding the target account.
    :param campus:   Parent site asset.
    :returns:        The hub asset and its aggregate power sensor.
    """
    hub = builder.asset(
        EVSE_HUB_NAME,
        "building",
        f"EVSE charging hub behind a {EVSE_HUB_POWER_CAPACITY} sub-connection, shared by all charge points below it.",
        parent=campus,
    )
    hub_power = builder.power_sensor(hub)
    hub.flex_model = {
        "power-capacity": EVSE_HUB_POWER_CAPACITY,
        "consumption": {"sensor": hub_power.id},
    }
    db.session.flush()

    charge_point_power_sensors = [build_charge_point(builder, hub, spec) for spec in EVSE_SPECS]
    hub.sensors_to_show = [
        {
            "title": "Charging power",
            "plots": [{"sensors": [hub_power.id, *(sensor.id for sensor in charge_point_power_sensors)]}],
        },
    ]
    db.session.flush()
    return hub, hub_power


def build_charge_point(builder: DemoSiteBuilder, hub: GenericAsset, spec: EvseSpec) -> Sensor:
    """
    Create one charge point below the hub.

    :param builder:  Builder holding the target account.
    :param hub:      Parent hub asset, which also acts as the charge point's device group.
    :param spec:     Rating and battery limits of this charge point.
    :returns:        The charge point's power sensor.
    """
    is_bidirectional = spec.asset_type == "two-way_evse"
    charge_point = builder.asset(
        spec.name,
        spec.asset_type,
        f"{spec.power_capacity} {'bidirectional (V2G)' if is_bidirectional else 'charge-only'} charge point.",
        parent=hub,
    )
    power = builder.power_sensor(charge_point)
    state_of_charge = builder.sensor(charge_point, SOC_SENSOR_NAME, ENERGY_UNIT, event_resolution=SOC_RESOLUTION)

    charge_point.flex_model = {
        "group": {"asset": hub.id},
        "power-capacity": spec.power_capacity,
        "soc-min": spec.soc_min,
        "soc-max": spec.soc_max,
        "roundtrip-efficiency": spec.roundtrip_efficiency,
        "state-of-charge": {"sensor": state_of_charge.id},
        "consumption": {"sensor": power.id},
    }
    if spec.production_capacity is not None:
        charge_point.flex_model["production-capacity"] = spec.production_capacity
    db.session.flush()
    return power


def build_office(builder: DemoSiteBuilder, campus: GenericAsset) -> tuple[GenericAsset, Sensor]:
    """
    Create the grid-friendly office with rooftop PV, a heat pump and a background load.

    PV and background load are modelled as their own child assets carrying an inflexible
    flex-model entry, rather than as flex-context fields on the office. That way the
    scheduler still sees them when the campus, rather than the office, is the asset being
    scheduled, because a flex-model is gathered downwards through the whole asset tree.

    Like the heat pump, both also reference the office as their `group`, which is what
    makes the office's `power-capacity` a real cap on its whole sub-connection: a group's
    capacity only binds the entries that name it, so leaving the two inflexible devices
    out would silently give the heat pump the entire 250 kVA to itself.

    :param builder:  Builder holding the target account.
    :param campus:   Parent site asset.
    :returns:        The office asset and its aggregate power sensor.
    """
    office = builder.asset(
        OFFICE_NAME,
        "building",
        f"Grid-friendly office behind a {OFFICE_POWER_CAPACITY} sub-connection, with rooftop PV, a heat pump and a background load.",
        parent=campus,
    )
    office_power = builder.power_sensor(office)
    indoor_temperature = builder.sensor(office, INDOOR_TEMPERATURE_SENSOR_NAME, TEMPERATURE_UNIT)
    office.flex_model = {
        "power-capacity": OFFICE_POWER_CAPACITY,
        "consumption": {"sensor": office_power.id},
    }
    db.session.flush()

    pv_power = build_office_pv(builder, office)
    baseload_power = build_office_baseload(builder, office)
    heat_pump_power = build_office_heat_pump(builder, office)

    office.sensors_to_show = [
        {
            "title": "Office power flows",
            "plots": [{"sensors": [office_power.id, pv_power.id, baseload_power.id, heat_pump_power.id]}],
        },
        {"title": "Indoor climate", "plots": [{"sensor": indoor_temperature.id}]},
    ]
    db.session.flush()
    return office, office_power


def build_office_pv(builder: DemoSiteBuilder, office: GenericAsset) -> Sensor:
    """
    Create the office's rooftop PV as must-take production.

    :param builder:  Builder holding the target account.
    :param office:   Parent office asset.
    :returns:        The PV power sensor.
    """
    pv = builder.asset(
        OFFICE_PV_NAME,
        "solar",
        f"{OFFICE_PV_PEAK_POWER} of rooftop PV, modelled as must-take production rather than a curtailable device.",
        parent=office,
    )
    pv_power = builder.power_sensor(pv, consumption_is_positive=False)
    pv.flex_model = {
        # Behind the office's connection like everything else, so its infeed counts
        # against that connection's capacity even though it is not schedulable.
        "group": {"asset": office.id},
        "inflexible-production": {"sensor": pv_power.id},
    }
    db.session.flush()
    return pv_power


def build_office_baseload(builder: DemoSiteBuilder, office: GenericAsset) -> Sensor:
    """
    Create the office's background load as inflexible consumption.

    :param builder:  Builder holding the target account.
    :param office:   Parent office asset.
    :returns:        The background load's power sensor.
    """
    baseload = builder.asset(
        OFFICE_BASELOAD_NAME,
        "process",
        "Background office load (lighting, servers, plug loads) that cannot be shifted.",
        parent=office,
    )
    baseload_power = builder.power_sensor(baseload)
    baseload.flex_model = {
        # Behind the office's connection like everything else, so it eats into the
        # headroom the heat pump has left under that connection's capacity.
        "group": {"asset": office.id},
        "inflexible-consumption": {"sensor": baseload_power.id},
    }
    db.session.flush()
    return baseload_power


def build_office_heat_pump(builder: DemoSiteBuilder, office: GenericAsset) -> Sensor:
    """
    Create the office's heat pump as a thermal buffer.

    FlexMeasures ships no `heat_pump` asset type, so the heat pump together with the
    building's thermal mass is modelled as a `heat-storage` device, mirroring the
    `Heat Pump Template` asset FlexMeasures seeds itself: the scheduler charges and
    discharges the buffer, and the storage efficiency stands in for heat losses.
    The coefficient of performance goes in `charging-efficiency`, the one-way conversion
    from electricity to stored heat, which may exceed 100% for exactly this purpose.
    Power is therefore electrical while state of charge is thermal.

    :param builder:  Builder holding the target account.
    :param office:   Parent office asset, which also acts as the heat pump's device group.
    :returns:        The heat pump's power sensor.
    """
    description = (
        f"Heat pump ({OFFICE_HEAT_PUMP_POWER_CAPACITY} electrical, COP {OFFICE_HEAT_PUMP_COP}) with the building's thermal mass as its buffer. "
        "State of charge is thermal energy."
    )
    heat_pump = builder.asset(OFFICE_HEAT_PUMP_NAME, "heat-storage", description, parent=office)
    heat_pump_power = builder.power_sensor(heat_pump)
    state_of_charge = builder.sensor(heat_pump, SOC_SENSOR_NAME, ENERGY_UNIT, event_resolution=SOC_RESOLUTION)

    heat_pump.flex_model = {
        "group": {"asset": office.id},
        "power-capacity": OFFICE_HEAT_PUMP_POWER_CAPACITY,
        # A heat pump cannot feed power back into the office.
        "production-capacity": "0 kW",
        "soc-min": OFFICE_HEAT_PUMP_SOC_MIN,
        "soc-max": OFFICE_HEAT_PUMP_SOC_MAX,
        # Mutually exclusive with `roundtrip-efficiency`, which the charge points use instead.
        "charging-efficiency": OFFICE_HEAT_PUMP_CHARGING_EFFICIENCY,
        "storage-efficiency": OFFICE_HEAT_PUMP_STORAGE_EFFICIENCY,
        "state-of-charge": {"sensor": state_of_charge.id},
        "consumption": {"sensor": heat_pump_power.id},
    }
    db.session.flush()
    return heat_pump_power


def find_openadr_capacity_sensors(report: SeedReport) -> dict[str, Sensor]:
    """
    Look up the OpenADR capacity-limit sensors, keyed by the flex-context field they feed.

    The sensors only come into existence once a VEN client and polling schedule have been
    configured (walkthrough steps 4 and 5), so finding none is a normal state to report
    rather than an error.

    :param report:  Run report to record why no sensors were found, if that is the case.
    :returns:       Flex-context field name mapped to the sensor that should drive it.
    """
    ven_client = VenClientRepository().find_by_name(VEN_CLIENT_NAME)
    if ven_client is None:
        report.openadr_wiring = f"not wired: no VEN client '{VEN_CLIENT_NAME}' configured yet"
        return {}

    sensor_config = ven_client.get_sensor_config(POLLING_SCHEDULE_NAME)
    if sensor_config is None:
        report.openadr_wiring = f"not wired: VEN client '{VEN_CLIENT_NAME}' has no polling schedule '{POLLING_SCHEDULE_NAME}' yet"
        return {}

    found = {
        context_field: sensor
        for context_field, sensor in (
            ("site-consumption-capacity", sensor_config.import_sensor),
            ("site-production-capacity", sensor_config.export_sensor),
        )
        if sensor is not None
    }
    if not found:
        report.openadr_wiring = f"not wired: polling schedule '{POLLING_SCHEDULE_NAME}' fetches neither import nor export capacity limits"
    return found


def wire_openadr_capacity_limits(campus: GenericAsset, report: SeedReport) -> list[Sensor]:
    """
    Point the site's capacity limits at the OpenADR sensors, or clear them when absent.

    This is what ties the hierarchy to the rest of the walkthrough: the import and export
    capacity limits fetched from the VTN become the site capacities the scheduler has to
    respect. Both fields are rewritten from scratch on every run, so a hierarchy seeded
    while a polling schedule existed does not keep pointing at sensors that were since
    deleted along with it.

    :param campus:  Top-level site asset whose flex-context is updated.
    :param report:  Run report to record the outcome in.
    :returns:       The capacity-limit sensors that were wired in, empty when there are none.
    """
    sensors_by_field = find_openadr_capacity_sensors(report)

    for context_field in ("site-consumption-capacity", "site-production-capacity"):
        sensor = sensors_by_field.get(context_field)
        if sensor is None:
            campus.flex_context.pop(context_field, None)
        else:
            campus.flex_context[context_field] = {"sensor": sensor.id}

    if sensors_by_field:
        report.openadr_wiring = ", ".join(f"{context_field} -> {sensor.name}" for context_field, sensor in sensors_by_field.items())
    db.session.flush()
    return list(sensors_by_field.values())


def validate_flex_metadata(assets: list[GenericAsset]) -> None:
    """
    Check the seeded flex-context and flex-model against their storage schemas.

    FlexMeasures validates these JSON columns when they are written through the API, but
    not on a direct ORM write, and an invalid entry would only surface later as a failed
    schedule. Validating here keeps the demo data guaranteed schedulable.

    :param assets:       Assets this run built or found.
    :raises ValueError:  When a seeded flex-context or flex-model is invalid.
    """
    for asset in assets:
        for label, stored, schema in (
            ("flex-context", asset.flex_context, DBFlexContextSchema()),
            ("flex-model", asset.flex_model, DBStorageFlexModelSchema()),
        ):
            if not stored:
                continue
            try:
                schema.load(stored)
            except ValidationError as exc:
                msg = f"Seeded {label} of asset '{asset.name}' is invalid: {exc.messages}. Fix the specification in hierarchy.py."
                raise ValueError(msg) from exc


def print_summary(report: SeedReport, campus: GenericAsset) -> None:
    """
    Print what the run created, found and wired up.

    :param report:  Run report gathered by the builder.
    :param campus:  Top-level site asset, to link to in the closing hint.
    """
    print(f"Created {len(report.created_assets)} asset(s) and {len(report.created_sensors)} sensor(s).")
    print(f"Found {len(report.existing_assets)} asset(s) and {len(report.existing_sensors)} sensor(s) already in place.")
    for name in report.created_assets:
        print(f"  + asset  {name}")
    for name in report.created_sensors:
        print(f"  + sensor {name}")
    for name in report.existing_assets:
        print(f"  = asset  {name}")
    print(f"OpenADR site capacity limits: {report.openadr_wiring}")
    print(f"Next: open {FLEXMEASURES_URL}/assets/{campus.id} to inspect the hierarchy.")


def main() -> None:
    """Seed the demo asset hierarchy and print a summary of the result."""
    parser = argparse.ArgumentParser(description="Seed the LF Energy demo asset hierarchy in FlexMeasures.")
    parser.add_argument(
        "--account-name",
        default=ACCOUNT_NAME,
        help=f"Account to own the demo assets (default: {ACCOUNT_NAME})",
    )
    args = parser.parse_args()

    app = create_flexmeasures_app()

    with app.app_context():
        account = find_account(args.account_name)
        price_sensor = get_or_create_day_ahead_price_sensor()

        builder = DemoSiteBuilder(account)
        campus, campus_power = build_campus(builder, price_sensor)
        _, hub_power = build_evse_hub(builder, campus)
        _, office_power = build_office(builder, campus)
        capacity_sensors = wire_openadr_capacity_limits(campus, builder.report)
        set_campus_dashboard(campus, [campus_power, hub_power, office_power], capacity_sensors)

        validate_flex_metadata(builder.assets)
        db.session.commit()
        print_summary(builder.report, campus)


if __name__ == "__main__":
    try:
        main()
    except ValueError as exc:
        print(f"Failed to seed the demo asset hierarchy: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
