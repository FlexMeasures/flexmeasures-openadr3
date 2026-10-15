"""Integration tests for the OpenADR 3 fetch-events automation type."""

from __future__ import annotations

import pytest
from flask import Flask
from flexmeasures.data.automations import AutomationPayloadValidationError
from flexmeasures.data.services.automations import create_automation, run_automation
from rq.job import Job

from flexmeasures_openadr3.automations import (
    FETCH_EVENTS_AUTOMATION,
    FETCH_EVENTS_AUTOMATION_TYPE,
    OpenADREventFetcher,
)
from flexmeasures_openadr3.models.forms import VenSensorConfigPostValues
from flexmeasures_openadr3.utils.ven_client_forms import validate_ven_sensor_config_form
from flexmeasures_openadr3.utils.ven_clients import VenClient


def test_plugin_registers_fetch_events_automation_type(app: Flask) -> None:
    """Loading the plugin registers its automation type and data generator with FlexMeasures."""
    assert app.automation_handlers[FETCH_EVENTS_AUTOMATION_TYPE] is FETCH_EVENTS_AUTOMATION
    assert app.data_generators["ingestor"][OpenADREventFetcher.__name__] is OpenADREventFetcher


@pytest.mark.usefixtures("clean_queues")
def test_running_polling_schedule_queues_ingestion_job(
    app: Flask,
    created_ven_client_with_schedule: VenClient,
) -> None:
    """Running a polling schedule's automation queues one job on the ingestion queue."""
    automation = created_ven_client_with_schedule.sensor_configs[0].automation

    result = run_automation(automation)

    assert result is not None
    assert result["n_jobs"] == 1
    job = Job.fetch(result["job_id"], connection=app.redis_connection)
    assert job.origin == "ingestion"
    assert job.meta["trigger"] == {"origin": "automation", "automation_id": automation.id}
    assert job.kwargs["data_source_id"] == automation.generator_id


def test_fetch_events_automation_requires_a_sensor(created_ven_client: VenClient) -> None:
    """An automation that fetches neither limit type is refused."""
    with pytest.raises(AutomationPayloadValidationError, match="import-sensor"):
        create_automation(
            asset=created_ven_client.asset,
            name="no-sensors",
            cronstr="0 * * * *",
            automation_type=FETCH_EVENTS_AUTOMATION_TYPE,
            config={"targets": ["target-a"]},
            origin="CLI",
        )


@pytest.mark.parametrize(
    ("cronstr", "timezone", "error_field"),
    [
        ("30 2 * *", "UTC", "cronstr"),
        ("61 2 * * *", "UTC", "cronstr"),
        ("30 2 * * *", "Mars/Olympus_Mons", "timezone"),
    ],
)
def test_sensor_config_form_validates_schedule(
    app: Flask,  # noqa: ARG001
    cronstr: str,
    timezone: str,
    error_field: str,
) -> None:
    """The polling schedule form accepts the cron strings and timezones FlexMeasures accepts for automations."""
    validation = validate_ven_sensor_config_form(
        VenSensorConfigPostValues(
            name="poll",
            targets="",
            cronstr=cronstr,
            timezone=timezone,
            fetch_import_capacity_limits=True,
            fetch_export_capacity_limits=False,
        )
    )

    assert not validation.is_valid
    assert getattr(validation.errors, error_field)
