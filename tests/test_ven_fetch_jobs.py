"""Integration tests for RQ scheduling of daily fetch-events jobs."""

from __future__ import annotations

import pytest
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from rq.job import Job

from flexmeasures_openadr3.utils.ven_clients import (
    VenClient,
    VenClientRepository,
    VenSensorConfig,
)
from flexmeasures_openadr3.utils.ven_jobs import (
    FETCH_EVENTS_QUEUE_NAME,
    VenFetchJobScheduler,
)


def test_schedule_enqueues_job_in_forecasting_queue(
    app: Flask,
    fresh_db: SQLAlchemy,
    clean_forecasting_queue: None,
    created_ven_client_with_schedule: VenClient,
    ven_client_repository: VenClientRepository,
    ven_fetch_job_scheduler: VenFetchJobScheduler,
) -> None:
    """Scheduling a polling schedule enqueues an RQ job on the forecasting queue."""
    reloaded = ven_client_repository.find_by_id(created_ven_client_with_schedule.id)
    assert reloaded is not None

    config = reloaded.get_sensor_config("daily-poll")
    assert config is not None

    job = ven_fetch_job_scheduler.schedule(reloaded, config)
    fresh_db.session.commit()

    assert job is not None
    assert job.id is not None
    assert job.origin == FETCH_EVENTS_QUEUE_NAME

    queued_job = Job.fetch(
        job.id, connection=app.queues[FETCH_EVENTS_QUEUE_NAME].connection
    )
    assert queued_job.id == job.id
    assert queued_job.kwargs == {
        "ven_id": reloaded.id,
        "config_name": config.name,
    }


def test_schedule_persists_job_id_on_sensor_config(
    fresh_db: SQLAlchemy,
    clean_forecasting_queue: None,
    created_ven_client_with_schedule: VenClient,
    ven_client_repository: VenClientRepository,
    ven_fetch_job_scheduler: VenFetchJobScheduler,
) -> None:
    """After scheduling, the job id is stored on the polling schedule record."""
    reloaded = ven_client_repository.find_by_id(created_ven_client_with_schedule.id)
    assert reloaded is not None

    config = reloaded.get_sensor_config("daily-poll")
    assert config is not None

    job = ven_fetch_job_scheduler.schedule(reloaded, config)
    fresh_db.session.commit()

    assert job is not None

    persisted = ven_client_repository.find_by_id(reloaded.id)
    assert persisted is not None
    persisted_config = persisted.get_sensor_config("daily-poll")
    assert persisted_config is not None
    assert persisted_config.fetch_events_job_id == job.id


def test_schedule_without_utc_trigger_time_returns_none(
    fresh_db: SQLAlchemy,
    created_ven_client: VenClient,
    ven_client_repository: VenClientRepository,
    ven_fetch_job_scheduler: VenFetchJobScheduler,
) -> None:
    """An empty UTC trigger time does not enqueue a job."""
    config = VenSensorConfig(
        name="no-trigger",
        targets=["t1"],
        utc_trigger_time="",
        fetch_import_capacity_limits=True,
    )
    created_ven_client.sensor_configs.append(config)
    ven_client_repository._persist_payload(created_ven_client)
    fresh_db.session.commit()

    job = ven_fetch_job_scheduler.schedule(created_ven_client, config)
    assert job is None


def test_delete_removes_scheduled_job_from_queue(
    app: Flask,
    fresh_db: SQLAlchemy,
    clean_forecasting_queue: None,
    created_ven_client_with_schedule: VenClient,
    ven_client_repository: VenClientRepository,
    ven_fetch_job_scheduler: VenFetchJobScheduler,
) -> None:
    """Deleting a schedule removes its RQ job from Redis."""
    reloaded = ven_client_repository.find_by_id(created_ven_client_with_schedule.id)
    assert reloaded is not None

    config = reloaded.get_sensor_config("daily-poll")
    assert config is not None

    job = ven_fetch_job_scheduler.schedule(reloaded, config)
    fresh_db.session.commit()
    assert job is not None

    persisted = ven_client_repository.find_by_id(reloaded.id)
    assert persisted is not None
    config_with_job = persisted.get_sensor_config("daily-poll")
    assert config_with_job is not None

    ven_fetch_job_scheduler.delete(config_with_job)

    from rq.exceptions import NoSuchJobError

    with pytest.raises(NoSuchJobError):
        Job.fetch(job.id, connection=app.queues[FETCH_EVENTS_QUEUE_NAME].connection)


def test_delete_all_removes_jobs_for_every_schedule(
    app: Flask,
    fresh_db: SQLAlchemy,
    clean_forecasting_queue: None,
    created_ven_client_with_schedule: VenClient,
    ven_client_repository: VenClientRepository,
    ven_fetch_job_scheduler: VenFetchJobScheduler,
) -> None:
    """delete_all clears scheduled jobs for all polling schedules on a VEN client."""
    reloaded = ven_client_repository.find_by_id(created_ven_client_with_schedule.id)
    assert reloaded is not None

    for config in reloaded.sensor_configs:
        ven_fetch_job_scheduler.schedule(reloaded, config)
    fresh_db.session.commit()

    persisted = ven_client_repository.find_by_id(reloaded.id)
    assert persisted is not None
    job_ids = [cfg.fetch_events_job_id for cfg in persisted.sensor_configs]
    assert all(job_id is not None for job_id in job_ids)

    ven_fetch_job_scheduler.delete_all(persisted)

    from rq.exceptions import NoSuchJobError

    for job_id in job_ids:
        assert job_id is not None
        with pytest.raises(NoSuchJobError):
            Job.fetch(job_id, connection=app.queues[FETCH_EVENTS_QUEUE_NAME].connection)
