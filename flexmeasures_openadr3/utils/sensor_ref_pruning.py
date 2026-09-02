from __future__ import annotations

from typing import cast

type JsonScalar = int | str | None
type JsonValue = JsonScalar | dict[str, "JsonValue"] | list["JsonValue"]


class RemoveMarker:
    """Sentinel indicating a JSON node should be removed from its parent."""

    __slots__ = ()

    def __repr__(self) -> str:
        """Return a stable debug representation."""
        return "REMOVE"


REMOVE = RemoveMarker()


def _prune_flex_config_sensor_refs(value: JsonValue, sensor_id: int) -> JsonValue | RemoveMarker:
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
        The value with sensor references removed. Can be:
        - `REMOVE` (sentinel object): the entire entry should be removed from its parent
        - A pruned dict/list/scalar: the value with refs removed

    Example:
        >>> value = {"soc-max": {"sensor": 42}, "limit": "10 kW"}
        >>> _prune_flex_config_sensor_refs(value, sensor_id=42)
        {'limit': '10 kW'}

    """
    if isinstance(value, dict):
        if set(value.keys()) == {"sensor"} and value.get("sensor") == sensor_id:
            return REMOVE

        pruned_dict: dict[str, JsonValue] = {}
        for key, nested in value.items():
            if key == "inflexible-device-sensors" and isinstance(nested, list):
                pruned_dict[key] = [entry for entry in nested if entry != sensor_id]
                continue

            nested_pruned = _prune_flex_config_sensor_refs(nested, sensor_id)
            if nested_pruned is not REMOVE:
                pruned_dict[key] = cast("JsonValue", nested_pruned)
        return pruned_dict

    if isinstance(value, list):
        pruned_items = (_prune_flex_config_sensor_refs(item, sensor_id) for item in value)
        return [cast("JsonValue", item) for item in pruned_items if item is not REMOVE]

    return value


def _prune_sensors_to_show_refs(value: list[JsonValue] | None, sensor_id: int) -> list[JsonValue] | None:
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
        The list with sensor references removed (and now-empty groups filtered out), or `value`
        unchanged if it isn't a list.

    Example:
        >>> value = [42, [43, 42], {"sensor": 42}]
        >>> _prune_sensors_to_show_refs(value, sensor_id=42)
        [[43]]

    """
    if not isinstance(value, list):
        return value

    cleaned: list[JsonValue] = []
    for entry in value:
        if isinstance(entry, int):
            if entry != sensor_id:
                cleaned.append(entry)
            continue

        if isinstance(entry, list):
            new_group = [sid for sid in entry if sid != sensor_id]
            if new_group:
                cleaned.append(new_group)
            continue

        if isinstance(entry, dict):
            pruned_entry = _prune_sensors_to_show_entry(entry, sensor_id)
            if pruned_entry is not REMOVE:
                cleaned.append(cast("JsonValue", pruned_entry))
            continue

        cleaned.append(entry)

    return cleaned


def _prune_sensors_to_show_entry(  # noqa: C901
    entry: dict[str, JsonValue], sensor_id: int
) -> dict[str, JsonValue] | RemoveMarker:
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
        `REMOVE` if the entire entry should be removed from its parent list, otherwise the
        (possibly modified) entry dict.

    Example:
        >>> entry = {"sensor": 42, "title": "Power"}
        >>> _prune_sensors_to_show_entry(entry, sensor_id=42) is REMOVE
        True

    """
    if "sensor" in entry:
        return REMOVE if entry.get("sensor") == sensor_id else entry

    if "sensors" in entry and isinstance(entry["sensors"], list):
        new_sensors = [sid for sid in entry["sensors"] if sid != sensor_id]
        if not new_sensors:
            return REMOVE
        return {**entry, "sensors": new_sensors}

    if "plots" in entry and isinstance(entry["plots"], list):
        new_plots: list[JsonValue] = []
        for plot in entry["plots"]:
            if not isinstance(plot, dict):
                new_plots.append(plot)
                continue
            if plot.get("sensor") == sensor_id:
                continue
            if "sensors" in plot and isinstance(plot["sensors"], list):
                new_plot_sensors = [sid for sid in plot["sensors"] if sid != sensor_id]
                if new_plot_sensors:
                    new_plots.append({**plot, "sensors": new_plot_sensors})
                continue
            new_plots.append(plot)

        if not new_plots:
            return REMOVE
        return {**entry, "plots": new_plots}

    return entry


def _prune_sensors_to_show_as_kpis_refs(value: list[JsonValue] | None, sensor_id: int) -> list[JsonValue] | None:
    """
    Remove sensor references from sensors_to_show_as_kpis JSON list.

    This function handles sensors_to_show_as_kpis lists which support:
    - Bare sensor IDs: [42, 43, ...]
    - Dict entries: [{"sensor": 42, "title": "...", "function": "sum"}, ...]

    Args:
        value: The sensors_to_show_as_kpis JSON list (or None). Each entry is an int or dict.
        sensor_id: The ID of the sensor to remove references to.

    Returns:
        The list with sensor references removed, or `value` unchanged if it isn't a list.

    Example:
        >>> value = [42, {"sensor": 42, "title": "Temp KPI", "function": "sum"}]
        >>> _prune_sensors_to_show_as_kpis_refs(value, sensor_id=42)
        []

    """
    if not isinstance(value, list):
        return value

    return [entry for entry in value if not (isinstance(entry, int) and entry == sensor_id) and not (isinstance(entry, dict) and entry.get("sensor") == sensor_id)]
