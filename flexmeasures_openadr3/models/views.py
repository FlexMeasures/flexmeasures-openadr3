from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class VenSensorConfigOverview:
    """Row model for the polling-schedule overview template."""

    ven_id: int
    ven_name: str
    config_name: str
    targets: tuple[str, ...]
    utc_trigger_time: str
    fetch_import_capacity_limits: bool
    fetch_export_capacity_limits: bool
    import_sensor_id: int | None
    export_sensor_id: int | None
