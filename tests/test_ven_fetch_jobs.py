"""Integration tests for RQ cron scheduling of daily fetch-events jobs."""

from __future__ import annotations

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from rq.cron import CronJob

from flexmeasures_openadr3.utils.ven_clients import (
    VenClient,
    VenClientRepository,
    VenSensorConfig,
)
from flexmeasures_openadr3.utils.ven_jobs import (
    FETCH_EVENTS_QUEUE_NAME,
    VenFetchJobScheduler,
)


def test_schedule_registers_cron_job(
    app: Flask,
    fresh_db: SQLAlchemy,
    created_ven_client_with_schedule: VenClient,
    ven_client_repository: VenClientRepository,
    ven_fetch_job_scheduler: VenFetchJobScheduler,
) -> None:
    """Scheduling a polling schedule registers a CronJob on the scheduler."""
    reloaded = ven_client_repository.find_by_id(created_ven_client_with_schedule.id)
    assert reloaded is not None

    config = reloaded.get_sensor_config("daily-poll")
    assert config is not None

    assert FETCH_EVENTS_QUEUE_NAME in app.queues

    cron_job = ven_fetch_job_scheduler.schedule(reloaded, config)
    fresh_db.session.commit()

    assert cron_job is not None
    assert isinstance(cron_job, CronJob)
    assert cron_job.queue_name == FETCH_EVENTS_QUEUE_NAME
    assert cron_job.kwargs == {"ven_id": reloaded.id, "config_name": config.name}
    assert cron_job in ven_fetch_job_scheduler.get_cron_jobs()


def test_schedule_without_utc_trigger_time_returns_none(
    fresh_db: SQLAlchemy,
    created_ven_client: VenClient,
    ven_client_repository: VenClientRepository,
    ven_fetch_job_scheduler: VenFetchJobScheduler,
) -> None:
    """An empty UTC trigger time does not register a cron job."""
    config = VenSensorConfig(
        name="no-trigger",
        targets=["t1"],
        utc_trigger_time=None,
        fetch_import_capacity_limits=True,
    )
    created_ven_client.sensor_configs.append(config)
    ven_client_repository._persist_payload(created_ven_client)
    fresh_db.session.commit()

    cron_job = ven_fetch_job_scheduler.schedule(created_ven_client, config)
    assert cron_job is None


def test_delete_removes_cron_job(
    app: Flask,
    fresh_db: SQLAlchemy,  # NOQA: ARG001
    created_ven_client_with_schedule: VenClient,
    ven_client_repository: VenClientRepository,
    ven_fetch_job_scheduler: VenFetchJobScheduler,
) -> None:
    """Deleting a schedule unregisters its cron job from the scheduler."""
    reloaded = ven_client_repository.find_by_id(created_ven_client_with_schedule.id)
    assert reloaded is not None

    config = reloaded.get_sensor_config("daily-poll")
    assert config is not None
    assert FETCH_EVENTS_QUEUE_NAME in app.queues

    ven_fetch_job_scheduler.schedule(reloaded, config)

    ven_fetch_job_scheduler.delete(config)

    assert ven_fetch_job_scheduler.get_cron_jobs() == []


def test_delete_all_removes_all_cron_jobs(
    app: Flask,
    fresh_db: SQLAlchemy,  # NOQA: ARG001
    created_ven_client_with_schedule: VenClient,
    ven_client_repository: VenClientRepository,
    ven_fetch_job_scheduler: VenFetchJobScheduler,
) -> None:
    """delete_all unregisters cron jobs for all polling schedules on a VEN client."""
    reloaded = ven_client_repository.find_by_id(created_ven_client_with_schedule.id)
    assert reloaded is not None
    assert FETCH_EVENTS_QUEUE_NAME in app.queues

    for config in reloaded.sensor_configs:
        ven_fetch_job_scheduler.schedule(reloaded, config)

    ven_fetch_job_scheduler.delete_all(reloaded)

    assert ven_fetch_job_scheduler.get_cron_jobs() == []
