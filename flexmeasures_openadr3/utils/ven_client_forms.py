from __future__ import annotations

from datetime import time  # NOQA: TC003
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, ValidationError, field_validator

from flexmeasures_openadr3.models.forms import (
    FormValidationErrors,
    FormValidationResult,
    VenClientFormValues,
    VenSensorConfigFormValues,
    VenSensorConfigPostValues,
)

if TYPE_CHECKING:
    from flexmeasures_openadr3.utils.ven_clients import VenClient, VenClientRepository, VenSensorConfig


def _parse_csv_values(raw_value: str) -> list[str]:
    """Parse a comma-separated string into a list of trimmed, non-empty values."""
    return [item.strip() for item in raw_value.split(",") if item.strip()]


# ---------------------------------------------------------------------------
# Form validation models
# ---------------------------------------------------------------------------


class VenClientFormData(BaseModel):
    """Validated VEN client form data (shared connection fields)."""

    name: str
    vtn_url: str
    oauth_client_id: str
    oauth_client_secret: str
    oauth_token_url: str
    scopes: list[str] = Field(default_factory=list)

    @field_validator(
        "name",
        "vtn_url",
        "oauth_client_id",
        "oauth_client_secret",
        "oauth_token_url",
    )
    @classmethod
    def _strip_required_string_fields(cls, value: str) -> str:
        return value.strip()

    @field_validator("scopes", mode="before")
    @classmethod
    def _parse_csv_string_fields(cls, value: str | list[str] | tuple[str, ...] | None) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return _parse_csv_values(value)
        if isinstance(value, tuple):
            return [item.strip() for item in value if item.strip()]
        return [item.strip() for item in value if item.strip()]

    @classmethod
    def from_form_values(cls, field_values: VenClientFormValues) -> VenClientFormData:
        """Build validated data from raw VEN client form values."""
        return cls.model_validate(field_values.to_validation_input())


class VenSensorConfigFormData(BaseModel):
    """Validated polling schedule form data."""

    name: str
    targets: list[str] = Field(default_factory=list)
    utc_trigger_time: time
    fetch_import_capacity_limits: bool = False
    fetch_export_capacity_limits: bool = False

    @field_validator("targets", mode="before")
    @classmethod
    def _parse_targets_csv(cls, value: str | list[str] | tuple[str, ...] | None) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return _parse_csv_values(value)
        if isinstance(value, tuple):
            return [item.strip() for item in value if item.strip()]
        return [item.strip() for item in value if item.strip()]

    @classmethod
    def from_post_values(cls, post_values: VenSensorConfigPostValues) -> VenSensorConfigFormData:
        """Build validated data from parsed polling schedule POST values."""
        return cls.model_validate(post_values.to_validation_input())


# ---------------------------------------------------------------------------
# Form helper functions
# ---------------------------------------------------------------------------

VEN_CLIENT_FORM_FIELDS: tuple[str, ...] = (
    "name",
    "vtn_url",
    "oauth_client_id",
    "oauth_client_secret",
    "oauth_token_url",
    "scopes",
)


def build_ven_client_form_values(
    ven_client: VenClient | None = None,
) -> VenClientFormValues:
    """Build form values for VEN client create/update screens."""
    if ven_client is None:
        return VenClientFormValues.empty()

    return VenClientFormValues(
        name=ven_client.name,
        vtn_url=ven_client.vtn_url,
        oauth_client_id="",
        oauth_client_secret="",
        oauth_token_url=ven_client.oauth_token_url,
        scopes=", ".join(ven_client.scopes),
    )


def build_ven_sensor_config_form_values(
    ven_client: VenClient | None = None,
    config_name: str | None = None,
) -> VenSensorConfigFormValues:
    """Build form values for sensor config create/update screens."""
    if ven_client is None:
        return VenSensorConfigFormValues.empty()

    source: VenSensorConfig | None = None
    if config_name is not None:
        source = ven_client.get_sensor_config(config_name)
    elif ven_client.sensor_configs:
        source = ven_client.sensor_configs[-1]

    if source is None:
        return VenSensorConfigFormValues.empty()

    return VenSensorConfigFormValues(
        name=source.name,
        targets=", ".join(source.targets),
        utc_trigger_time=source.utc_trigger_time,
        fetch_import_capacity_limits=("on" if source.fetch_import_capacity_limits else ""),
        fetch_export_capacity_limits=("on" if source.fetch_export_capacity_limits else ""),
    )


def validate_ven_client_form(
    field_values: VenClientFormValues,
    ven_client_repository: VenClientRepository,
    current_name: str | None = None,
) -> FormValidationResult[VenClientFormData]:
    """Validate VEN client form values, checking uniqueness."""
    errors = FormValidationErrors()
    ven_client_data: VenClientFormData | None = None

    try:
        ven_client_data = VenClientFormData.from_form_values(field_values)
    except ValidationError as validation_error:
        for issue in validation_error.errors():
            field_name = str(issue["loc"][-1])
            errors.add(field_name, issue["msg"])
    else:
        if ven_client_data is None:
            msg = "Validated VEN client form data missing despite successful validation."
            raise RuntimeError(msg)
        if current_name is None:
            if not ven_client_data.oauth_client_id:
                errors.add("oauth_client_id", "OAuth client ID is required.")
            if not ven_client_data.oauth_client_secret:
                errors.add("oauth_client_secret", "OAuth client secret is required.")
        existing = ven_client_repository.find_by_name(ven_client_data.name)
        if existing and ven_client_data.name != current_name:
            errors.add("name", "A VEN client with this name already exists.")

    return FormValidationResult(data=ven_client_data if not errors else None, errors=errors)


def validate_ven_sensor_config_form(
    post_values: VenSensorConfigPostValues,
    existing_configs: list[VenSensorConfig] | None = None,
    current_name: str | None = None,
) -> FormValidationResult[VenSensorConfigFormData]:
    """Validate a sensor config form payload."""
    errors = FormValidationErrors()
    config: VenSensorConfigFormData | None = None

    try:
        config = VenSensorConfigFormData.from_post_values(post_values)
    except ValidationError as validation_error:
        for issue in validation_error.errors():
            field_name = str(issue["loc"][-1])
            errors.add(field_name, issue["msg"])
    else:
        if not (config.fetch_import_capacity_limits or config.fetch_export_capacity_limits):
            errors.add("limits", "Select at least one limit type (import and/or export).")

        if existing_configs is not None and config.name != current_name and any(cfg.name == config.name for cfg in existing_configs):
            errors.add("name", "A sensor config with this name already exists.")

    return FormValidationResult(data=config if not errors else None, errors=errors)
