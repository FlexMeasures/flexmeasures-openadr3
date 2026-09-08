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

Import capacity is generous for most of the event and drops to a hard limit over one
curtailment window, which spans the demo site's whole office day and is pinned to the local
wall clock rather than offset from whenever this script runs. That is what makes the
walkthrough's before-and-after comparison worth looking at: covering the office day end to
end guarantees the ask overlaps every flexible thing the site has, and reaches hours the
day-ahead price was not already discouraging load in. See CURTAILMENT_START_HOUR and
CURTAILED_CAPACITY_LIMIT below for how those numbers were chosen.

Run with:
    uv run seed_events.py
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

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

# The site's timezone and office hours are taken from the walkthrough's one description of
# the demo site, so the curtailment window below and the load shapes it has to bite into
# cannot drift apart: the window *is* the office day, not a second opinion about when that
# day runs. hierarchy.py imports nothing but the standard library and this folder's
# settings.py, so it is importable from the host too.
_WALKTHROUGH_FLEXMEASURES_DIR = Path(__file__).resolve().parent.parent / "flexmeasures"
if str(_WALKTHROUGH_FLEXMEASURES_DIR) not in sys.path:
    sys.path.append(str(_WALKTHROUGH_FLEXMEASURES_DIR))

from hierarchy import (  # noqa: E402 - only importable after the sys.path line above
    OFFICE_CLOSING_HOUR,
    OFFICE_OPENING_HOUR,
    SITE_TIMEZONE,
)

# One day of quarter-hourly intervals, matching the resolution of the sensors the plugin
# records the fetched limits on.
EVENT_DURATION = timedelta(hours=24)
INTERVAL_DURATION = timedelta(minutes=15)

# Comfortably above anything the demo site draws or feeds back, so outside the curtailment
# window the limit is present but never binding.
UNCONSTRAINED_CAPACITY_LIMIT = 400.0

# What the site is asked down to over the curtailment window, in kW.
#
# Chosen against the site's *inflexible* load — the office baseload of about 85 kW over the
# working day, less whatever its rooftop PV happens to be covering. It is a deep ask against
# the 400 kW the site is otherwise allowed, about 18% of it, which is the point: a
# distribution-grid congestion window, not a nudge.
#
# Over the middle of the office day PV keeps that inflexible floor comfortably below this
# limit, so the flexible fleet can absorb the whole ask there and the schedule sits pinned
# exactly on the limit. The window's own edges are tighter: the limit now opens at
# OFFICE_OPENING_HOUR, on the morning ramp, before the PV has come up under a baseload that
# has already switched to its day level. On a cloudy morning the load the site cannot move
# can therefore start out at or just above this limit on its own, and the scheduler has no
# choice but to breach it for the first interval or two before PV closes the gap.
#
# That residual breach is honest rather than a misconfiguration — a real congestion ask does
# not stop at what the site finds convenient — but it does mean the walkthrough's report
# normally shows the intervals over the limit falling to a low number rather than cleanly to
# zero. How many depends on the day's cloud cover, which seed_forecasts.py draws per day.
CURTAILED_CAPACITY_LIMIT = 70.0

# When the curtailment sits: the demo site's whole office day, as a local wall-clock hour in
# SITE_TIMEZONE and a duration derived from it, so the two can never disagree about where
# the day ends.
#
# Pinned to the clock rather than offset from whenever this script happens to run, for the
# same reason hierarchy.py pins the office and charging shapes: it has to collide with load
# that is actually worth curtailing. Taking the office day whole is the surest way to do
# that, and asks more of the scheduler than a narrower window would:
#
# - it covers the EVSE fleet's entire presence, from the first arrivals through to the last
#   departure (arrivals are centred just after opening and the earliest anyone leaves is
#   mid-afternoon), and the heat pump's whole occupied period, so nothing flexible is left
#   sitting outside the ask with somewhere easy to hide; and
# - it reaches past the day-ahead curve's midday solar dip at both ends. Around opening the
#   curve is climbing into its morning peak and by closing it is climbing into its evening
#   peak — hours where the price signal alone would not have discouraged load, and in the
#   morning would have positively encouraged filling up before the peak. A flat cap held
#   across all of that is therefore unmistakably the capacity limit's doing rather than
#   price optimisation happening to agree with it.
CURTAILMENT_START_HOUR = OFFICE_OPENING_HOUR
CURTAILMENT_DURATION = timedelta(hours=OFFICE_CLOSING_HOUR - OFFICE_OPENING_HOUR)

# How far ahead of the curtailment the event itself starts. The window needs company on
# either side: it is the stretches where the limit is generous that show the two plans
# agreeing, which is what makes the curtailed stretch read as a deliberate ask.
CURTAILMENT_LEAD = timedelta(hours=3)

# The horizon a walkthrough schedule covers (compare_schedules.py plans 24 hours from the
# next quarter hour), used to check the pinned window can still be scheduled in full.
SCHEDULE_HORIZON = timedelta(hours=24)

# Shortest notice the curtailment is ever given, for the fallback below.
MINIMUM_NOTICE = timedelta(minutes=30)


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


def aligned_to_interval(moment: datetime) -> datetime:
    """
    Round a moment down onto a quarter-hour boundary.

    The alignment matters. FlexMeasures records these limits on a quarter-hourly sensor and
    the scheduler resamples them onto its own quarter-hourly grid, so an interval starting
    at, say, 15:10 falls between two of that sensor's events and is silently dropped: the
    limits would be visible on the sensor's own chart, yet have no effect on any schedule.

    :param moment:  Any moment, timezone-aware.
    :returns:       The start of the quarter hour it falls in, in UTC.
    """
    floored = moment.astimezone(UTC).replace(second=0, microsecond=0)
    return floored - timedelta(minutes=floored.minute % (INTERVAL_DURATION // timedelta(minutes=1)))


def next_local_hour(hour: float, after: datetime) -> datetime:
    """
    Return the next moment the site's local clock reads the given hour.

    Built by localising a wall-clock time on a local calendar date, so the two days a year
    the offset changes still land on the intended local hour rather than an hour either side
    of it.

    :param hour:   Local wall-clock hour in SITE_TIMEZONE, as a float, so 8.5 means 08:30.
    :param after:  The moment to find the next occurrence after.
    :returns:      That moment, in UTC.
    """
    zone = ZoneInfo(SITE_TIMEZONE)
    local_now = after.astimezone(zone)
    midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    candidate = midnight + timedelta(hours=hour)
    if candidate <= local_now:
        # A local day is not always 24 hours long, so step to the next date and re-localise
        # the wall-clock hour on it rather than adding a fixed 24 hours to the candidate.
        candidate = (midnight + timedelta(days=1, hours=12)).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=hour)
    return candidate.astimezone(UTC)


def curtailment_window(now: datetime) -> tuple[datetime, bool]:
    """
    Decide when the curtailment starts.

    Normally this is the next time the site's clock reaches CURTAILMENT_START_HOUR. That
    only works while the whole window still fits inside the horizon a walkthrough schedule
    covers, though: a user who runs this script once the day's window has already opened
    would otherwise get a window whose tail falls past the end of the schedule, and a
    before-and-after comparison that only sees part of the ask.

    Because the window is now as long as the office day itself, that leaves less room than
    it used to: the next office day only fits inside the coming SCHEDULE_HORIZON when this
    script runs outside office hours, so a walkthrough run during the day takes the
    fallback below rather than the pinned window.

    In that case the curtailment is instead given the shortest notice it is ever given and
    starts almost immediately, which reads as a grid operator asking for a reduction now.
    It then covers an office day's worth of hours from that moment rather than the office
    day proper, so it still opens on a plugged-in fleet with something to shift; it simply
    runs on into the evening instead of stopping at closing time.

    :param now:  The current moment.
    :returns:    Start of the curtailment in UTC, and whether it had to fall back.
    """
    pinned = next_local_hour(CURTAILMENT_START_HOUR, now)
    if pinned + CURTAILMENT_DURATION <= now + SCHEDULE_HORIZON:
        return aligned_to_interval(pinned), False
    return aligned_to_interval(now + MINIMUM_NOTICE + INTERVAL_DURATION), True


def capacity_limit_at(interval_start: datetime, curtailment_start: datetime) -> float:
    """
    Return the import capacity limit for one interval of the event.

    A real capacity-limit event is not noise: it leaves the site alone most of the time and
    asks for a hard reduction over a defined window. Shaping it that way is what makes the
    demo's before-and-after comparison readable — outside the window both plans follow the
    price curve and agree, and inside it the scheduler has to move load out of the way.

    :param interval_start:     Start of the interval being valued.
    :param curtailment_start:  Start of the curtailment window.
    :returns:                  The limit in kW.
    """
    if curtailment_start <= interval_start < curtailment_start + CURTAILMENT_DURATION:
        return CURTAILED_CAPACITY_LIMIT
    return UNCONSTRAINED_CAPACITY_LIMIT


def seed_capacity_limit_event(
    bl_client: BusinessLogicClient,
    *,
    target: str | None = None,
) -> tuple[ExistingEvent, datetime, bool]:
    """
    Create a program and event with IMPORT/EXPORT_CAPACITY_LIMIT payloads.

    The event spans 24 hours as 96 consecutive 15-minute intervals — matching
    tests/test_container/openadr_vtn_setup.py — and is positioned so the curtailment window
    sits CURTAILMENT_LEAD into it. Import capacity is curtailed over that one window; export
    capacity is left generous throughout, so the demo has a single, legible constraint to
    react to.

    :param bl_client:  Business Logic client for the walkthrough VTN.
    :param target:     Optional event target, or None to publish to every VEN.
    :returns:          The created event, the curtailment start in UTC, and whether the
                       pinned local window had to fall back to short notice.
    """
    curtailment_start, gave_short_notice = curtailment_window(datetime.now(UTC))
    # Placing the event a fixed lead ahead of the curtailment keeps the window well inside
    # the event's own 24 hours, so the event always covers the whole ask. When the
    # curtailment had to be given short notice this puts the event start in the recent past,
    # which is harmless: those intervals land on the sensor as history the schedule window,
    # which begins at the next quarter hour, never reaches back into.
    event_start = curtailment_start - CURTAILMENT_LEAD
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
    for i in range(EVENT_DURATION // INTERVAL_DURATION):
        interval_start = event_start + i * INTERVAL_DURATION
        intervals += (
            Interval(
                id=i,
                interval_period=IntervalPeriod(
                    start=interval_start,
                    duration=INTERVAL_DURATION,
                ),
                payloads=(
                    EventPayload(
                        type=EventPayloadType.IMPORT_CAPACITY_LIMIT,
                        values=(capacity_limit_at(interval_start, curtailment_start),),
                    ),
                    EventPayload(
                        type=EventPayloadType.EXPORT_CAPACITY_LIMIT,
                        values=(UNCONSTRAINED_CAPACITY_LIMIT,),
                    ),
                ),
            ),
        )

    event = bl_client.events.create_event(
        NewEvent(
            programID=program.id,
            event_name=f"walkthrough-capacity-event-{uuid.uuid4().hex[:8]}",
            targets=() if target is None else (target,),
            payload_descriptors=payload_descriptors,
            intervals=intervals,
        )
    )
    return event, curtailment_start, gave_short_notice


def main() -> None:
    """Seed a capacity-limit event on the VTN and print a short summary."""
    print("Connecting to VTN at", HOST_VTN_BASE_URL)
    bl_client = create_bl_client()
    event, curtailment_start, gave_short_notice = seed_capacity_limit_event(bl_client)
    first_start = event.intervals[0].interval_period.start if event.intervals and event.intervals[0].interval_period else None
    last_start = event.intervals[-1].interval_period.start if event.intervals and event.intervals[-1].interval_period else None
    print(f"Created event id={event.id} with {len(event.intervals or ())} intervals.")
    if first_start and last_start:
        print(f"  First interval start (UTC): {first_start.isoformat()}")
        print(f"  Last interval start  (UTC): {last_start.isoformat()}")
    local_start = curtailment_start.astimezone(ZoneInfo(SITE_TIMEZONE))
    local_end = (curtailment_start + CURTAILMENT_DURATION).astimezone(ZoneInfo(SITE_TIMEZONE))
    print(
        f"  Import capacity is {UNCONSTRAINED_CAPACITY_LIMIT:.0f} kW, dropping to {CURTAILED_CAPACITY_LIMIT:.0f} kW "
        f"from {curtailment_start.isoformat()} for {CURTAILMENT_DURATION}."
    )
    print(f"  That is {local_start:%a %H:%M} to {local_end:%H:%M} local time ({SITE_TIMEZONE}).")
    if gave_short_notice:
        print(
            f"  Note: the pinned {CURTAILMENT_START_HOUR:02.0f}:00-{CURTAILMENT_START_HOUR + CURTAILMENT_DURATION / timedelta(hours=1):02.0f}:00 "
            f"local office day no longer fits inside the coming {SCHEDULE_HORIZON // timedelta(hours=1)} hours, so the "
            "curtailment was given short notice and starts almost immediately instead. The fleet is still plugged in, "
            "so the comparison still has something to show; the window simply runs on into the evening rather than "
            "stopping at closing time."
        )
    print("Next: configure the VEN client and polling schedule in the FlexMeasures UI.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Failed to seed VTN events: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
