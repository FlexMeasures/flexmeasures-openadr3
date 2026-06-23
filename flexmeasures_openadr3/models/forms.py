from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

ValidatedDataT = TypeVar("ValidatedDataT")


@dataclass
class FormValidationErrors:
    """Field-level validation messages for HTML forms."""

    _errors: dict[str, str] = field(default_factory=dict)

    def add(self, field_name: str, message: str) -> None:
        """Record a validation error for a form field."""
        if field_name not in self._errors:
            self._errors[field_name] = message

    def __getattr__(self, name: str) -> str:
        """Return the error message for a field, or an empty string."""
        return self._errors.get(name, "")

    def __bool__(self) -> bool:
        """Return whether any validation errors are present."""
        return bool(self._errors)

    def __len__(self) -> int:
        """Return the number of validation errors."""
        return len(self._errors)

    def __iter__(self) -> Iterator[tuple[str, str]]:
        """Iterate over field names and their error messages."""
        return iter(self._errors.items())


@dataclass(frozen=True, slots=True)
class FormValidationResult[ValidatedDataT]:
    """Outcome of validating HTML form input."""

    data: ValidatedDataT | None
    errors: FormValidationErrors

    @property
    def is_valid(self) -> bool:
        """Return whether validation succeeded without errors."""
        return self.data is not None and not self.errors


@dataclass(frozen=True, slots=True)
class VenClientFormValues:
    """Raw string values submitted by the VEN client create/update form."""

    name: str = ""
    vtn_url: str = ""
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    oauth_token_url: str = ""
    scopes: str = ""

    @classmethod
    def empty(cls) -> VenClientFormValues:
        """Return an empty form state for a new VEN client."""
        return cls()

    def with_field(self, field_name: str, value: str) -> VenClientFormValues:
        """Return a copy with one field replaced."""
        return replace(self, **{field_name: value})

    def with_request_fields(self, field_names: tuple[str, ...], form: Mapping[str, str]) -> VenClientFormValues:
        """Return a copy with fields populated from a request form mapping."""
        updated = self
        for field_name in field_names:
            updated = updated.with_field(field_name, form.get(field_name, "").strip())
        return updated

    def to_validation_input(self) -> dict[str, str]:
        """Convert form values to the dict expected by the validation model."""
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
    """Raw string values submitted by the polling schedule form."""

    name: str = ""
    targets: str = ""
    utc_trigger_time: str = ""
    fetch_import_capacity_limits: str = ""
    fetch_export_capacity_limits: str = ""

    @classmethod
    def empty(cls) -> VenSensorConfigFormValues:
        """Return an empty form state for a new polling schedule."""
        return cls()

    @classmethod
    def from_post_values(cls, post_values: VenSensorConfigPostValues) -> VenSensorConfigFormValues:
        """Build display form values from validated POST data."""
        return cls(
            name=post_values.name,
            targets=post_values.targets,
            utc_trigger_time=post_values.utc_trigger_time,
            fetch_import_capacity_limits="on" if post_values.fetch_import_capacity_limits else "",
            fetch_export_capacity_limits="on" if post_values.fetch_export_capacity_limits else "",
        )


@dataclass(frozen=True, slots=True)
class VenSensorConfigPostValues:
    """Parsed POST values for a polling schedule form submission."""

    name: str
    targets: str
    utc_trigger_time: str
    fetch_import_capacity_limits: bool
    fetch_export_capacity_limits: bool

    def to_validation_input(self) -> dict[str, str | bool]:
        """Convert POST values to the dict expected by the validation model."""
        return {
            "name": self.name,
            "targets": self.targets,
            "utc_trigger_time": self.utc_trigger_time,
            "fetch_import_capacity_limits": self.fetch_import_capacity_limits,
            "fetch_export_capacity_limits": self.fetch_export_capacity_limits,
        }
