#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "openadr3-client>=2.0.4",
# ]
# ///
"""
Seed a 24-hour IMPORT/EXPORT_CAPACITY_LIMIT event on the OpenLEADR-rs VTN.

Uses the Business Logic OAuth client (test-client-id) — the same flow as the
integration test fixture vtn_capacity_limit_event_seed in tests/conftest.py.

Run with:
    uv run seed_events.py
"""

from __future__ import annotations

import os
import random
import sys
import uuid
from datetime import UTC, datetime, timedelta

from openadr3_client._models.common.interval_period import IntervalPeriod
from openadr3_client.bl.http_factory import BusinessLogicHttpClientFactory
from openadr3_client.oadr310._bl.client import BusinessLogicClient
from openadr3_client.oadr310.models.event.event import ExistingEvent, Interval, NewEvent
from openadr3_client.oadr310.models.event.event_payload import (
    EventPayload,
    EventPayloadDescriptor,
    EventPayloadType,
)
from openadr3_client.oadr310.models.program.program import NewProgram
from openadr3_client.oadr310.models.unit import Unit
from openadr3_client.version import OADRVersion
from settings import (
    BL_OAUTH_CLIENT_ID,
    HOST_KEYCLOAK_TOKEN_URL,
    HOST_VTN_BASE_URL,
    OAUTH_CLIENT_SECRET,
)


def create_bl_client() -> BusinessLogicClient:
    """Build a Business Logic HTTP client for the walkthrough VTN."""
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    return BusinessLogicHttpClientFactory.create_http_bl_client(
        vtn_base_url=HOST_VTN_BASE_URL,
        client_id=BL_OAUTH_CLIENT_ID,
        client_secret=OAUTH_CLIENT_SECRET,
        token_url=HOST_KEYCLOAK_TOKEN_URL,
        scopes=None,
        audience=None,
        verify_vtn_tls_certificate=False,
        allow_insecure_http=True,
        version=OADRVersion.OADR_310,
    )  # type: ignore[return-value]


def seed_capacity_limit_event(
    bl_client: BusinessLogicClient,
    *,
    target: str | None = None,
) -> ExistingEvent:
    """
    Create a program and event with IMPORT/EXPORT_CAPACITY_LIMIT payloads.

    The event starts one hour from the current UTC time and spans 24 hours as
    96 consecutive 15-minute intervals — matching tests/test_container/openadr_vtn_setup.py.
    """
    event_start = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=1)
    event_duration = timedelta(hours=24)
    payload_descriptors = (
        EventPayloadDescriptor(
            payload_type=EventPayloadType.IMPORT_CAPACITY_LIMIT,
            units=Unit.KW,
        ),
        EventPayloadDescriptor(
            payload_type=EventPayloadType.EXPORT_CAPACITY_LIMIT,
            units=Unit.KW,
        ),
    )

    program = bl_client.programs.create_program(
        NewProgram(
            program_name=f"walkthrough-capacity-program-{uuid.uuid4().hex[:8]}",
            payload_descriptors=payload_descriptors,
        )
    )

    intervals: tuple[Interval[EventPayload], ...] = ()
    for i in range(event_duration // timedelta(minutes=15)):
        intervals += (
            Interval(
                id=i,
                interval_period=IntervalPeriod(
                    start=event_start + timedelta(minutes=i * 15),
                    duration=timedelta(minutes=15),
                ),
                payloads=(
                    EventPayload(
                        type=EventPayloadType.IMPORT_CAPACITY_LIMIT,
                        values=(random.uniform(0, 100),),
                    ),
                    EventPayload(
                        type=EventPayloadType.EXPORT_CAPACITY_LIMIT,
                        values=(random.uniform(0, 100),),
                    ),
                ),
            ),
        )

    return bl_client.events.create_event(
        NewEvent(
            programID=program.id,
            event_name=f"walkthrough-capacity-event-{uuid.uuid4().hex[:8]}",
            targets=() if target is None else (target,),
            payload_descriptors=payload_descriptors,
            intervals=intervals,
        )
    )


def main() -> None:
    """Seed a capacity-limit event on the VTN and print a short summary."""
    print("Connecting to VTN at", HOST_VTN_BASE_URL)
    bl_client = create_bl_client()
    event = seed_capacity_limit_event(bl_client)
    first_start = event.intervals[0].interval_period.start if event.intervals and event.intervals[0].interval_period else None
    last_start = event.intervals[-1].interval_period.start if event.intervals and event.intervals[-1].interval_period else None
    print(f"Created event id={event.id} with {len(event.intervals or ())} intervals.")
    if first_start and last_start:
        print(f"  First interval start (UTC): {first_start.isoformat()}")
        print(f"  Last interval start  (UTC): {last_start.isoformat()}")
    print("Next: configure the VEN client and polling schedule in the FlexMeasures UI.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Failed to seed VTN events: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
