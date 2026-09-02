from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time

VEN_CLIENT_ATTRIBUTE_KEY = "ven_client"


@dataclass(frozen=True, slots=True)
class VenSensorConfigRecord:
    """Serialized polling schedule stored in a VEN asset's JSON attributes."""

    name: str
    targets: tuple[str, ...] = ()
    utc_trigger_time: time | None = None
    fetch_import_capacity_limits: bool = False
    fetch_export_capacity_limits: bool = False
    fetch_events_job_id: str | None = None

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> VenSensorConfigRecord:
        """Build a record from JSON stored on a VEN asset."""
        raw_targets = data.get("targets", [])
        targets: tuple[str, ...]
        targets = tuple(str(item) for item in raw_targets) if isinstance(raw_targets, list) else ()

        job_id = data.get("fetch_events_job_id")
        raw_time = str(data.get("utc_trigger_time", "") or "")
        return cls(
            name=str(data.get("name", "")),
            targets=targets,
            utc_trigger_time=time.fromisoformat(raw_time) if raw_time else None,
            fetch_import_capacity_limits=bool(data.get("fetch_import_capacity_limits", False)),
            fetch_export_capacity_limits=bool(data.get("fetch_export_capacity_limits", False)),
            fetch_events_job_id=str(job_id) if job_id is not None else None,
        )

    def to_json(self) -> dict[str, object]:
        """Serialize the record for storage in asset attributes."""
        payload: dict[str, object] = {
            "name": self.name,
            "targets": list(self.targets),
            "utc_trigger_time": self.utc_trigger_time.isoformat() if self.utc_trigger_time is not None else "",
            "fetch_import_capacity_limits": self.fetch_import_capacity_limits,
            "fetch_export_capacity_limits": self.fetch_export_capacity_limits,
        }
        if self.fetch_events_job_id is not None:
            payload["fetch_events_job_id"] = self.fetch_events_job_id
        return payload


@dataclass(frozen=True, slots=True)
class VenClientAttributePayload:
    """VEN connection settings and polling schedules stored on a generic asset."""

    vtn_url: str = ""
    oauth_token_url: str = ""
    scopes: tuple[str, ...] = ()
    sensor_configs: tuple[VenSensorConfigRecord, ...] = ()

    @classmethod
    def empty(cls) -> VenClientAttributePayload:
        """Return an empty VEN attribute payload."""
        return cls()

    @classmethod
    def from_asset_attributes(cls, attributes: Mapping[str, object] | None) -> VenClientAttributePayload:
        """Parse VEN settings from a generic asset's attributes column."""
        if attributes is None:
            return cls.empty()

        raw_payload = attributes.get(VEN_CLIENT_ATTRIBUTE_KEY, {})
        if not isinstance(raw_payload, Mapping):
            return cls.empty()

        raw_scopes = raw_payload.get("scopes", [])
        scopes: tuple[str, ...]
        scopes = tuple(str(item) for item in raw_scopes) if isinstance(raw_scopes, list) else ()

        raw_configs = raw_payload.get("sensor_configs", [])
        sensor_configs: tuple[VenSensorConfigRecord, ...] = ()
        if isinstance(raw_configs, list):
            sensor_configs = tuple(VenSensorConfigRecord.from_json(item) for item in raw_configs if isinstance(item, Mapping))

        return cls(
            vtn_url=str(raw_payload.get("vtn_url", "") or ""),
            oauth_token_url=str(raw_payload.get("oauth_token_url", "") or ""),
            scopes=scopes,
            sensor_configs=sensor_configs,
        )

    def to_json(self) -> dict[str, object]:
        """Serialize the payload for storage in asset attributes."""
        return {
            "vtn_url": self.vtn_url,
            "oauth_token_url": self.oauth_token_url,
            "scopes": list(self.scopes),
            "sensor_configs": [record.to_json() for record in self.sensor_configs],
        }
