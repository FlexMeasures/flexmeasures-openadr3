"""Integration tests for VEN client and polling-schedule persistence."""

from __future__ import annotations

import pytest
from flask_sqlalchemy import SQLAlchemy

from flexmeasures_openadr3.models.storage import (
    VEN_CLIENT_ATTRIBUTE_KEY,
    VenClientAttributePayload,
)
from flexmeasures_openadr3.utils.encryption import SecretsEncryptor
from flexmeasures_openadr3.utils.sensor import (
    EXPORT_CAPACITY_LIMIT_SENSOR_NAME,
    IMPORT_CAPACITY_LIMIT_SENSOR_NAME,
    VenSensorRepository,
)
from flexmeasures_openadr3.utils.ven_clients import (
    VenClient,
    VenClientFormData,
    VenClientRepository,
    VenSensorConfigFormData,
)


def test_create_ven_client_persists_connection_attributes(
    fresh_db: SQLAlchemy,
    logged_in_prosumer: object,
    ven_client_repository: VenClientRepository,
    sample_ven_client_form_data: VenClientFormData,
) -> None:
    """Creating a VEN client stores connection settings on the generic asset."""
    ven_client = ven_client_repository.create(sample_ven_client_form_data)
    fresh_db.session.commit()

    reloaded = ven_client_repository.find_by_id(ven_client.id)
    assert reloaded is not None
    assert reloaded.name == sample_ven_client_form_data.name
    assert reloaded.vtn_url == sample_ven_client_form_data.vtn_url
    assert reloaded.oauth_token_url == sample_ven_client_form_data.oauth_token_url
    assert reloaded.scopes == sample_ven_client_form_data.scopes

    payload = VenClientAttributePayload.from_asset_attributes(reloaded.asset.attributes)
    assert payload.vtn_url == sample_ven_client_form_data.vtn_url
    assert list(payload.scopes) == sample_ven_client_form_data.scopes


def test_create_ven_client_encrypts_oauth_credentials_in_attributes(
    fresh_db: SQLAlchemy,
    logged_in_prosumer: object,
    ven_client_repository: VenClientRepository,
    sample_ven_client_form_data: VenClientFormData,
) -> None:
    """OAuth client id and secret are encrypted in the asset JSON attributes."""
    ven_client = ven_client_repository.create(sample_ven_client_form_data)
    fresh_db.session.commit()

    raw = ven_client.asset.attributes[VEN_CLIENT_ATTRIBUTE_KEY]
    assert raw["oauth_client_id"] != sample_ven_client_form_data.oauth_client_id
    assert raw["oauth_client_secret"] != sample_ven_client_form_data.oauth_client_secret

    encryptor = SecretsEncryptor.from_current_app()
    assert (
        encryptor.decrypt(raw["oauth_client_id"])
        == sample_ven_client_form_data.oauth_client_id
    )
    assert (
        encryptor.decrypt(raw["oauth_client_secret"])
        == sample_ven_client_form_data.oauth_client_secret
    )


def test_append_sensor_config_persists_polling_schedule(
    created_ven_client_with_schedule: VenClient,
    ven_client_repository: VenClientRepository,
    sample_sensor_config_form_data: VenSensorConfigFormData,
) -> None:
    """A polling schedule is stored on the VEN asset and reloads correctly."""
    reloaded = ven_client_repository.find_by_id(created_ven_client_with_schedule.id)
    assert reloaded is not None

    config = reloaded.get_sensor_config(sample_sensor_config_form_data.name)
    assert config is not None
    assert config.targets == sample_sensor_config_form_data.targets
    assert config.utc_trigger_time == sample_sensor_config_form_data.utc_trigger_time
    assert (
        config.fetch_import_capacity_limits
        == sample_sensor_config_form_data.fetch_import_capacity_limits
    )
    assert (
        config.fetch_export_capacity_limits
        == sample_sensor_config_form_data.fetch_export_capacity_limits
    )

    payload = VenClientAttributePayload.from_asset_attributes(reloaded.asset.attributes)
    assert len(payload.sensor_configs) == 1
    record = payload.sensor_configs[0]
    assert record.name == sample_sensor_config_form_data.name
    assert list(record.targets) == sample_sensor_config_form_data.targets
    assert record.utc_trigger_time == sample_sensor_config_form_data.utc_trigger_time


def test_append_sensor_config_creates_import_and_export_sensors(
    created_ven_client_with_schedule: VenClient,
    sample_sensor_config_form_data: VenSensorConfigFormData,
) -> None:
    """Import and export capacity-limit sensors are created on the VEN asset."""
    sensor_repo = VenSensorRepository()
    asset = created_ven_client_with_schedule.asset

    import_sensor = sensor_repo.find_sensor(
        sensor_repo.import_sensor_name(sample_sensor_config_form_data.name),
        asset,
    )
    export_sensor = sensor_repo.find_sensor(
        sensor_repo.export_sensor_name(sample_sensor_config_form_data.name),
        asset,
    )

    assert import_sensor is not None
    assert export_sensor is not None
    assert import_sensor.name.startswith(IMPORT_CAPACITY_LIMIT_SENSOR_NAME)
    assert export_sensor.name.startswith(EXPORT_CAPACITY_LIMIT_SENSOR_NAME)
    assert import_sensor.generic_asset_id == asset.id
    assert export_sensor.generic_asset_id == asset.id


def test_update_ven_client_persists_changed_fields(
    fresh_db: SQLAlchemy,
    created_ven_client: VenClient,
    ven_client_repository: VenClientRepository,
) -> None:
    """Updating a VEN client overwrites stored connection attributes."""
    updated_form = VenClientFormData(
        name="renamed-ven-client",
        vtn_url="https://vtn.example/v2",
        oauth_client_id="new-client-id",
        oauth_client_secret="new-client-secret",
        oauth_token_url="https://vtn.example/oauth/v2",
        scopes=["openid"],
    )

    ven_client_repository.update(created_ven_client, updated_form)
    fresh_db.session.commit()

    reloaded = ven_client_repository.find_by_name("renamed-ven-client")
    assert reloaded is not None
    assert reloaded.vtn_url == updated_form.vtn_url
    assert reloaded.scopes == updated_form.scopes


def test_delete_sensor_config_removes_schedule_from_attributes(
    fresh_db: SQLAlchemy,
    created_ven_client_with_schedule: VenClient,
    ven_client_repository: VenClientRepository,
    sample_sensor_config_form_data: VenSensorConfigFormData,
) -> None:
    """Deleting a polling schedule removes it from the VEN asset attributes."""
    ven_client_repository.delete_sensor_config(
        created_ven_client_with_schedule,
        sample_sensor_config_form_data.name,
    )
    fresh_db.session.commit()

    reloaded = ven_client_repository.find_by_id(created_ven_client_with_schedule.id)
    assert reloaded is not None
    assert reloaded.get_sensor_config(sample_sensor_config_form_data.name) is None

    payload = VenClientAttributePayload.from_asset_attributes(reloaded.asset.attributes)
    assert payload.sensor_configs == ()


def test_duplicate_ven_client_name_raises(
    created_ven_client: VenClient,
    logged_in_prosumer: object,
    ven_client_repository: VenClientRepository,
    sample_ven_client_form_data: VenClientFormData,
) -> None:
    """Two VEN clients cannot share the same asset name."""
    with pytest.raises(ValueError, match="already exists"):
        ven_client_repository.create(sample_ven_client_form_data)
