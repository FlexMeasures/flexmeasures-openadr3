"""Helpers to seed OpenADR 3.1 VTN test data for end-to-end tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import random
from typing import Tuple
import uuid

from openadr3_client.bl.http_factory import BusinessLogicHttpClientFactory
from openadr3_client.oadr310._bl.client import BusinessLogicClient
from openadr3_client.oadr310._ven.client import VirtualEndNodeClient
from openadr3_client.oadr310.models.event.event import ExistingEvent, Interval, NewEvent
from openadr3_client.oadr310.models.event.event_payload import (
    EventPayload,
    EventPayloadDescriptor,
    EventPayloadType,
)
from openadr3_client.oadr310.models.program.program import NewProgram
from openadr3_client.oadr310.models.unit import Unit
from openadr3_client.ven.http_factory import VirtualEndNodeHttpClientFactory
from openadr3_client._models.common.interval_period import IntervalPeriod
from openadr3_client.version import OADRVersion

from tests.test_container.integration_types import IntegrationTestVTNClient


def create_bl_http_client(vtn_client: IntegrationTestVTNClient) -> BusinessLogicClient:
    """Build a Business Logic HTTP client from integration-test VTN/OAuth settings."""
    oauth = vtn_client.oauth_configuration
    vtn = vtn_client.vtn_configuration
    return BusinessLogicHttpClientFactory.create_http_bl_client(
        vtn_base_url=vtn.vtn_base_url,
        client_id=oauth.client_id,
        client_secret=oauth.client_secret,
        token_url=oauth.token_url,
        scopes=oauth.scopes,
        audience=oauth.audience,
        verify_vtn_tls_certificate=False,
        allow_insecure_http=vtn.allow_insecure_http,
        version=OADRVersion.OADR_310,
    )  # type: ignore[return-value]


def create_ven_http_client(
    vtn_client: IntegrationTestVTNClient,
) -> VirtualEndNodeClient:
    """Build a VEN HTTP client from integration-test VTN/OAuth settings."""
    oauth = vtn_client.oauth_configuration
    vtn = vtn_client.vtn_configuration
    return VirtualEndNodeHttpClientFactory.create_http_ven_client(
        vtn_base_url=vtn.vtn_base_url,
        client_id=oauth.client_id,
        client_secret=oauth.client_secret,
        token_url=oauth.token_url,
        scopes=oauth.scopes,
        verify_vtn_tls_certificate=False,
        allow_insecure_http=vtn.allow_insecure_http,
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
    96 consecutive 15-minute intervals. Each interval carries one import and one
    export capacity limit value. FlexMeasures stores one belief per interval at the
    interval start when the VEN fetch job runs.
    """
    event_start = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)
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
            program_name=f"e2e-capacity-program-{uuid.uuid4().hex[:8]}",
            payload_descriptors=payload_descriptors,
        )
    )

    intervals: Tuple[Interval[EventPayload], ...] = ()

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
            event_name=f"e2e-capacity-event-{uuid.uuid4().hex[:8]}",
            targets=() if target is None else (target,),
            payload_descriptors=payload_descriptors,
            intervals=intervals,
        )
    )
