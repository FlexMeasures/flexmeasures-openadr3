from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING, cast

from flask import current_app
from flexmeasures.data import db  # type: ignore[import-untyped]
from flexmeasures.data.models.time_series import Sensor, TimedBelief
from flexmeasures.data.services.data_sources import get_or_create_source
from openadr3_client.oadr310._vtn.interfaces.filters import TargetFilter
from openadr3_client.oadr310.models.event.event import ExistingEvent, Interval
from openadr3_client.oadr310.models.event.event_payload import EventPayloadType
from rq import Queue
from rq.exceptions import NoSuchJobError
from rq.job import Job

from flexmeasures_openadr3.models.jobs import EventActivePeriod
from flexmeasures_openadr3.utils.ven_clients import (
    VenClient,
    VenClientRepository,
    VenSensorConfig,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

OPENADR_EVENT_SOURCE_NAME = "OpenADR 3 VTN"
OPENADR_EVENT_SOURCE_TYPE = "gateway"
FETCH_EVENTS_QUEUE_NAME = "ingestion"
SUPPORTED_DR_PAYLOAD_TYPES = (
    EventPayloadType.IMPORT_CAPACITY_LIMIT,
    EventPayloadType.EXPORT_CAPACITY_LIMIT,
)


class OverlappingDemandResponseEventsError(ValueError):
    """Raised when multiple OpenADR events are active for the same period."""


@dataclass(frozen=True, slots=True)
class PayloadTypeSensorMapping:
    """Maps supported OpenADR payload types to FlexMeasures sensors."""

    import_sensor: Sensor | None = None
    export_sensor: Sensor | None = None

    @classmethod
    def from_sensor_config(cls, sensor_config: VenSensorConfig) -> PayloadTypeSensorMapping:
        """Build a mapping from a polling schedule's enabled limit types."""
        import_sensor = sensor_config.import_sensor if sensor_config.fetch_import_capacity_limits else None
        export_sensor = sensor_config.export_sensor if sensor_config.fetch_export_capacity_limits else None
        return cls(import_sensor=import_sensor, export_sensor=export_sensor)

    def get_sensor_for(self, payload_type: EventPayloadType) -> Sensor | None:
        """Return the FlexMeasures sensor for an OpenADR payload type."""
        if payload_type is EventPayloadType.IMPORT_CAPACITY_LIMIT:
            return self.import_sensor
        if payload_type is EventPayloadType.EXPORT_CAPACITY_LIMIT:
            return self.export_sensor
        return None

    def __bool__(self) -> bool:
        """Return whether at least one sensor is configured."""
        return self.import_sensor is not None or self.export_sensor is not None


def _build_job_id(ven_id: int, config_name: str, run_at: datetime) -> str:
    return f"oadr3-ven-{ven_id}-{config_name}-fetch-events-{int(run_at.timestamp())}"


def _next_daily_trigger_datetime(utc_trigger_time: time) -> datetime:
    now_utc = datetime.now(UTC)
    trigger = now_utc.replace(hour=utc_trigger_time.hour, minute=utc_trigger_time.minute, second=utc_trigger_time.second, microsecond=0)
    if trigger <= now_utc:
        trigger += timedelta(days=1)
    return trigger


def _fetch_events_queue() -> Queue:
    return cast("Queue", current_app.queues[FETCH_EVENTS_QUEUE_NAME])


def _delete_job(job_id: str) -> None:
    queue = _fetch_events_queue()
    try:
        existing_job = Job.fetch(job_id, connection=queue.connection)
    except NoSuchJobError:
        return
    existing_job.delete()


def _get_interval_period(event: ExistingEvent, interval: Interval) -> tuple[datetime, datetime]:
    interval_period = interval.interval_period or event.interval_period
    if interval_period is None:
        msg = f"OpenADR event '{event.id}' interval '{interval.id}' has no interval period."
        raise ValueError(msg)
    return interval_period.start, interval_period.start + interval_period.duration


def _iter_sensor_event_starts(start: datetime, end: datetime, event_resolution: timedelta) -> Iterator[datetime]:
    if event_resolution == timedelta(0):
        yield start
        return

    belief_start = start
    while belief_start + event_resolution <= end:
        yield belief_start
        belief_start += event_resolution


def _get_supported_event_payload_types(event: ExistingEvent) -> set[EventPayloadType]:
    return {pd.payload_type for pd in (event.payload_descriptors or ()) if pd.payload_type in SUPPORTED_DR_PAYLOAD_TYPES}


def _collect_active_periods(events: list[ExistingEvent]) -> list[EventActivePeriod]:
    active_periods: list[EventActivePeriod] = []
    for event in events:
        for interval in event.intervals or ():
            start, end = _get_interval_period(event, interval)
            active_periods.append(EventActivePeriod(start=start, end=end, event_id=event.id))
    return active_periods


def _validate_no_overlapping_events(events: list[ExistingEvent]) -> None:
    active_periods = sorted(_collect_active_periods(events), key=lambda period: period.start)
    for index, period in enumerate(active_periods):
        for previous in active_periods[:index]:
            if previous.event_id == period.event_id:
                continue
            if previous.start < period.end and period.start < previous.end:
                msg = (
                    f"OpenADR events overlap: "
                    f"event '{previous.event_id}' active from {previous.start} to {previous.end}, "
                    f"event '{period.event_id}' active from {period.start} to {period.end}."
                )
                raise OverlappingDemandResponseEventsError(msg)


def _fetch_events(ven_client: VenClient, targets: list[str]) -> list[ExistingEvent]:
    client = ven_client.create_http_client()

    target_filter = TargetFilter(targets=targets)

    all_events = client.events.get_events(
        target=target_filter,
        pagination=None,
        program_id=None,
    )
    return [event for event in all_events if _get_supported_event_payload_types(event)]


def _store_event_payloads(
    events: list[ExistingEvent],
    sensor_mapping: PayloadTypeSensorMapping,
) -> int:
    source = get_or_create_source(
        source=OPENADR_EVENT_SOURCE_NAME,
        source_type=OPENADR_EVENT_SOURCE_TYPE,
    )
    stored_beliefs = 0
    for event in events:
        belief_time = event.modification_date_time
        for interval in event.intervals or ():
            start, end = _get_interval_period(event, interval)
            for payload in interval.payloads:
                sensor = sensor_mapping.get_sensor_for(payload.type)
                if sensor is None:
                    continue
                if len(payload.values) != 1:
                    msg = f"OpenADR event '{event.id}' interval '{interval.id}' payload '{payload.type}' has {len(payload.values)} values."
                    raise ValueError(msg)

                for belief_start in _iter_sensor_event_starts(start, end, sensor.event_resolution):
                    db.session.merge(
                        TimedBelief(
                            sensor=sensor,
                            source=source,
                            event_value=float(payload.values[0]),
                            event_start=belief_start,
                            belief_time=belief_time,
                        )
                    )
                    stored_beliefs += 1
    db.session.flush()
    return stored_beliefs


class VenFetchJobScheduler:
    """Manages RQ jobs for fetching OpenADR events per sensor config."""

    def __init__(self, repository: VenClientRepository) -> None:
        self._repository = repository

    def schedule(
        self,
        ven_client: VenClient,
        sensor_config: VenSensorConfig,
        *,
        replace_existing: bool = True,
    ) -> Job | None:
        """Schedule or reschedule the daily fetch-events job for a sensor config."""
        if sensor_config.utc_trigger_time is None:
            return None

        if replace_existing and sensor_config.fetch_events_job_id:
            _delete_job(sensor_config.fetch_events_job_id)

        run_at = _next_daily_trigger_datetime(sensor_config.utc_trigger_time)
        job_id = _build_job_id(ven_client.id, sensor_config.name, run_at)
        queue = _fetch_events_queue()

        current_app.logger.info(
            "Scheduling fetch-events job for VEN '%s' (%s) config '%s' at %s.",
            ven_client.name,
            ven_client.id,
            sensor_config.name,
            run_at,
        )

        job = queue.enqueue_at(
            datetime=run_at,
            f=self._execute,
            kwargs={"ven_id": ven_client.id, "config_name": sensor_config.name},
            job_id=job_id,
            result_ttl=60 * 60 * 24,
        )

        self._repository.persist_job_id(ven_client, sensor_config.name, job.id)
        return cast("Job", job)

    def delete(self, sensor_config: VenSensorConfig) -> None:
        """Delete the scheduled job for a single sensor config."""
        if sensor_config.fetch_events_job_id:
            _delete_job(sensor_config.fetch_events_job_id)

    def delete_all(self, ven_client: VenClient) -> None:
        """Delete all scheduled jobs for every sensor config of a VEN client."""
        for sensor_config in ven_client.sensor_configs:
            self.delete(sensor_config)

    def _execute(self, ven_id: int, config_name: str) -> None:
        """Fetch events for a sensor config and store the resulting beliefs."""
        ven_client = self._repository.find_by_id(ven_id)
        if ven_client is None:
            current_app.logger.warning("Stopping fetch-events job: VEN id '%s' no longer exists.", ven_id)
            return

        sensor_config = ven_client.get_sensor_config(config_name)
        if sensor_config is None:
            current_app.logger.warning(
                "Stopping fetch-events job: config '%s' no longer exists on VEN '%s' (%s).",
                config_name,
                ven_client.name,
                ven_id,
            )
            return

        try:
            sensor_mapping = PayloadTypeSensorMapping.from_sensor_config(sensor_config)
            if not sensor_mapping:
                current_app.logger.warning(
                    "No sensors configured for VEN '%s' config '%s', skipping.",
                    ven_client.name,
                    sensor_config.name,
                )
                return

            events = _fetch_events(ven_client, sensor_config.targets)
            _validate_no_overlapping_events(events)
            stored_beliefs = _store_event_payloads(events, sensor_mapping)
            current_app.logger.info(
                "Fetched DR events for VEN '%s' (%s) config '%s', stored %s beliefs.",
                ven_client.name,
                ven_id,
                config_name,
                stored_beliefs,
            )
            db.session.commit()
        finally:
            self.schedule(ven_client, sensor_config, replace_existing=False)
            db.session.commit()