"""End-to-end test: VTN capacity events -> FlexMeasures fetch-events automation -> sensor beliefs."""

from __future__ import annotations

import pytest
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flexmeasures.data.models.automations import Automation
from flexmeasures.data.models.time_series import TimedBelief
from flexmeasures.data.services.automations import run_automation
from flexmeasures.utils.job_utils import work_on_rq
from openadr3_client.oadr310.models.event.event import ExistingEvent
from openadr3_client.oadr310.models.event.event_payload import EventPayloadType
from rq.job import Job
from sqlalchemy import select

from flexmeasures_openadr3.utils.ven_client_forms import VenClientFormData, VenSensorConfigFormData
from flexmeasures_openadr3.utils.ven_clients import VenClientRepository
from tests.test_container.integration_types import IntegrationTestVTNClient


def _build_ven_client_form_data(
    ven_client_fixture: IntegrationTestVTNClient,
) -> VenClientFormData:
    oauth = ven_client_fixture.oauth_configuration
    return VenClientFormData(
        name="e2e-ven-client",
        vtn_url=ven_client_fixture.vtn_configuration.vtn_base_url,
        oauth_client_id=oauth.client_id,
        oauth_client_secret=oauth.client_secret,
        oauth_token_url=oauth.token_url,
        scopes=[],  # use Keycloak default client scopes (avoids oauthlib scope-mismatch warnings)
    )


def _run_automation_now(app: Flask, automation: Automation) -> Job:
    """Queue a run of the automation, as *Run now* does, and work it off the ingestion queue right away."""
    result = run_automation(automation)
    assert result is not None
    job = Job.fetch(result["job_id"], connection=app.redis_connection)
    work_on_rq(app.queues["ingestion"], job=job)
    job.refresh()
    assert job.is_finished, job.exc_info
    return job


def _beliefs_for_sensor(db: SQLAlchemy, sensor_id: int, source_id: int) -> list[TimedBelief]:
    return list(db.session.scalars(select(TimedBelief).filter_by(sensor_id=sensor_id, source_id=source_id).order_by(TimedBelief.event_start)).all())


@pytest.mark.usefixtures("clean_queues")
def test_ven_fetch_events_e2e_stores_openadr_capacity_limits(
    app: Flask,
    logged_in_prosumer: object,  # noqa: ARG001
    fresh_db: SQLAlchemy,
    ven_client: IntegrationTestVTNClient,
    vtn_capacity_limit_event_seed: ExistingEvent,
    ven_client_repository: VenClientRepository,
) -> None:
    """
    Full flow: create VEN + polling schedule, run fetch job, verify sensor beliefs.

    OpenADR setup (session fixture): BL client seeds a 24-hour program/event on the VTN
    with 96 fifteen-minute intervals, each carrying IMPORT/EXPORT_CAPACITY_LIMIT
    payloads. The event starts one hour after the session fixture is created.

    Running the polling schedule's automation queues a job on the ingestion queue, which
    stores one TimedBelief per interval at the interval start on dedicated
    import/export sensors (15-minute sensor resolution). Assertions verify belief count,
    that each belief falls within its seed interval, payload values match, and that
    event_start times run from the first interval start through the last interval start.
    """
    seed = vtn_capacity_limit_event_seed

    # --- FlexMeasures: VEN client and polling schedule ---
    ven = ven_client_repository.create(_build_ven_client_form_data(ven_client))
    schedule_form = VenSensorConfigFormData(
        name="e2e-poll",
        targets=[],
        cronstr="0 0 * * *",
        timezone="UTC",
        fetch_import_capacity_limits=True,
        fetch_export_capacity_limits=True,
    )
    ven_client_repository.append_sensor_config(ven, schedule_form)
    fresh_db.session.commit()

    updated_ven = ven_client_repository.find_by_id(ven.id)
    assert updated_ven is not None
    config = updated_ven.get_sensor_config("e2e-poll")
    assert config is not None
    assert config.import_sensor is not None
    assert config.export_sensor is not None

    # --- Run the automation now (do not wait for its cron schedule) ---
    automation = config.automation
    job = _run_automation_now(app, automation)

    # --- Assert beliefs on import/export sensors ---
    source_id = automation.generator_id
    import_beliefs = _beliefs_for_sensor(fresh_db, config.import_sensor.id, source_id)
    export_beliefs = _beliefs_for_sensor(fresh_db, config.export_sensor.id, source_id)

    expected_count = len(seed.intervals or ())
    assert sorted(job.return_value(), key=lambda saved: saved["sensor_id"]) == sorted(
        [
            {"sensor_id": config.import_sensor.id, "n_rows": expected_count},
            {"sensor_id": config.export_sensor.id, "n_rows": expected_count},
        ],
        key=lambda saved: saved["sensor_id"],
    )
    assert len(import_beliefs) == expected_count
    assert len(export_beliefs) == expected_count

    for i, belief in enumerate(import_beliefs):
        interval = seed.intervals[i] if seed.intervals is not None else None
        assert interval is not None and interval.interval_period is not None
        assert interval.interval_period.start <= belief.event_start < interval.interval_period.start + interval.interval_period.duration

        import_capacity_payload = next(
            (payload for payload in interval.payloads if payload.type == EventPayloadType.IMPORT_CAPACITY_LIMIT),
            None,
        )
        assert import_capacity_payload is not None
        assert belief.event_value == pytest.approx(import_capacity_payload.values[0])

    for i, belief in enumerate(export_beliefs):
        interval = seed.intervals[i] if seed.intervals is not None else None
        assert interval is not None and interval.interval_period is not None
        assert interval.interval_period.start <= belief.event_start < interval.interval_period.start + interval.interval_period.duration

        export_capacity_payload = next(
            (payload for payload in interval.payloads if payload.type == EventPayloadType.EXPORT_CAPACITY_LIMIT),
            None,
        )
        assert export_capacity_payload is not None
        assert belief.event_value == pytest.approx(export_capacity_payload.values[0])

    # Interval spans ~24 hours into the future from fixture creation.
    min_start = min(b.event_start for b in import_beliefs)
    max_start = max(b.event_start for b in import_beliefs)

    initial_interval = seed.intervals[0] if seed.intervals is not None else None
    assert initial_interval is not None and initial_interval.interval_period is not None
    last_interval = seed.intervals[-1] if seed.intervals is not None else None
    assert last_interval is not None and last_interval.interval_period is not None

    assert min_start == initial_interval.interval_period.start
    assert max_start == last_interval.interval_period.start

    # --- Fetching the same events again records nothing new ---
    rerun = _run_automation_now(app, automation)
    assert all(saved["n_rows"] == 0 for saved in rerun.return_value())
    assert len(_beliefs_for_sensor(fresh_db, config.import_sensor.id, source_id)) == expected_count
