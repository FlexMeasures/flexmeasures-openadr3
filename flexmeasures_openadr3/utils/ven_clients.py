from __future__ import annotations

import functools
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from flask_login import current_user
from flexmeasures.data import db
from flexmeasures.data.automations import validate_output_scope
from flexmeasures.data.models.audit_log import AssetAuditLog
from flexmeasures.data.models.automations import Automation
from flexmeasures.data.models.generic_assets import GenericAsset
from flexmeasures.data.models.time_series import Sensor
from flexmeasures.data.services.automations import create_automation, delete_automation, update_automation
from flexmeasures.data.services.data_generators import check_sensor_access
from flexmeasures.data.services.data_sources import get_or_create_source
from flexmeasures.utils.secrets_utils import get_secret, get_secret_paths, store_asset_secret
from openadr3_client.oadr310._ven.client import VirtualEndNodeClient
from openadr3_client.ven.http_factory import VirtualEndNodeHttpClientFactory
from openadr3_client.version import OADRVersion
from sqlalchemy import select

from flexmeasures_openadr3.automations import (
    FETCH_EVENTS_AUTOMATION,
    FETCH_EVENTS_AUTOMATION_TYPE,
    OpenADREventFetcher,
)
from flexmeasures_openadr3.models.storage import (
    VEN_CLIENT_ATTRIBUTE_KEY,
    VenClientAttributePayload,
)
from flexmeasures_openadr3.utils.sensor import VenAssetRepository, VenSensorPair, VenSensorRepository

if TYPE_CHECKING:
    from flexmeasures.data.models.data_sources import DataSource

    from flexmeasures_openadr3.utils.ven_client_forms import VenClientFormData, VenSensorConfigFormData

OAUTH_CLIENT_ID_SECRET_PATH = "ven_client.oauth_client_id"  # noqa: S105
OAUTH_CLIENT_SECRET_SECRET_PATH = "ven_client.oauth_client_secret"  # noqa: S105
# openadr3_client exposes no timeout/session injection point on its VEN client
# factory, so requests through it would otherwise block forever on an
# unresponsive VTN. This does not cover the OAuth token fetch, which uses its
# own requests_oauthlib session; that path relies on the ingestion queue's job timeout instead.
VTN_HTTP_TIMEOUT_SECONDS = 30
# How changes made through the plugin UI are attributed in the asset's audit log.
AUTOMATION_ORIGIN = "OpenADR 3 plugin"


class VenClientCredentialsMissingError(RuntimeError):
    """Raised when a VEN client has no OAuth credentials configured."""


# ---------------------------------------------------------------------------
# Domain models
# ---------------------------------------------------------------------------


@dataclass
class VenSensorConfig:
    """
    A polling schedule that defines when and what to fetch from a VTN.

    It is backed by a FlexMeasures automation on the VEN client's asset, which maps
    to one or two FlexMeasures sensors (import/export).
    """

    automation: Automation = field(repr=False)
    targets: list[str] = field(default_factory=list)
    import_sensor: Sensor | None = field(default=None, repr=False)
    export_sensor: Sensor | None = field(default=None, repr=False)

    @property
    def name(self) -> str:
        """Return the polling schedule's name."""
        return str(self.automation.name)

    @property
    def cronstr(self) -> str:
        """Return the cron string of when the polling schedule runs."""
        return str(self.automation.cronstr)

    @property
    def timezone(self) -> str:
        """Return the timezone the cron string is evaluated in."""
        return str(self.automation.timezone)

    @property
    def active(self) -> bool:
        """Return whether FlexMeasures runs this polling schedule when it is due."""
        return bool(self.automation.active)

    @property
    def fetch_import_capacity_limits(self) -> bool:
        """Return whether import capacity limits are fetched."""
        return self.import_sensor is not None

    @property
    def fetch_export_capacity_limits(self) -> bool:
        """Return whether export capacity limits are fetched."""
        return self.export_sensor is not None

    @classmethod
    def from_automation(cls, automation: Automation) -> VenSensorConfig:
        """Build a polling schedule from its automation and the configuration stored on the automation's data source."""
        config = automation.generator.attributes.get("data_generator", {}).get("config", {})
        import_sensor_id = config.get("import-sensor")
        export_sensor_id = config.get("export-sensor")
        return cls(
            automation=automation,
            targets=[str(target) for target in config.get("targets", [])],
            import_sensor=db.session.get(Sensor, import_sensor_id) if import_sensor_id is not None else None,
            export_sensor=db.session.get(Sensor, export_sensor_id) if export_sensor_id is not None else None,
        )


@dataclass
class VenClient:
    """
    Domain model representing an OpenADR VEN client.

    Wraps a FlexMeasures GenericAsset and provides typed access to VEN-specific
    attributes stored in the asset's JSON attributes column.
    """

    asset: GenericAsset = field(repr=False)
    vtn_url: str = ""
    oauth_token_url: str = ""
    scopes: list[str] = field(default_factory=list)
    sensor_configs: list[VenSensorConfig] = field(default_factory=list)
    oauth_client_id_is_set: bool = field(default=False, repr=False)
    oauth_client_secret_is_set: bool = field(default=False, repr=False)

    @property
    def id(self) -> int:
        """Return the underlying FlexMeasures asset id."""
        return int(self.asset.id)

    @property
    def name(self) -> str:
        """Return the VEN client display name."""
        return str(self.asset.name)

    @name.setter
    def name(self, value: str) -> None:
        """Update the VEN client display name."""
        self.asset.name = value

    def get_sensor_config(self, config_name: str) -> VenSensorConfig | None:
        """Return a polling schedule by name, if it exists."""
        return next((cfg for cfg in self.sensor_configs if cfg.name == config_name), None)

    def create_http_client(self) -> VirtualEndNodeClient:
        """Build an authenticated OpenADR HTTP client for this VEN."""
        try:
            oauth_client_id = get_secret(self.asset.secrets, OAUTH_CLIENT_ID_SECRET_PATH)
            oauth_client_secret = get_secret(self.asset.secrets, OAUTH_CLIENT_SECRET_SECRET_PATH)
        except KeyError as exc:
            msg = f"VEN client '{self.name}' has no OAuth credentials configured."
            raise VenClientCredentialsMissingError(msg) from exc

        client = VirtualEndNodeHttpClientFactory.create_http_ven_client(
            vtn_base_url=self.vtn_url,
            client_id=oauth_client_id,
            client_secret=oauth_client_secret,
            token_url=self.oauth_token_url,
            scopes=self.scopes,
            allow_insecure_http=True,
            version=OADRVersion.OADR_310,
        )
        client.events.session.request = functools.partial(  # type: ignore[attr-defined]
            client.events.session.request,  # type: ignore[attr-defined]
            timeout=VTN_HTTP_TIMEOUT_SECONDS,
        )
        return client  # type: ignore[return-value]

    def to_attribute_payload(self) -> VenClientAttributePayload:
        """Convert this client to the JSON payload stored on the asset."""
        return VenClientAttributePayload(
            vtn_url=self.vtn_url,
            oauth_token_url=self.oauth_token_url,
            scopes=tuple(self.scopes),
        )


def _fetch_events_config(targets: list[str], sensors: VenSensorPair) -> dict[str, object]:
    """Serialize what a polling schedule fetches into the configuration of its automation."""
    config: dict[str, object] = {"targets": list(targets)}
    if sensors.import_sensor is not None:
        config["import-sensor"] = sensors.import_sensor.id
    if sensors.export_sensor is not None:
        config["export-sensor"] = sensors.export_sensor.id
    return config


def _fetch_events_source(asset: GenericAsset, config: dict[str, object]) -> DataSource:
    """
    Get or create the data source holding a fetch configuration, as FlexMeasures does when creating the automation.

    FlexMeasures cannot change an automation's configuration in place, so changing what a
    polling schedule fetches hands its automation the data source of the new configuration.
    """
    generator = OpenADREventFetcher(config=config)
    validate_output_scope(asset.id, generator.output_sensors)
    if current_user and not current_user.is_anonymous:
        check_sensor_access(input_sensors=[], output_sensors=generator.output_sensors)
    source_info = generator.get_data_source_info()
    source_info["version"] = FETCH_EVENTS_AUTOMATION.installed_generator_version()
    source_info["attributes"] = {"data_generator": {"config": generator._config_schema.dump(generator._config)}}  # noqa: SLF001
    return get_or_create_source(**source_info, account=asset.owner)


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class VenClientRepository:
    """Repository for persisting and retrieving VenClient domain objects."""

    def __init__(self) -> None:
        self._assets = VenAssetRepository()
        self._sensors = VenSensorRepository()

    def _build_sensor_configs(self, asset: GenericAsset) -> list[VenSensorConfig]:
        automations = db.session.scalars(select(Automation).filter_by(asset_id=asset.id, type=FETCH_EVENTS_AUTOMATION_TYPE).order_by(Automation.id)).all()
        return [VenSensorConfig.from_automation(automation) for automation in automations]

    def _build_ven_client(self, asset: GenericAsset) -> VenClient:
        payload = VenClientAttributePayload.from_asset_attributes(asset.attributes)
        secret_paths = set(get_secret_paths(asset.secrets or {}))

        return VenClient(
            asset=asset,
            vtn_url=payload.vtn_url,
            oauth_token_url=payload.oauth_token_url,
            scopes=list(payload.scopes),
            sensor_configs=self._build_sensor_configs(asset),
            oauth_client_id_is_set=OAUTH_CLIENT_ID_SECRET_PATH in secret_paths,
            oauth_client_secret_is_set=OAUTH_CLIENT_SECRET_SECRET_PATH in secret_paths,
        )

    def _persist_payload(self, ven_client: VenClient) -> None:
        """Write VEN-specific, non-secret data back to the asset's attributes column."""
        attributes = dict(ven_client.asset.attributes or {})
        attributes[VEN_CLIENT_ATTRIBUTE_KEY] = ven_client.to_attribute_payload().to_json()
        ven_client.asset.attributes = attributes

    def list_ven_clients(self) -> list[VenClient]:
        """Return all persisted VEN clients."""
        return [self._build_ven_client(asset) for asset in self._assets.list_all()]

    def find_by_id(self, ven_id: int) -> VenClient | None:
        """Find a VEN client by asset id."""
        asset = self._assets.find_by_id(ven_id)
        return self._build_ven_client(asset) if asset else None

    def find_by_name(self, ven_name: str) -> VenClient | None:
        """Find a VEN client by asset name."""
        asset = self._assets.find_by_name(ven_name)
        return self._build_ven_client(asset) if asset else None

    def create(self, form_data: VenClientFormData) -> VenClient:
        """Create a new VEN client from validated form data."""
        asset = self._assets.create_asset(form_data.name)
        ven_client = VenClient(
            asset=asset,
            vtn_url=form_data.vtn_url,
            oauth_token_url=form_data.oauth_token_url,
            scopes=form_data.scopes,
        )
        store_asset_secret(asset, OAUTH_CLIENT_ID_SECRET_PATH, form_data.oauth_client_id)
        store_asset_secret(asset, OAUTH_CLIENT_SECRET_SECRET_PATH, form_data.oauth_client_secret)
        self._persist_payload(ven_client)
        db.session.flush()
        return self._build_ven_client(asset)

    def update(self, ven_client: VenClient, form_data: VenClientFormData) -> VenClient:
        """Update an existing VEN client from validated form data. A blank credential keeps the existing one."""
        ven_client.name = form_data.name
        ven_client.vtn_url = form_data.vtn_url
        ven_client.oauth_token_url = form_data.oauth_token_url
        ven_client.scopes = form_data.scopes
        if form_data.oauth_client_id:
            store_asset_secret(ven_client.asset, OAUTH_CLIENT_ID_SECRET_PATH, form_data.oauth_client_id)
        if form_data.oauth_client_secret:
            store_asset_secret(ven_client.asset, OAUTH_CLIENT_SECRET_SECRET_PATH, form_data.oauth_client_secret)
        self._persist_payload(ven_client)
        db.session.flush()
        return ven_client

    def delete(self, ven_client: VenClient) -> None:
        """Delete a VEN client and its underlying asset, which takes its polling schedules' automations along."""
        db.session.delete(ven_client.asset)

    def append_sensor_config(self, ven_client: VenClient, form_data: VenSensorConfigFormData) -> VenSensorConfig:
        """Create a polling schedule as an automation, ensuring its sensors exist."""
        if ven_client.get_sensor_config(form_data.name) is not None:
            msg = f"Sensor config with name '{form_data.name}' already exists."
            raise ValueError(msg)

        sensors = self._sensors.ensure_sensors_for_config(
            ven_client.asset,
            form_data.name,
            fetch_import_capacity_limits=form_data.fetch_import_capacity_limits,
            fetch_export_capacity_limits=form_data.fetch_export_capacity_limits,
        )
        automation, _warnings = create_automation(
            asset=ven_client.asset,
            name=form_data.name,
            cronstr=form_data.cronstr,
            timezone=form_data.timezone,
            automation_type=FETCH_EVENTS_AUTOMATION_TYPE,
            config=_fetch_events_config(form_data.targets, sensors),
            origin=AUTOMATION_ORIGIN,
            check_permissions=True,
        )
        db.session.flush()
        sensor_config = VenSensorConfig.from_automation(automation)
        ven_client.sensor_configs.append(sensor_config)
        return sensor_config

    def update_sensor_config(
        self,
        ven_client: VenClient,
        config_name: str,
        form_data: VenSensorConfigFormData,
    ) -> VenSensorConfig:
        """Update a polling schedule's automation, ensuring the sensors it now fetches for exist."""
        sensor_config = ven_client.get_sensor_config(config_name)
        if sensor_config is None:
            msg = f"Sensor config with name '{config_name}' not found."
            raise ValueError(msg)

        # Keep recording on the sensors the schedule already has, also when it is renamed.
        sensors = self._sensors.ensure_sensors_for_config(
            ven_client.asset,
            form_data.name,
            fetch_import_capacity_limits=form_data.fetch_import_capacity_limits,
            fetch_export_capacity_limits=form_data.fetch_export_capacity_limits,
            existing=VenSensorPair(import_sensor=sensor_config.import_sensor, export_sensor=sensor_config.export_sensor),
        )
        automation = sensor_config.automation
        update_automation(
            automation,
            name=form_data.name,
            cronstr=form_data.cronstr,
            timezone=form_data.timezone,
            origin=AUTOMATION_ORIGIN,
        )
        config = _fetch_events_config(form_data.targets, sensors)
        if config != automation.generator.attributes.get("data_generator", {}).get("config"):
            automation.generator = _fetch_events_source(ven_client.asset, config)
            AssetAuditLog.add_record(
                ven_client.asset,
                f"Updated automation '{automation.name}' ({automation.id}): fetch configuration → {config}. Via {AUTOMATION_ORIGIN}.",
            )
        db.session.flush()

        updated = VenSensorConfig.from_automation(automation)
        ven_client.sensor_configs = [updated if cfg.automation is automation else cfg for cfg in ven_client.sensor_configs]
        return updated

    def delete_sensor_config(self, ven_client: VenClient, config_name: str) -> None:
        """Remove a polling schedule's automation and delete its associated sensors."""
        sensor_config = ven_client.get_sensor_config(config_name)
        if sensor_config is None:
            msg = f"Sensor config with name '{config_name}' not found."
            raise ValueError(msg)

        delete_automation(sensor_config.automation, origin=AUTOMATION_ORIGIN)
        self._sensors.delete_sensors_for_config(
            ven_client.asset,
            config_name,
            VenSensorPair(import_sensor=sensor_config.import_sensor, export_sensor=sensor_config.export_sensor),
        )
        ven_client.sensor_configs = [cfg for cfg in ven_client.sensor_configs if cfg.name != config_name]
        db.session.flush()
