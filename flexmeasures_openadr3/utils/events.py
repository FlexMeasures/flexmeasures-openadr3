from datetime import datetime, timedelta, timezone
import uuid

from openadr3_client.oadr310.models.event.event_payload import (
    EventPayload,
    EventPayloadType,
    EventPayloadDescriptor,
)
from openadr3_client.oadr310.models.event.event import ExistingEvent, Interval
from openadr3_client._models.common.interval_period import IntervalPeriod
from openadr3_client.oadr310.models.unit import Unit

SUPPORTED_SIGNAL_NAMES: tuple[EventPayloadType, ...] = (
    EventPayloadType.IMPORT_CAPACITY_AVAILABLE,
    EventPayloadType.EXPORT_CAPACITY_LIMIT,
)

ACTIVE_OPENADR_EVENTS: tuple[ExistingEvent, ...] = (
    ExistingEvent(
        id=str(uuid.uuid4()),
        programID="program-1",
        event_name="event-1",
        created_date_time=datetime.now(tz=timezone.utc),
        modification_date_time=datetime.now(tz=timezone.utc),
        priority=1,
        targets=("site:main", "market_role:prosumer"),
        payload_descriptors=(
            EventPayloadDescriptor(
                payload_type=EventPayloadType.IMPORT_CAPACITY_AVAILABLE,
                units=Unit.KW,
            ),
        ),
        intervals=(
            Interval(
                id=1,
                interval_period=IntervalPeriod(
                    start=datetime.now(tz=timezone.utc),
                    duration=timedelta(hours=1),
                ),
                payloads=(
                    EventPayload(
                        type=EventPayloadType.IMPORT_CAPACITY_AVAILABLE, values=(200,)
                    ),
                ),
            ),
        ),
    ),
)
