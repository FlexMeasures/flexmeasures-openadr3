from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import timely_beliefs as tb
from flexmeasures.data.models.time_series import Sensor
from openadr3_client.oadr310._vtn.interfaces.filters import TargetFilter
from openadr3_client.oadr310.models.event.event import ExistingEvent, Interval
from openadr3_client.oadr310.models.event.event_payload import EventPayloadType

from flexmeasures_openadr3.models.events import EventActivePeriod

if TYPE_CHECKING:
    from collections.abc import Iterator

    from flexmeasures.data.models.data_sources import DataSource

    from flexmeasures_openadr3.utils.ven_clients import VenClient

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


def validate_no_overlapping_events(events: list[ExistingEvent]) -> None:
    """Refuse events whose active intervals overlap, as it would be ambiguous which limit applies."""
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


def fetch_events(ven_client: VenClient, targets: list[str]) -> list[ExistingEvent]:
    """Fetch the events carrying a supported payload type for the given targets from the VEN client's VTN."""
    client = ven_client.create_http_client()

    target_filter = TargetFilter(targets=targets)

    all_events = client.events.get_events(
        target=target_filter,
        pagination=None,
        program_id=None,
    )
    return [event for event in all_events if _get_supported_event_payload_types(event)]


def event_payloads_to_beliefs(
    events: list[ExistingEvent],
    sensor_mapping: PayloadTypeSensorMapping,
    source: DataSource,
) -> dict[Sensor, list[tb.TimedBelief]]:
    """Turn the payloads of OpenADR events into beliefs, per sensor they are to be recorded on."""
    beliefs: dict[Sensor, list[tb.TimedBelief]] = {}
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

                beliefs.setdefault(sensor, []).extend(
                    tb.TimedBelief(
                        sensor=sensor,
                        source=source,
                        event_value=float(payload.values[0]),
                        event_start=belief_start,
                        belief_time=belief_time,
                    )
                    for belief_start in _iter_sensor_event_starts(start, end, sensor.event_resolution)
                )
    return beliefs
