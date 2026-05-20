from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from typing import Generic, TypeVar

ValidatedDataT = TypeVar("ValidatedDataT")


@dataclass
class FormValidationErrors:
    """Field-level validation messages for HTML forms."""

    _errors: dict[str, str] = field(default_factory=dict)

    def add(self, field_name: str, message: str) -> None:
        if field_name not in self._errors:
            self._errors[field_name] = message

    def __getattr__(self, name: str) -> str:
        return self._errors.get(name, "")

    def __bool__(self) -> bool:
        return bool(self._errors)

    def __len__(self) -> int:
        return len(self._errors)

    def __iter__(self) -> Iterator[tuple[str, str]]:
        return iter(self._errors.items())


@dataclass(frozen=True, slots=True)
class FormValidationResult(Generic[ValidatedDataT]):
    data: ValidatedDataT | None
    errors: FormValidationErrors

    @property
    def is_valid(self) -> bool:
        return self.data is not None and not self.errors


@dataclass(frozen=True, slots=True)
class VenClientFormValues:
    name: str = ""
    vtn_url: str = ""
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    oauth_token_url: str = ""
    scopes: str = ""

    @classmethod
    def empty(cls) -> VenClientFormValues:
        return cls()

    def with_field(self, field_name: str, value: str) -> VenClientFormValues:
        return replace(self, **{field_name: value})

    def with_request_fields(
        self, field_names: tuple[str, ...], form: Mapping[str, str]
    ) -> VenClientFormValues:
        updated = self
        for field_name in field_names:
            updated = updated.with_field(field_name, form.get(field_name, "").strip())
        return updated

    def to_validation_input(self) -> dict[str, str]:
        return {
            "name": self.name,
            "vtn_url": self.vtn_url,
            "oauth_client_id": self.oauth_client_id,
            "oauth_client_secret": self.oauth_client_secret,
            "oauth_token_url": self.oauth_token_url,
            "scopes": self.scopes,
        }


@dataclass(frozen=True, slots=True)
class VenSensorConfigFormValues:
    name: str = ""
    targets: str = ""
    utc_trigger_time: str = ""
    fetch_import_capacity_limits: str = ""
    fetch_export_capacity_limits: str = ""

    @classmethod
    def empty(cls) -> VenSensorConfigFormValues:
        return cls()

    @classmethod
    def from_post_values(
        cls, post_values: VenSensorConfigPostValues
    ) -> VenSensorConfigFormValues:
        return cls(
            name=post_values.name,
            targets=post_values.targets,
            utc_trigger_time=post_values.utc_trigger_time,
            fetch_import_capacity_limits="on"
            if post_values.fetch_import_capacity_limits
            else "",
            fetch_export_capacity_limits="on"
            if post_values.fetch_export_capacity_limits
            else "",
        )


@dataclass(frozen=True, slots=True)
class VenSensorConfigPostValues:
    name: str
    targets: str
    utc_trigger_time: str
    fetch_import_capacity_limits: bool
    fetch_export_capacity_limits: bool

    def to_validation_input(self) -> dict[str, str | bool]:
        return {
            "name": self.name,
            "targets": self.targets,
            "utc_trigger_time": self.utc_trigger_time,
            "fetch_import_capacity_limits": self.fetch_import_capacity_limits,
            "fetch_export_capacity_limits": self.fetch_export_capacity_limits,
        }
