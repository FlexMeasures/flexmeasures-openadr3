"""Integration tests for VEN client and polling-schedule persistence."""

from __future__ import annotations

import pytest
from flask_sqlalchemy import SQLAlchemy
from flexmeasures.data.models.automations import Automation
from flexmeasures.data.models.time_series import Sensor
from flexmeasures.data.models.user import User
from flexmeasures.utils.secrets_utils import get_secret

from flexmeasures_openadr3.automations import FETCH_EVENTS_AUTOMATION_TYPE, OPENADR_EVENT_SOURCE_NAME, OpenADREventFetcher
from flexmeasures_openadr3.models.storage import (
    VEN_CLIENT_ATTRIBUTE_KEY,
    VenClientAttributePayload,
)
from flexmeasures_openadr3.utils.sensor import (
    EXPORT_CAPACITY_LIMIT_SENSOR_NAME,
    IMPORT_CAPACITY_LIMIT_SENSOR_NAME,
    VenSensorRepository,
)
from flexmeasures_openadr3.utils.ven_client_forms import VenClientFormData, VenSensorConfigFormData
from flexmeasures_openadr3.utils.ven_clients import VenClient, VenClientRepository


def test_create_ven_client_persists_connection_attributes(
    fresh_db: SQLAlchemy,
    logged_in_prosumer: object,  # noqa: ARG001
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


def test_create_ven_client_stores_oauth_credentials_as_platform_secrets(
    fresh_db: SQLAlchemy,
    logged_in_prosumer: object,  # noqa: ARG001
    ven_client_repository: VenClientRepository,
    sample_ven_client_form_data: VenClientFormData,
) -> None:
    """OAuth client id and secret are stored via FlexMeasures platform secrets, not in attributes."""
    ven_client = ven_client_repository.create(sample_ven_client_form_data)
    fresh_db.session.commit()

    raw = ven_client.asset.attributes[VEN_CLIENT_ATTRIBUTE_KEY]
    assert "oauth_client_id" not in raw
    assert "oauth_client_secret" not in raw

    assert get_secret(ven_client.asset.secrets, "ven_client.oauth_client_id") == sample_ven_client_form_data.oauth_client_id
    assert get_secret(ven_client.asset.secrets, "ven_client.oauth_client_secret") == sample_ven_client_form_data.oauth_client_secret
    assert ven_client.oauth_client_id_is_set
    assert ven_client.oauth_client_secret_is_set


def test_append_sensor_config_persists_polling_schedule_as_automation(
    created_ven_client_with_schedule: VenClient,
    logged_in_prosumer: User,
    ven_client_repository: VenClientRepository,
    sample_sensor_config_form_data: VenSensorConfigFormData,
) -> None:
    """A polling schedule is stored as an automation on the VEN asset, and reloads correctly."""
    reloaded = ven_client_repository.find_by_id(created_ven_client_with_schedule.id)
    assert reloaded is not None

    config = reloaded.get_sensor_config(sample_sensor_config_form_data.name)
    assert config is not None
    assert config.targets == sample_sensor_config_form_data.targets
    assert config.cronstr == sample_sensor_config_form_data.cronstr
    assert config.timezone == sample_sensor_config_form_data.timezone
    assert config.active
    assert config.fetch_import_capacity_limits == sample_sensor_config_form_data.fetch_import_capacity_limits
    assert config.fetch_export_capacity_limits == sample_sensor_config_form_data.fetch_export_capacity_limits

    automation = config.automation
    assert automation.asset_id == reloaded.id
    assert automation.type == FETCH_EVENTS_AUTOMATION_TYPE
    assert automation.name == sample_sensor_config_form_data.name
    assert automation.execution_user_id == logged_in_prosumer.id
    assert automation.generator.name == OPENADR_EVENT_SOURCE_NAME
    assert automation.generator.model == OpenADREventFetcher.__name__
    assert automation.generator.account_id == reloaded.asset.account_id
    assert automation.generator.attributes["data_generator"]["config"] == {
        "targets": sample_sensor_config_form_data.targets,
        "import-sensor": config.import_sensor.id,
        "export-sensor": config.export_sensor.id,
    }

    payload = reloaded.asset.attributes[VEN_CLIENT_ATTRIBUTE_KEY]
    assert "sensor_configs" not in payload


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
    assert get_secret(reloaded.asset.secrets, "ven_client.oauth_client_id") == "new-client-id"
    assert get_secret(reloaded.asset.secrets, "ven_client.oauth_client_secret") == "new-client-secret"


def test_update_ven_client_with_blank_credentials_keeps_existing_secrets(
    fresh_db: SQLAlchemy,
    created_ven_client: VenClient,
    ven_client_repository: VenClientRepository,
) -> None:
    """Submitting blank OAuth credentials on update leaves the stored secrets unchanged."""
    updated_form = VenClientFormData(
        name=created_ven_client.name,
        vtn_url="https://vtn.example/v2",
        oauth_client_id="",
        oauth_client_secret="",
        oauth_token_url="https://vtn.example/oauth/v2",
        scopes=["openid"],
    )

    ven_client_repository.update(created_ven_client, updated_form)
    fresh_db.session.commit()

    reloaded = ven_client_repository.find_by_name(created_ven_client.name)
    assert reloaded is not None
    assert reloaded.vtn_url == updated_form.vtn_url
    assert get_secret(reloaded.asset.secrets, "ven_client.oauth_client_id") == "test-client-id"
    assert get_secret(reloaded.asset.secrets, "ven_client.oauth_client_secret") == "test-client-secret"


def test_update_sensor_config_updates_automation_and_keeps_sensors(
    fresh_db: SQLAlchemy,
    created_ven_client_with_schedule: VenClient,
    ven_client_repository: VenClientRepository,
    sample_sensor_config_form_data: VenSensorConfigFormData,
) -> None:
    """Editing a polling schedule updates its automation in place, and a renamed schedule keeps recording on its sensors."""
    original = created_ven_client_with_schedule.get_sensor_config(sample_sensor_config_form_data.name)
    assert original is not None
    automation_id = original.automation.id
    original_source_id = original.automation.generator_id
    import_sensor_id = original.import_sensor.id

    updated_form = sample_sensor_config_form_data.model_copy(
        update={
            "name": "renamed-poll",
            "cronstr": "*/15 * * * *",
            "timezone": "UTC",
            "targets": ["target-c"],
            "fetch_export_capacity_limits": False,
        }
    )
    ven_client_repository.update_sensor_config(created_ven_client_with_schedule, sample_sensor_config_form_data.name, updated_form)
    fresh_db.session.commit()

    reloaded = ven_client_repository.find_by_id(created_ven_client_with_schedule.id)
    assert reloaded is not None
    assert reloaded.get_sensor_config(sample_sensor_config_form_data.name) is None
    config = reloaded.get_sensor_config("renamed-poll")
    assert config is not None
    assert config.automation.id == automation_id
    assert config.cronstr == "*/15 * * * *"
    assert config.timezone == "UTC"
    assert config.targets == ["target-c"]
    assert config.import_sensor is not None
    assert config.import_sensor.id == import_sensor_id
    assert config.export_sensor is None
    assert config.automation.generator_id != original_source_id


def test_update_sensor_config_without_fetch_changes_keeps_data_source(
    fresh_db: SQLAlchemy,
    created_ven_client_with_schedule: VenClient,
    ven_client_repository: VenClientRepository,
    sample_sensor_config_form_data: VenSensorConfigFormData,
) -> None:
    """Changing only when a polling schedule runs keeps recording under the same data source."""
    original = created_ven_client_with_schedule.get_sensor_config(sample_sensor_config_form_data.name)
    assert original is not None
    original_source_id = original.automation.generator_id

    updated_form = sample_sensor_config_form_data.model_copy(update={"cronstr": "0 6 * * 1-5"})
    ven_client_repository.update_sensor_config(created_ven_client_with_schedule, sample_sensor_config_form_data.name, updated_form)
    fresh_db.session.commit()

    reloaded = ven_client_repository.find_by_id(created_ven_client_with_schedule.id)
    assert reloaded is not None
    config = reloaded.get_sensor_config(sample_sensor_config_form_data.name)
    assert config is not None
    assert config.cronstr == "0 6 * * 1-5"
    assert config.automation.generator_id == original_source_id


def test_delete_sensor_config_removes_automation_and_sensors(
    fresh_db: SQLAlchemy,
    created_ven_client_with_schedule: VenClient,
    ven_client_repository: VenClientRepository,
    sample_sensor_config_form_data: VenSensorConfigFormData,
) -> None:
    """Deleting a polling schedule removes its automation and its sensors."""
    config = created_ven_client_with_schedule.get_sensor_config(sample_sensor_config_form_data.name)
    assert config is not None
    automation_id = config.automation.id
    sensor_ids = [config.import_sensor.id, config.export_sensor.id]

    ven_client_repository.delete_sensor_config(
        created_ven_client_with_schedule,
        sample_sensor_config_form_data.name,
    )
    fresh_db.session.commit()

    reloaded = ven_client_repository.find_by_id(created_ven_client_with_schedule.id)
    assert reloaded is not None
    assert reloaded.get_sensor_config(sample_sensor_config_form_data.name) is None
    assert fresh_db.session.get(Automation, automation_id) is None
    assert all(fresh_db.session.get(Sensor, sensor_id) is None for sensor_id in sensor_ids)


def test_delete_ven_client_removes_its_automations(
    fresh_db: SQLAlchemy,
    created_ven_client_with_schedule: VenClient,
    ven_client_repository: VenClientRepository,
) -> None:
    """Deleting a VEN client takes its polling schedules' automations along."""
    automation_ids = [cfg.automation.id for cfg in created_ven_client_with_schedule.sensor_configs]
    assert automation_ids

    ven_client_repository.delete(created_ven_client_with_schedule)
    fresh_db.session.commit()

    assert all(fresh_db.session.get(Automation, automation_id) is None for automation_id in automation_ids)


def test_duplicate_ven_client_name_raises(
    created_ven_client: VenClient,  # noqa: ARG001
    ven_client_repository: VenClientRepository,
    sample_ven_client_form_data: VenClientFormData,
) -> None:
    """Two VEN clients cannot share the same asset name."""
    with pytest.raises(ValueError, match="already exists"):
        ven_client_repository.create(sample_ven_client_form_data)
