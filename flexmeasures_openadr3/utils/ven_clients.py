from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time  # NOQA: TC003

from flask import current_app
from flexmeasures.data import db
from flexmeasures.data.models.generic_assets import GenericAsset
from flexmeasures.data.models.time_series import Sensor
from flexmeasures.utils.secrets_utils import get_secret, get_secret_paths, store_asset_secret
from openadr3_client.oadr310._ven.client import VirtualEndNodeClient
from openadr3_client.ven.http_factory import VirtualEndNodeHttpClientFactory
from openadr3_client.version import OADRVersion
from pydantic import BaseModel, Field, ValidationError, field_validator

from flexmeasures_openadr3.models.forms import (
    FormValidationErrors,
    FormValidationResult,
    VenClientFormValues,
    VenSensorConfigFormValues,
    VenSensorConfigPostValues,
)
from flexmeasures_openadr3.models.storage import (
    VEN_CLIENT_ATTRIBUTE_KEY,
    VenClientAttributePayload,
    VenSensorConfigRecord,
)
from flexmeasures_openadr3.utils.sensor import VenAssetRepository, VenSensorRepository

OAUTH_CLIENT_ID_SECRET_PATH = "ven_client.oauth_client_id"
OAUTH_CLIENT_SECRET_SECRET_PATH = "ven_client.oauth_client_secret"


class VenClientCredentialsMissingError(RuntimeError):
    """Raised when a VEN client has no OAuth credentials configured."""


def _parse_csv_values(raw_value: str) -> list[str]:
    """Parse a comma-separated string into a list of trimmed, non-empty values."""
    return [item.strip() for item in raw_value.split(",") if item.strip()]


# ---------------------------------------------------------------------------
# Domain models
# ---------------------------------------------------------------------------


@dataclass
class VenSensorConfig:
    """
    A polling schedule that defines when and what to fetch from a VTN.

    Each config maps to zero, one, or two FlexMeasures sensors (import/export).
    """

    name: str
    targets: list[str] = field(default_factory=list)
    utc_trigger_time: time | None = None
    fetch_import_capacity_limits: bool = False
    fetch_export_capacity_limits: bool = False
    fetch_events_job_id: str | None = None
    import_sensor: Sensor | None = field(default=None, repr=False)
    export_sensor: Sensor | None = field(default=None, repr=False)

    def to_record(self) -> VenSensorConfigRecord:
        """Serialize this config to a storable record."""
        return VenSensorConfigRecord(
            name=self.name,
            targets=tuple(self.targets),
            utc_trigger_time=self.utc_trigger_time,
            fetch_import_capacity_limits=self.fetch_import_capacity_limits,
            fetch_export_capacity_limits=self.fetch_export_capacity_limits,
            fetch_events_job_id=self.fetch_events_job_id,
        )

    @classmethod
    def from_record(cls, record: VenSensorConfigRecord) -> VenSensorConfig:
        """Build a config from a stored record without linked sensors."""
        return cls(
            name=record.name,
            targets=list(record.targets),
            utc_trigger_time=record.utc_trigger_time,
            fetch_import_capacity_limits=record.fetch_import_capacity_limits,
            fetch_export_capacity_limits=record.fetch_export_capacity_limits,
            fetch_events_job_id=record.fetch_events_job_id,
        )


@dataclass
class VenClient:
    """
    Domain model representing an OpenADR VEN client.

    Wraps a FlexMeasures GenericAsset and provides typed access to VEN-specific
    attributes stored in the asset's JSON attributes column.
    """

    asset: GenericAsset = field(repr=False)
    vtn_url: str = ""
    oauth_token_url: str = ""
    scopes: list[str] = field(default_factory=list)
    sensor_configs: list[VenSensorConfig] = field(default_factory=list)
    oauth_client_id_is_set: bool = field(default=False, repr=False)
    oauth_client_secret_is_set: bool = field(default=False, repr=False)

    @property
    def id(self) -> int:
        """Return the underlying FlexMeasures asset id."""
        return int(self.asset.id)

    @property
    def name(self) -> str:
        """Return the VEN client display name."""
        return str(self.asset.name)

    @name.setter
    def name(self, value: str) -> None:
        """Update the VEN client display name."""
        self.asset.name = value

    def get_sensor_config(self, config_name: str) -> VenSensorConfig | None:
        """Return a polling schedule by name, if it exists."""
        return next((cfg for cfg in self.sensor_configs if cfg.name == config_name), None)

    def create_http_client(self) -> VirtualEndNodeClient:
        """Build an authenticated OpenADR HTTP client for this VEN."""
        try:
            oauth_client_id = get_secret(self.asset.secrets, OAUTH_CLIENT_ID_SECRET_PATH)
            oauth_client_secret = get_secret(self.asset.secrets, OAUTH_CLIENT_SECRET_SECRET_PATH)
        except KeyError as exc:
            msg = f"VEN client '{self.name}' has no OAuth credentials configured."
            raise VenClientCredentialsMissingError(msg) from exc

        return VirtualEndNodeHttpClientFactory.create_http_ven_client(
            vtn_base_url=self.vtn_url,
            client_id=oauth_client_id,
            client_secret=oauth_client_secret,
            token_url=self.oauth_token_url,
            scopes=self.scopes,
            allow_insecure_http=True,
            version=OADRVersion.OADR_310,
        )  # type: ignore[return-value]

    def to_attribute_payload(self) -> VenClientAttributePayload:
        """Convert this client to the JSON payload stored on the asset."""
        return VenClientAttributePayload(
            vtn_url=self.vtn_url,
            oauth_token_url=self.oauth_token_url,
            scopes=tuple(self.scopes),
            sensor_configs=tuple(cfg.to_record() for cfg in self.sensor_configs),
        )


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class VenClientRepository:
    """Repository for persisting and retrieving VenClient domain objects."""

    def __init__(self) -> None:
        self._assets = VenAssetRepository()
        self._sensors = VenSensorRepository()

    def _build_sensor_configs(self, asset: GenericAsset, records: tuple[VenSensorConfigRecord, ...]) -> list[VenSensorConfig]:
        configs: list[VenSensorConfig] = []
        for record in records:
            sensor_config = VenSensorConfig.from_record(record)
            sensors = self._sensors.resolve_sensors_for_config(asset, sensor_config)
            sensor_config.import_sensor = sensors.import_sensor
            sensor_config.export_sensor = sensors.export_sensor
            configs.append(sensor_config)
        return configs

    def _build_ven_client(self, asset: GenericAsset) -> VenClient:
        payload = VenClientAttributePayload.from_asset_attributes(asset.attributes)
        secret_paths = set(get_secret_paths(asset.secrets or {}))

        return VenClient(
            asset=asset,
            vtn_url=payload.vtn_url,
            oauth_token_url=payload.oauth_token_url,
            scopes=list(payload.scopes),
            sensor_configs=self._build_sensor_configs(asset, payload.sensor_configs),
            oauth_client_id_is_set=OAUTH_CLIENT_ID_SECRET_PATH in secret_paths,
            oauth_client_secret_is_set=OAUTH_CLIENT_SECRET_SECRET_PATH in secret_paths,
        )

    def _persist_payload(self, ven_client: VenClient) -> None:
        """Write VEN-specific, non-secret data back to the asset's attributes column."""
        attributes = dict(ven_client.asset.attributes or {})
        attributes[VEN_CLIENT_ATTRIBUTE_KEY] = ven_client.to_attribute_payload().to_json()
        ven_client.asset.attributes = attributes

    def list_ven_clients(self) -> list[VenClient]:
        """Return all persisted VEN clients."""
        return [self._build_ven_client(asset) for asset in self._assets.list_all()]

    def find_by_id(self, ven_id: int) -> VenClient | None:
        """Find a VEN client by asset id."""
        asset = self._assets.find_by_id(ven_id)
        return self._build_ven_client(asset) if asset else None

    def find_by_name(self, ven_name: str) -> VenClient | None:
        """Find a VEN client by asset name."""
        asset = self._assets.find_by_name(ven_name)
        return self._build_ven_client(asset) if asset else None

    def create(self, form_data: VenClientFormData) -> VenClient:
        """Create a new VEN client from validated form data."""
        asset = self._assets.create_asset(form_data.name)
        ven_client = VenClient(
            asset=asset,
            vtn_url=form_data.vtn_url,
            oauth_token_url=form_data.oauth_token_url,
            scopes=form_data.scopes,
        )
        store_asset_secret(asset, OAUTH_CLIENT_ID_SECRET_PATH, form_data.oauth_client_id)
        store_asset_secret(asset, OAUTH_CLIENT_SECRET_SECRET_PATH, form_data.oauth_client_secret)
        self._persist_payload(ven_client)
        db.session.flush()
        return self._build_ven_client(asset)

    def update(self, ven_client: VenClient, form_data: VenClientFormData) -> VenClient:
        """Update an existing VEN client from validated form data. A blank credential keeps the existing one."""
        ven_client.name = form_data.name
        ven_client.vtn_url = form_data.vtn_url
        ven_client.oauth_token_url = form_data.oauth_token_url
        ven_client.scopes = form_data.scopes
        if form_data.oauth_client_id:
            store_asset_secret(ven_client.asset, OAUTH_CLIENT_ID_SECRET_PATH, form_data.oauth_client_id)
        if form_data.oauth_client_secret:
            store_asset_secret(ven_client.asset, OAUTH_CLIENT_SECRET_SECRET_PATH, form_data.oauth_client_secret)
        self._persist_payload(ven_client)
        db.session.flush()
        return ven_client

    def delete(self, ven_client: VenClient) -> None:
        """Delete a VEN client and its underlying asset."""
        db.session.delete(ven_client.asset)

    def append_sensor_config(self, ven_client: VenClient, form_data: VenSensorConfigFormData) -> VenSensorConfig:
        """Append a new sensor config and ensure its sensors exist."""
        if ven_client.get_sensor_config(form_data.name) is not None:
            msg = f"Sensor config with name '{form_data.name}' already exists."
            raise ValueError(msg)

        sensor_config = VenSensorConfig(
            name=form_data.name,
            targets=list(form_data.targets),
            utc_trigger_time=form_data.utc_trigger_time,
            fetch_import_capacity_limits=form_data.fetch_import_capacity_limits,
            fetch_export_capacity_limits=form_data.fetch_export_capacity_limits,
        )

        sensors = self._sensors.ensure_sensors_for_config(ven_client.asset, sensor_config)
        sensor_config.import_sensor = sensors.import_sensor
        sensor_config.export_sensor = sensors.export_sensor

        ven_client.sensor_configs.append(sensor_config)
        self._persist_payload(ven_client)
        db.session.flush()
        return sensor_config

    def update_sensor_config(
        self,
        ven_client: VenClient,
        config_name: str,
        form_data: VenSensorConfigFormData,
    ) -> VenSensorConfig:
        """Update an existing sensor config and ensure its sensors exist."""
        sensor_config = ven_client.get_sensor_config(config_name)
        if sensor_config is None:
            msg = f"Sensor config with name '{config_name}' not found."
            raise ValueError(msg)

        sensor_config.name = form_data.name
        sensor_config.targets = list(form_data.targets)
        sensor_config.utc_trigger_time = form_data.utc_trigger_time
        sensor_config.fetch_import_capacity_limits = form_data.fetch_import_capacity_limits
        sensor_config.fetch_export_capacity_limits = form_data.fetch_export_capacity_limits

        sensors = self._sensors.ensure_sensors_for_config(ven_client.asset, sensor_config)
        sensor_config.import_sensor = sensors.import_sensor
        sensor_config.export_sensor = sensors.export_sensor

        self._persist_payload(ven_client)
        db.session.flush()
        return sensor_config

    def delete_sensor_config(self, ven_client: VenClient, config_name: str) -> None:
        """Remove a sensor config and delete its associated sensors."""
        sensor_config = ven_client.get_sensor_config(config_name)
        if sensor_config is None:
            msg = f"Sensor config with name '{config_name}' not found."
            raise ValueError(msg)

        self._sensors.delete_sensors_for_config(ven_client.asset, config_name)
        ven_client.sensor_configs = [cfg for cfg in ven_client.sensor_configs if cfg.name != config_name]
        self._persist_payload(ven_client)
        db.session.flush()

    def persist_job_id(self, ven_client: VenClient, config_name: str, job_id: str | None) -> None:
        """Store a job ID on a specific sensor config."""
        sensor_config = ven_client.get_sensor_config(config_name)
        if sensor_config is None:
            return
        sensor_config.fetch_events_job_id = job_id
        self._persist_payload(ven_client)
        db.session.flush()


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
