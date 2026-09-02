from __future__ import annotations

from dataclasses import dataclass
from typing import TypeVar, cast

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
