from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

import sqlalchemy as sa
from flask_login import current_user
from flexmeasures.data import db
from flexmeasures.data.models.audit_log import AssetAuditLog
from flexmeasures.data.models.generic_assets import GenericAsset, GenericAssetType
from flexmeasures.data.models.time_series import Sensor, TimedBelief
from flexmeasures.utils.time_utils import get_timezone
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from flexmeasures_openadr3.utils.sensor_ref_pruning import (
    REMOVE,
    _prune_flex_config_sensor_refs,
    _prune_sensors_to_show_as_kpis_refs,
    _prune_sensors_to_show_refs,
)

if TYPE_CHECKING:
    from flexmeasures_openadr3.utils.ven_clients import VenSensorConfig

IMPORT_CAPACITY_LIMIT_SENSOR_NAME = "import-capacity-limit"
EXPORT_CAPACITY_LIMIT_SENSOR_NAME = "export-capacity-limit"

VEN_ASSET_TYPE_NAME = "OpenADR VEN"
VEN_ASSET_TYPE_DESCRIPTION = "OpenADR Virtual End Node for demand response signals"
VEN_SENSOR_UNIT = "kW"
VEN_SENSOR_EVENT_RESOLUTION = timedelta(minutes=15)


@dataclass(frozen=True, slots=True)
class VenSensorPair:
    """Import and export sensors linked to a polling schedule."""

    import_sensor: Sensor | None
    export_sensor: Sensor | None


class VenAssetRepository:
    """Handles persistence of VEN GenericAsset and GenericAssetType objects."""

    def get_or_create_asset_type(self) -> GenericAssetType:
        """
        Return the OpenADR VEN asset type, creating it if needed.

        Commits immediately rather than relying on the caller to commit or roll
        back: this is also called from long-lived processes (the cron-scheduler
        CLI command and its resync thread) whose app context is never torn down,
        so an uncommitted insert here would hold its unique-name lock forever and
        block every other process trying to read-or-create the same row.
        """
        asset_type = db.session.execute(select(GenericAssetType).filter_by(name=VEN_ASSET_TYPE_NAME)).scalar_one_or_none()
        if asset_type is not None:
            return asset_type

        asset_type = GenericAssetType(
            name=VEN_ASSET_TYPE_NAME,
            description=VEN_ASSET_TYPE_DESCRIPTION,
        )
        db.session.add(asset_type)
        try:
            db.session.commit()
        except IntegrityError:
            # Lost the race to a concurrent creator; use the row it committed.
            db.session.rollback()
            asset_type = db.session.execute(select(GenericAssetType).filter_by(name=VEN_ASSET_TYPE_NAME)).scalar_one()

        return asset_type

    def create_asset(self, asset_name: str) -> GenericAsset:
        """Create a new VEN generic asset for the current user."""
        asset_type = self.get_or_create_asset_type()
        existing = db.session.execute(
            select(GenericAsset).filter_by(
                name=asset_name,
                generic_asset_type_id=asset_type.id,
            )
        ).scalar_one_or_none()

        if existing is not None:
            msg = f"Asset with name '{asset_name}' already exists."
            raise ValueError(msg)

        asset = GenericAsset(
            name=asset_name,
            generic_asset_type_id=asset_type.id,
            account_id=current_user.id,
        )
        db.session.add(asset)
        db.session.flush()
        return asset

    def find_by_id(self, asset_id: int) -> GenericAsset | None:
        """Find a VEN asset by database id."""
        return db.session.execute(select(GenericAsset).filter_by(id=asset_id)).scalar_one_or_none()

    def find_by_name(self, asset_name: str) -> GenericAsset | None:
        """Find a VEN asset by name."""
        return db.session.execute(select(GenericAsset).filter_by(name=asset_name)).scalar_one_or_none()

    def list_all(self) -> list[GenericAsset]:
        """List all generic assets of the OpenADR VEN type."""
        asset_type = self.get_or_create_asset_type()
        return list(db.session.execute(select(GenericAsset).filter_by(generic_asset_type_id=asset_type.id)).scalars())


class VenSensorRepository:
    """Handles persistence of FlexMeasures Sensor objects tied to VEN configs."""

    def _cleanup_sensor_references_in_assets(
        self,
        sensor_id: int,
        sensor_name: str | None = None,
    ) -> int:
        """
        Remove references to a sensor in JSONB config fields across assets.

        Returns the number of updated assets.
        """
        vars_json = sa.func.jsonb_build_object("sid", sensor_id)
        candidates = db.session.scalars(
            sa.select(GenericAsset).where(
                sa.or_(
                    sa.func.jsonb_path_exists(
                        GenericAsset.flex_model,
                        "$.**.sensor ? (@ == $sid)",
                        vars_json,
                    ),
                    sa.func.jsonb_path_exists(
                        GenericAsset.flex_context,
                        "$.**.sensor ? (@ == $sid)",
                        vars_json,
                    ),
                    sa.func.jsonb_path_exists(
                        GenericAsset.flex_context,
                        '$."inflexible-device-sensors"[*] ? (@ == $sid)',
                        vars_json,
                    ),
                    sa.func.jsonb_path_exists(
                        GenericAsset.sensors_to_show,
                        "$.**.sensor ? (@ == $sid)",
                        vars_json,
                    ),
                    sa.func.jsonb_path_exists(
                        GenericAsset.sensors_to_show,
                        "$.**.sensors[*] ? (@ == $sid)",
                        vars_json,
                    ),
                    sa.func.jsonb_path_exists(
                        GenericAsset.sensors_to_show,
                        "$[*] ? (@ == $sid)",
                        vars_json,
                    ),
                    sa.func.jsonb_path_exists(
                        GenericAsset.sensors_to_show_as_kpis,
                        "$.**.sensor ? (@ == $sid)",
                        vars_json,
                    ),
                )
            )
        ).all()

        changed_assets = 0
        for asset in candidates:
            pruned_fields = (
                ("flex-model", "flex_model", _prune_flex_config_sensor_refs(asset.flex_model, sensor_id)),
                ("flex-context", "flex_context", _prune_flex_config_sensor_refs(asset.flex_context, sensor_id)),
                ("sensors-to-show", "sensors_to_show", _prune_sensors_to_show_refs(asset.sensors_to_show, sensor_id)),
                (
                    "sensors-to-show-as-kpis",
                    "sensors_to_show_as_kpis",
                    _prune_sensors_to_show_as_kpis_refs(asset.sensors_to_show_as_kpis, sensor_id),
                ),
            )
            changed_fields = [(field_name, attr, pruned) for field_name, attr, pruned in pruned_fields if pruned is REMOVE or pruned != getattr(asset, attr)]
            if not changed_fields:
                continue

            sensor_label = f"'{sensor_name}': {sensor_id}" if sensor_name else str(sensor_id)
            for field_name, attr, pruned in changed_fields:
                AssetAuditLog.add_record(
                    asset,
                    f"Removed sensor reference {sensor_label} from {field_name} (because sensor has been deleted).",
                )
                if pruned is not REMOVE:
                    setattr(asset, attr, pruned)

            db.session.add(asset)
            changed_assets += 1

        return changed_assets

    def _delete_sensor(self, sensor: Sensor) -> None:
        """
        Delete a sensor and all its time series data.

        Does not commit the session.
        Cleans up sensor references in asset JSONB fields.
        Creates an audit log.
        """
        sensor_name = sensor.name
        self._cleanup_sensor_references_in_assets(sensor.id, sensor.name)
        db.session.execute(delete(TimedBelief).filter_by(sensor_id=sensor.id))
        AssetAuditLog.add_record(sensor.generic_asset, f"Deleted sensor '{sensor_name}': {sensor.id}")
        db.session.execute(delete(Sensor).filter_by(id=sensor.id))

    def find_sensor(self, sensor_name: str, asset: GenericAsset) -> Sensor | None:
        """Find a sensor by name on the given VEN asset."""
        return db.session.execute(
            select(Sensor).filter_by(
                name=sensor_name,
                generic_asset_id=asset.id,
            )
        ).scalar_one_or_none()

    def get_or_create_sensor(self, sensor_name: str, asset: GenericAsset) -> Sensor:
        """Return an existing sensor or create one with default VEN settings."""
        sensor = self.find_sensor(sensor_name, asset)

        if sensor is None:
            sensor = Sensor(
                name=sensor_name,
                generic_asset_id=asset.id,
                unit=VEN_SENSOR_UNIT,
                timezone=get_timezone(of_user=True).zone or "UTC",
                event_resolution=VEN_SENSOR_EVENT_RESOLUTION,
                attributes={"ven_client_name": asset.name},
            )
            db.session.add(sensor)
            db.session.flush()

        return sensor

    def import_sensor_name(self, config_name: str) -> str:
        """Return the FlexMeasures sensor name for import capacity limits."""
        return f"{IMPORT_CAPACITY_LIMIT_SENSOR_NAME}-{config_name}"

    def export_sensor_name(self, config_name: str) -> str:
        """Return the FlexMeasures sensor name for export capacity limits."""
        return f"{EXPORT_CAPACITY_LIMIT_SENSOR_NAME}-{config_name}"

    def resolve_sensors_for_config(self, asset: GenericAsset, sensor_config: VenSensorConfig) -> VenSensorPair:
        """Find existing sensors for a config without creating them."""
        import_sensor = None
        export_sensor = None

        if sensor_config.fetch_import_capacity_limits:
            import_sensor = self.find_sensor(self.import_sensor_name(sensor_config.name), asset)
        if sensor_config.fetch_export_capacity_limits:
            export_sensor = self.find_sensor(self.export_sensor_name(sensor_config.name), asset)

        return VenSensorPair(import_sensor=import_sensor, export_sensor=export_sensor)

    def ensure_sensors_for_config(self, asset: GenericAsset, sensor_config: VenSensorConfig) -> VenSensorPair:
        """Get or create the sensors required by a config."""
        import_sensor = None
        export_sensor = None

        if sensor_config.fetch_import_capacity_limits:
            import_sensor = self.get_or_create_sensor(self.import_sensor_name(sensor_config.name), asset)
        if sensor_config.fetch_export_capacity_limits:
            export_sensor = self.get_or_create_sensor(self.export_sensor_name(sensor_config.name), asset)

        return VenSensorPair(import_sensor=import_sensor, export_sensor=export_sensor)

    def delete_sensors_for_config(self, asset: GenericAsset, config_name: str) -> None:
        """Delete both import and export sensors associated with a config name."""
        for sensor_name in (
            self.import_sensor_name(config_name),
            self.export_sensor_name(config_name),
        ):
            sensor = self.find_sensor(sensor_name, asset)
            if sensor is not None:
                self._delete_sensor(sensor)
