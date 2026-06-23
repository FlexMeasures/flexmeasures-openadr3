from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, TypeVar, cast

import sqlalchemy as sa
from flask_login import current_user
from flexmeasures.data import db
from flexmeasures.data.models.audit_log import AssetAuditLog
from flexmeasures.data.models.generic_assets import GenericAsset, GenericAssetType
from flexmeasures.data.models.time_series import Sensor, TimedBelief
from flexmeasures.utils.time_utils import get_timezone
from sqlalchemy import delete, select

if TYPE_CHECKING:
    from flexmeasures_openadr3.utils.ven_clients import VenSensorConfig

type JsonScalar = int | str | None
type JsonValue = JsonScalar | dict[str, "JsonValue"] | list["JsonValue"]
PrunedValueT = TypeVar("PrunedValueT", bound=JsonValue | None)


class RemoveMarker:
    """Sentinel indicating a JSON node should be removed from its parent."""

    __slots__ = ()

    def __repr__(self) -> str:
        """Return a stable debug representation."""
        return "REMOVE"


REMOVE = RemoveMarker()


@dataclass(frozen=True, slots=True)
class PruneResult[PrunedValueT: JsonValue | None]:
    """Result of pruning sensor references from a JSON subtree."""

    value: PrunedValueT | RemoveMarker
    changed: bool

    @property
    def should_remove(self) -> bool:
        """Return whether the pruned value should be removed from its parent."""
        return self.value is REMOVE


IMPORT_CAPACITY_LIMIT_SENSOR_NAME = "import-capacity-limit"
EXPORT_CAPACITY_LIMIT_SENSOR_NAME = "export-capacity-limit"

VEN_ASSET_TYPE_NAME = "OpenADR VEN"
VEN_ASSET_TYPE_DESCRIPTION = "OpenADR Virtual End Node for demand response signals"
VEN_SENSOR_UNIT = "kW"
VEN_SENSOR_EVENT_RESOLUTION = timedelta(minutes=15)


def _prune_flex_config_sensor_refs(value: JsonValue, sensor_id: int) -> PruneResult[JsonValue]:  # noqa: C901
    """
    Recursively remove sensor references from nested flex_model/flex_context JSON structures.

    This function handles deeply nested JSON objects and lists from flex_model and flex_context
    JSONB columns. It scans for sensor references in two forms:
    - Direct objects: {"sensor": sensor_id_to_remove}
    - Lists: [sensor_id_to_remove, ...] in "inflexible-device-sensors" keys

    Args:
        value: A JSON-like value (dict, list, int, str, None) from flex_model/flex_context.
               Can be arbitrarily nested.
        sensor_id: The ID of the sensor to remove references to.

    Returns:
        A tuple (pruned_value, changed):
        - pruned_value: The value with sensor references removed. Can be:
            - `_REMOVE` (sentinel object): Remove this entire entry from parent
            - A pruned dict/list/scalar: The value with refs removed
        - changed (bool): True if any references were actually removed.

    Example:
        >>> value = {"soc-max": {"sensor": 42}, "limit": "10 kW"}
        >>> pruned, did_change = _prune_flex_config_sensor_refs(value, sensor_id=42)
        >>> pruned
        {'limit': '10 kW'}
        >>> did_change
        True

    """
    if isinstance(value, dict):
        if set(value.keys()) == {"sensor"} and value.get("sensor") == sensor_id:
            return PruneResult(value=REMOVE, changed=True)

        changed = False
        pruned_dict: dict[str, JsonValue] = {}
        for key, nested in value.items():
            if key == "inflexible-device-sensors" and isinstance(nested, list):
                new_list = [entry for entry in nested if entry != sensor_id]
                if len(new_list) != len(nested):
                    changed = True
                pruned_dict[key] = new_list
                continue

            nested_result = _prune_flex_config_sensor_refs(nested, sensor_id)
            changed = changed or nested_result.changed
            if nested_result.should_remove:
                changed = True
                continue
            if nested_result.value is not REMOVE:
                pruned_dict[key] = cast("JsonValue", nested_result.value)
        return PruneResult(value=pruned_dict, changed=changed)

    if isinstance(value, list):
        changed = False
        pruned_list: list[JsonValue] = []
        for item in value:
            item_result = _prune_flex_config_sensor_refs(item, sensor_id)
            changed = changed or item_result.changed
            if item_result.should_remove:
                changed = True
                continue
            if item_result.value is not REMOVE:
                pruned_list.append(cast("JsonValue", item_result.value))
        return PruneResult(value=pruned_list, changed=changed)

    return PruneResult(value=value, changed=False)


def _prune_sensors_to_show_refs(value: list[JsonValue] | None, sensor_id: int) -> PruneResult[list[JsonValue] | None]:  # noqa: C901
    """
    Remove sensor references from sensors_to_show JSON list.

    This function handles sensors_to_show lists which support multiple entry formats:
    - Bare sensor IDs: [42, 43, ...]
    - Grouped sensor IDs: [42, [43, 44], ...] (nested lists)
    - Dict entries: [{"sensor": 42, ...}, ...] (delegated to _prune_sensors_to_show_entry)

    Args:
        value: The sensors_to_show JSON list (or None). Each entry can be an int, list of ints, or dict.
        sensor_id: The ID of the sensor to remove references to.

    Returns:
        A tuple (pruned_list, changed):
        - pruned_list: The list with sensor references removed (or empty lists filtered out).
                       Returns None/value unchanged if input is not a list.
        - changed (bool): True if any references were actually removed.

    Example:
        >>> value = [42, [43, 42], {"sensor": 42}]
        >>> pruned, did_change = _prune_sensors_to_show_refs(value, sensor_id=42)
        >>> pruned
        [[43]]
        >>> did_change
        True

    """
    if not isinstance(value, list):
        return PruneResult(value=value, changed=False)

    changed = False
    cleaned: list[JsonValue] = []

    for entry in value:
        if isinstance(entry, int):
            if entry == sensor_id:
                changed = True
                continue
            cleaned.append(entry)
            continue

        if isinstance(entry, list):
            new_group = [sid for sid in entry if sid != sensor_id]
            if len(new_group) != len(entry):
                changed = True
            if new_group:
                cleaned.append(new_group)
            else:
                changed = True
            continue

        if isinstance(entry, dict):
            entry_result = _prune_sensors_to_show_entry(entry, sensor_id)
            changed = changed or entry_result.changed
            if entry_result.should_remove:
                continue
            if entry_result.value is not REMOVE:
                cleaned.append(cast("JsonValue", entry_result.value))
            continue

        cleaned.append(entry)

    return PruneResult(value=cleaned, changed=changed)


def _prune_sensors_to_show_entry(entry: dict[str, JsonValue], sensor_id: int) -> PruneResult[dict[str, JsonValue]]:  # noqa: C901
    """
    Remove sensor references from a single sensors_to_show dict entry.

    Handles three field types within a dict entry:
    - "sensor": Direct sensor ID reference → remove if matches
    - "sensors": List of sensor IDs → filter out matching IDs
    - "plots": List of plot dicts, each may contain "sensor" or "sensors" → recurse

    Args:
        entry: A dict from the sensors_to_show list (e.g., {"sensor": 42, "title": "..."}).
        sensor_id: The ID of the sensor to remove references to.

    Returns:
        A tuple (pruned_entry, changed):
        - pruned_entry: Can be:
            - `_REMOVE`: Remove this entire entry from parent list
            - Modified entry dict: The entry with refs removed
        - changed (bool): True if any references were removed.

    Example:
        >>> entry = {"sensor": 42, "title": "Power"}
        >>> pruned, did_change = _prune_sensors_to_show_entry(entry, sensor_id=42)
        >>> pruned is _REMOVE
        True

    """
    if "sensor" in entry:
        if entry.get("sensor") == sensor_id:
            return PruneResult(value=REMOVE, changed=True)
        return PruneResult(value=entry, changed=False)

    if "sensors" in entry and isinstance(entry["sensors"], list):
        new_sensors = [sid for sid in entry["sensors"] if sid != sensor_id]
        changed = len(new_sensors) != len(entry["sensors"])
        if not new_sensors:
            return PruneResult(value=REMOVE, changed=True)
        copied = dict(entry)
        copied["sensors"] = new_sensors
        return PruneResult(value=copied, changed=changed)

    if "plots" in entry and isinstance(entry["plots"], list):
        changed = False
        new_plots: list[JsonValue] = []
        for plot in entry["plots"]:
            if not isinstance(plot, dict):
                new_plots.append(plot)
                continue
            if plot.get("sensor") == sensor_id:
                changed = True
                continue
            if "sensors" in plot and isinstance(plot["sensors"], list):
                new_sensors = [sid for sid in plot["sensors"] if sid != sensor_id]
                if len(new_sensors) != len(plot["sensors"]):
                    changed = True
                if not new_sensors:
                    changed = True
                    continue
                copied_plot = dict(plot)
                copied_plot["sensors"] = new_sensors
                new_plots.append(copied_plot)
            else:
                new_plots.append(plot)

        if not new_plots:
            return PruneResult(value=REMOVE, changed=True)
        copied = dict(entry)
        copied["plots"] = new_plots
        return PruneResult(value=copied, changed=changed)

    return PruneResult(value=entry, changed=False)


def _prune_sensors_to_show_as_kpis_refs(value: list[JsonValue] | None, sensor_id: int) -> PruneResult[list[JsonValue] | None]:
    """
    Remove sensor references from sensors_to_show_as_kpis JSON list.

    This function handles sensors_to_show_as_kpis lists which support:
    - Bare sensor IDs: [42, 43, ...]
    - Dict entries: [{"sensor": 42, "title": "...", "function": "sum"}, ...]

    Args:
        value: The sensors_to_show_as_kpis JSON list (or None). Each entry is an int or dict.
        sensor_id: The ID of the sensor to remove references to.

    Returns:
        A tuple (pruned_list, changed):
        - pruned_list: The list with sensor references removed.
                       Returns None/value unchanged if input is not a list.
        - changed (bool): True if any references were actually removed.

    Example:
        >>> value = [42, {"sensor": 42, "title": "Temp KPI", "function": "sum"}]
        >>> pruned, did_change = _prune_sensors_to_show_as_kpis_refs(value, sensor_id=42)
        >>> pruned
        []
        >>> did_change
        True

    """
    if not isinstance(value, list):
        return PruneResult(value=value, changed=False)

    changed = False
    cleaned: list[JsonValue] = []
    for entry in value:
        if isinstance(entry, int) and entry == sensor_id:
            changed = True
            continue
        if isinstance(entry, dict) and entry.get("sensor") == sensor_id:
            changed = True
            continue
        cleaned.append(entry)

    return PruneResult(value=cleaned, changed=changed)


@dataclass(frozen=True, slots=True)
class VenSensorPair:
    """Import and export sensors linked to a polling schedule."""

    import_sensor: Sensor | None
    export_sensor: Sensor | None


class VenAssetRepository:
    """Handles persistence of VEN GenericAsset and GenericAssetType objects."""

    def get_or_create_asset_type(self) -> GenericAssetType:
        """Return the OpenADR VEN asset type, creating it if needed."""
        asset_type = db.session.execute(select(GenericAssetType).filter_by(name=VEN_ASSET_TYPE_NAME)).scalar_one_or_none()

        if asset_type is None:
            asset_type = GenericAssetType(
                name=VEN_ASSET_TYPE_NAME,
                description=VEN_ASSET_TYPE_DESCRIPTION,
            )
            db.session.add(asset_type)
            db.session.flush()

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
            flex_model_result = _prune_flex_config_sensor_refs(asset.flex_model, sensor_id)
            flex_context_result = _prune_flex_config_sensor_refs(asset.flex_context, sensor_id)
            sensors_to_show_result = _prune_sensors_to_show_refs(asset.sensors_to_show, sensor_id)
            sensors_to_show_as_kpis_result = _prune_sensors_to_show_as_kpis_refs(asset.sensors_to_show_as_kpis, sensor_id)

            changed = any(
                (
                    flex_model_result.changed,
                    flex_context_result.changed,
                    sensors_to_show_result.changed,
                    sensors_to_show_as_kpis_result.changed,
                )
            )
            if not changed:
                continue

            changed_field_events = (
                (flex_model_result.changed, "flex-model"),
                (flex_context_result.changed, "flex-context"),
                (sensors_to_show_result.changed, "sensors-to-show"),
                (sensors_to_show_as_kpis_result.changed, "sensors-to-show-as-kpis"),
            )
            for field_changed, field_name in changed_field_events:
                if not field_changed:
                    continue
                sensor_label = f"'{sensor_name}': {sensor_id}" if sensor_name else str(sensor_id)
                AssetAuditLog.add_record(
                    asset,
                    f"Removed sensor reference {sensor_label} from {field_name} (because sensor has been deleted).",
                )

            if flex_model_result.value is not REMOVE:
                asset.flex_model = flex_model_result.value
            if flex_context_result.value is not REMOVE:
                asset.flex_context = flex_context_result.value
            if sensors_to_show_result.value is not REMOVE:
                asset.sensors_to_show = sensors_to_show_result.value
            if sensors_to_show_as_kpis_result.value is not REMOVE:
                asset.sensors_to_show_as_kpis = sensors_to_show_as_kpis_result.value
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
