from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

VEN_CLIENT_ATTRIBUTE_KEY = "ven_client"


@dataclass(frozen=True, slots=True)
class VenClientAttributePayload:
    """VEN connection settings stored on a generic asset (its polling schedules are automations)."""

    vtn_url: str = ""
    oauth_token_url: str = ""
    scopes: tuple[str, ...] = ()

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

        return cls(
            vtn_url=str(raw_payload.get("vtn_url", "") or ""),
            oauth_token_url=str(raw_payload.get("oauth_token_url", "") or ""),
            scopes=scopes,
        )

    def to_json(self) -> dict[str, object]:
        """Serialize the payload for storage in asset attributes."""
        return {
            "vtn_url": self.vtn_url,
            "oauth_token_url": self.oauth_token_url,
            "scopes": list(self.scopes),
        }
