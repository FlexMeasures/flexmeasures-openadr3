from __future__ import annotations

from dataclasses import dataclass, field

from flask import current_app
from openadr3_client.oadr310._ven.client import VirtualEndNodeClient
from openadr3_client.ven.http_factory import VirtualEndNodeHttpClientFactory
from openadr3_client.version import OADRVersion
from pydantic import BaseModel, Field, ValidationError, field_validator
from pydantic_core.core_schema import ValidationInfo

from flexmeasures.data import db
from flexmeasures.data.models.generic_assets import GenericAsset
from flexmeasures.data.models.time_series import Sensor

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
from flexmeasures_openadr3.utils.encryption import SecretsEncryptor
from flexmeasures_openadr3.utils.sensor import VenAssetRepository, VenSensorRepository


def _parse_csv_values(raw_value: str) -> list[str]:
    """Parse a comma-separated string into a list of trimmed, non-empty values."""
    return [item.strip() for item in raw_value.split(",") if item.strip()]


# ---------------------------------------------------------------------------
# Domain models
# ---------------------------------------------------------------------------


@dataclass
class VenSensorConfig:
    """A polling schedule that defines when and what to fetch from a VTN.

    Each config maps to zero, one, or two FlexMeasures sensors (import/export).
    """

    name: str
    targets: list[str] = field(default_factory=list)
    utc_trigger_time: str = ""
    fetch_import_capacity_limits: bool = False
    fetch_export_capacity_limits: bool = False
    fetch_events_job_id: str | None = None
    import_sensor: Sensor | None = field(default=None, repr=False)
    export_sensor: Sensor | None = field(default=None, repr=False)

    def to_record(self) -> VenSensorConfigRecord:
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
    """Domain model representing an OpenADR VEN client.

    Wraps a FlexMeasures GenericAsset and provides typed access to VEN-specific
    attributes stored in the asset's JSON attributes column.
    """

    asset: GenericAsset = field(repr=False)
    vtn_url: str = ""
    oauth_client_id: str = ""
    oauth_client_secret: str = field(default="", repr=False)
    oauth_token_url: str = ""
    scopes: list[str] = field(default_factory=list)
    sensor_configs: list[VenSensorConfig] = field(default_factory=list)

    @property
    def id(self) -> int:
        return int(self.asset.id)

    @property
    def name(self) -> str:
        return str(self.asset.name)

    @name.setter
    def name(self, value: str) -> None:
        self.asset.name = value

    def get_sensor_config(self, config_name: str) -> VenSensorConfig | None:
        return next(
            (cfg for cfg in self.sensor_configs if cfg.name == config_name), None
        )

    def create_http_client(self) -> VirtualEndNodeClient:
        secrets_encryptor = SecretsEncryptor.from_current_app()
        decrypted_oauth_client_id = secrets_encryptor.decrypt(self.oauth_client_id)
        decrypted_oauth_client_secret = secrets_encryptor.decrypt(
            self.oauth_client_secret
        )

        return VirtualEndNodeHttpClientFactory.create_http_ven_client(
            vtn_base_url=self.vtn_url,
            client_id=decrypted_oauth_client_id,
            client_secret=decrypted_oauth_client_secret,
            token_url=self.oauth_token_url,
            scopes=self.scopes,
            allow_insecure_http=current_app.config.get(
                "ALLOW_INSECURE_HTTP_VTN", "false"
            )
            == "true",
            version=OADRVersion.OADR_310,
        )  # type: ignore[return-value]

    def to_attribute_payload(self) -> VenClientAttributePayload:
        return VenClientAttributePayload(
            vtn_url=self.vtn_url,
            oauth_client_id=self.oauth_client_id,
            oauth_client_secret=self.oauth_client_secret,
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
        self._secrets_encryptor = SecretsEncryptor.from_current_app()

    def _build_sensor_configs(
        self, asset: GenericAsset, records: tuple[VenSensorConfigRecord, ...]
    ) -> list[VenSensorConfig]:
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

        return VenClient(
            asset=asset,
            vtn_url=payload.vtn_url,
            oauth_client_id=payload.oauth_client_id,
            oauth_client_secret=payload.oauth_client_secret,
            oauth_token_url=payload.oauth_token_url,
            scopes=list(payload.scopes),
            sensor_configs=self._build_sensor_configs(asset, payload.sensor_configs),
        )

    def _persist_payload(
        self, ven_client: VenClient, *, encrypt_oauth_credentials: bool = False
    ) -> None:
        """Write VEN-specific data back to the asset's attributes column.

        OAuth client id and secret are stored encrypted. Pass *encrypt_oauth_credentials*
        True when *ven_client* carries plaintext from a form; pass False when those
        fields are unchanged ciphertext loaded from the database (e.g. sensor config
        or job id updates).
        """
        attributes = dict(ven_client.asset.attributes or {})
        if encrypt_oauth_credentials:
            stored_oauth_client_id = self._secrets_encryptor.encrypt(
                ven_client.oauth_client_id
            )
            stored_oauth_client_secret = self._secrets_encryptor.encrypt(
                ven_client.oauth_client_secret
            )
        else:
            stored_oauth_client_id = ven_client.oauth_client_id
            stored_oauth_client_secret = ven_client.oauth_client_secret

        payload = ven_client.to_attribute_payload()
        attributes[VEN_CLIENT_ATTRIBUTE_KEY] = VenClientAttributePayload(
            vtn_url=payload.vtn_url,
            oauth_client_id=stored_oauth_client_id,
            oauth_client_secret=stored_oauth_client_secret,
            oauth_token_url=payload.oauth_token_url,
            scopes=payload.scopes,
            sensor_configs=payload.sensor_configs,
        ).to_json()
        ven_client.asset.attributes = attributes

    def list_ven_clients(self) -> list[VenClient]:
        return [self._build_ven_client(asset) for asset in self._assets.list_all()]

    def find_by_id(self, ven_id: int) -> VenClient | None:
        asset = self._assets.find_by_id(ven_id)
        return self._build_ven_client(asset) if asset else None

    def find_by_name(self, ven_name: str) -> VenClient | None:
        asset = self._assets.find_by_name(ven_name)
        return self._build_ven_client(asset) if asset else None

    def create(self, form_data: VenClientFormData) -> VenClient:
        asset = self._assets.create_asset(form_data.name)
        ven_client = VenClient(
            asset=asset,
            vtn_url=form_data.vtn_url,
            oauth_client_id=form_data.oauth_client_id,
            oauth_client_secret=form_data.oauth_client_secret,
            oauth_token_url=form_data.oauth_token_url,
            scopes=form_data.scopes,
        )
        self._persist_payload(ven_client, encrypt_oauth_credentials=True)
        db.session.flush()
        return self._build_ven_client(asset)

    def update(self, ven_client: VenClient, form_data: VenClientFormData) -> VenClient:
        ven_client.name = form_data.name
        ven_client.vtn_url = form_data.vtn_url
        ven_client.oauth_client_id = form_data.oauth_client_id
        ven_client.oauth_client_secret = form_data.oauth_client_secret
        ven_client.oauth_token_url = form_data.oauth_token_url
        ven_client.scopes = form_data.scopes
        self._persist_payload(ven_client, encrypt_oauth_credentials=True)
        db.session.flush()
        return ven_client

    def delete(self, ven_client: VenClient) -> None:
        db.session.delete(ven_client.asset)

    def append_sensor_config(
        self, ven_client: VenClient, form_data: VenSensorConfigFormData
    ) -> VenSensorConfig:
        """Append a new sensor config and ensure its sensors exist."""
        if ven_client.get_sensor_config(form_data.name) is not None:
            raise ValueError(
                f"Sensor config with name '{form_data.name}' already exists."
            )

        sensor_config = VenSensorConfig(
            name=form_data.name,
            targets=list(form_data.targets),
            utc_trigger_time=form_data.utc_trigger_time,
            fetch_import_capacity_limits=form_data.fetch_import_capacity_limits,
            fetch_export_capacity_limits=form_data.fetch_export_capacity_limits,
        )

        sensors = self._sensors.ensure_sensors_for_config(
            ven_client.asset, sensor_config
        )
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
            raise ValueError(f"Sensor config with name '{config_name}' not found.")

        sensor_config.name = form_data.name
        sensor_config.targets = list(form_data.targets)
        sensor_config.utc_trigger_time = form_data.utc_trigger_time
        sensor_config.fetch_import_capacity_limits = (
            form_data.fetch_import_capacity_limits
        )
        sensor_config.fetch_export_capacity_limits = (
            form_data.fetch_export_capacity_limits
        )

        sensors = self._sensors.ensure_sensors_for_config(
            ven_client.asset, sensor_config
        )
        sensor_config.import_sensor = sensors.import_sensor
        sensor_config.export_sensor = sensors.export_sensor

        self._persist_payload(ven_client)
        db.session.flush()
        return sensor_config

    def delete_sensor_config(self, ven_client: VenClient, config_name: str) -> None:
        """Remove a sensor config and delete its associated sensors."""
        sensor_config = ven_client.get_sensor_config(config_name)
        if sensor_config is None:
            raise ValueError(f"Sensor config with name '{config_name}' not found.")

        self._sensors.delete_sensors_for_config(ven_client.asset, config_name)
        ven_client.sensor_configs = [
            cfg for cfg in ven_client.sensor_configs if cfg.name != config_name
        ]
        self._persist_payload(ven_client)
        db.session.flush()

    def persist_job_id(
        self, ven_client: VenClient, config_name: str, job_id: str | None
    ) -> None:
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
    def _parse_csv_string_fields(
        cls, value: str | list[str] | tuple[str, ...] | None
    ) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return _parse_csv_values(value)
        if isinstance(value, tuple):
            return [item.strip() for item in value if item.strip()]
        return [item.strip() for item in value if item.strip()]

    @classmethod
    def from_form_values(cls, field_values: VenClientFormValues) -> VenClientFormData:
        return cls.model_validate(field_values.to_validation_input())


class VenSensorConfigFormData(BaseModel):
    """Validated polling schedule form data."""

    name: str
    targets: list[str] = Field(default_factory=list)
    utc_trigger_time: str
    fetch_import_capacity_limits: bool = False
    fetch_export_capacity_limits: bool = False

    @field_validator("targets", mode="before")
    @classmethod
    def _parse_targets_csv(
        cls, value: str | list[str] | tuple[str, ...] | None
    ) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return _parse_csv_values(value)
        if isinstance(value, tuple):
            return [item.strip() for item in value if item.strip()]
        return [item.strip() for item in value if item.strip()]

    @field_validator("utc_trigger_time")
    @classmethod
    def _validate_utc_trigger_time(cls, value: str, info: ValidationInfo) -> str:
        parsed_value = value.strip()
        time_parts = parsed_value.split(":")
        if len(time_parts) != 3 or not all(part.isdigit() for part in time_parts):
            raise ValueError("Must be in UTC format HH:MM:SS.")
        hour, minute, second = (int(part) for part in time_parts)
        if hour > 23 or minute > 59 or second > 59:
            raise ValueError("Must be in UTC format HH:MM:SS.")
        return f"{hour:02d}:{minute:02d}:{second:02d}"

    @classmethod
    def from_post_values(
        cls, post_values: VenSensorConfigPostValues
    ) -> VenSensorConfigFormData:
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
        oauth_client_id=ven_client.oauth_client_id,
        oauth_client_secret=ven_client.oauth_client_secret,
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
        fetch_import_capacity_limits=(
            "on" if source.fetch_import_capacity_limits else ""
        ),
        fetch_export_capacity_limits=(
            "on" if source.fetch_export_capacity_limits else ""
        ),
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
        assert ven_client_data is not None
        existing = ven_client_repository.find_by_name(ven_client_data.name)
        if existing and ven_client_data.name != current_name:
            errors.add("name", "A VEN client with this name already exists.")

    return FormValidationResult(
        data=ven_client_data if not errors else None, errors=errors
    )


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
        assert config is not None
        if not (
            config.fetch_import_capacity_limits or config.fetch_export_capacity_limits
        ):
            errors.add(
                "limits", "Select at least one limit type (import and/or export)."
            )

        if existing_configs is not None and config.name != current_name:
            if any(cfg.name == config.name for cfg in existing_configs):
                errors.add("name", "A sensor config with this name already exists.")

    return FormValidationResult(data=config if not errors else None, errors=errors)
