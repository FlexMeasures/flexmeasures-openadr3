from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class VenSensorConfigOverview:
    """Row model for the polling-schedule overview template."""

    ven_id: int
    ven_name: str
    config_name: str
    targets: tuple[str, ...]
    automation_id: int
    cronstr: str
    schedule_description: str
    timezone: str
    active: bool
    fetch_import_capacity_limits: bool
    fetch_export_capacity_limits: bool
    import_sensor_id: int | None
    export_sensor_id: int | None
