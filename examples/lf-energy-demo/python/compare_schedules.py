#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "httpx>=0.27",
# ]
# ///
"""
Trigger a schedule for the demo campus and show what OpenADR changed about it.

Run this twice in the walkthrough:

    uv run compare_schedules.py    # before wiring the OpenADR capacity limits in
    uv run compare_schedules.py    # again, after wiring them in

Each run triggers one schedule for `demo-campus`, waits for it, and saves the resulting
plan to `../schedule-runs/<label>.json`. The second run also prints a before-and-after
report and writes `../schedule-runs/comparison.html` — a self-contained page with an
inline SVG diagram (open it in a browser); see comparison_diagram.py for how it is drawn.

The request body deliberately carries no flex-model and no flex-context, so the scheduler
uses whatever is stored on the asset tree at that moment. Which of the two runs this is,
therefore, is not something the script is told: it reads the campus flex-context and
labels the run `after-openadr` when the OpenADR capacity-limit sensors are wired into it,
and `before-openadr` when they are not. Use --label to override that.

Both runs schedule the same window, because the second one reuses the first one's start
and duration. Without that, a user who spends twenty minutes on the manual UI steps in
between would end up comparing two plans for two different days.

Like seed_events.py, this runs on your host machine rather than in a container: it only
ever talks HTTP to the FlexMeasures instance at the URL in settings.py.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from comparison_diagram import render_html
from flexmeasures_client import (
    FlexMeasuresClient,
    FlexMeasuresError,
    SensorRef,
)
from schedule_comparison import (
    AFTER_LABEL,
    BEFORE_LABEL,
    EVSE_HUB_POWER,
    EXPORT_LIMIT,
    HEAT_PUMP_POWER,
    IMPORT_LIMIT,
    LABELS,
    PRICE,
    ROLE_TITLES,
    SITE_POWER,
    ComparisonError,
    ScheduleRun,
    SeriesRecord,
    compare,
    render_report,
)
from settings import FLEXMEASURES_URL

# hierarchy.py is the walkthrough's single description of what the demo site looks like,
# so the asset names are taken from there rather than repeated. It lives in the sibling
# folder of scripts that run inside the container, but it imports nothing but the standard
# library and this folder's settings.py, so it is equally importable from the host.
_WALKTHROUGH_FLEXMEASURES_DIR = Path(__file__).resolve().parent.parent / "flexmeasures"
if str(_WALKTHROUGH_FLEXMEASURES_DIR) not in sys.path:
    sys.path.append(str(_WALKTHROUGH_FLEXMEASURES_DIR))

from hierarchy import (  # noqa: E402 - only importable after the sys.path line above
    CAMPUS_NAME,
    EVSE_HUB_NAME,
    OFFICE_HEAT_PUMP_NAME,
    POWER_RESOLUTION,
    POWER_SENSOR_NAME,
    SITE_TIMEZONE,
)

# Saved runs sit next to the walkthrough's other folders rather than inside `python/`,
# which stays source-only because the containers bind-mount it read-only.
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "schedule-runs"

# A day is long enough to hold a full price cycle, an office day and a charging day, and
# short enough to stay readable on one chart. It also sits inside the 24-hour OpenADR
# event that seed_events.py creates.
DEFAULT_DURATION = timedelta(hours=24)

# A nine-device MILP over 96 quarter hours takes seconds, not minutes, but a cold worker
# has to import FlexMeasures first.
POLL_INTERVAL = timedelta(seconds=2)
SCHEDULE_TIMEOUT = timedelta(minutes=10)

# Which sensor tells which part of the story. The campus one is the headline: it carries
# the whole site's net grid exchange, because seed_assets.py points the flex-context's
# `aggregate-consumption` at it.
PLAN_SENSORS = {
    SITE_POWER: (CAMPUS_NAME, POWER_SENSOR_NAME),
    EVSE_HUB_POWER: (EVSE_HUB_NAME, POWER_SENSOR_NAME),
    HEAT_PUMP_POWER: (OFFICE_HEAT_PUMP_NAME, POWER_SENSOR_NAME),
}

# Flex-context fields that only exist once the OpenADR capacity limits have been wired in,
# and the roles their sensors play here.
OPENADR_LIMIT_FIELDS = {
    "site-consumption-capacity": IMPORT_LIMIT,
    "site-production-capacity": EXPORT_LIMIT,
}

# The flex-context field holding the price curve the scheduler optimises against.
PRICE_FIELD = "consumption-price"


class WindowError(RuntimeError):
    """The window to schedule is not covered by the seeded demo data."""


@dataclass(frozen=True, slots=True)
class CampusContext:
    """
    What the campus asset currently says about itself.

    :param asset_id:      Id of the campus asset in this instance.
    :param flex_context:  The flex-context stored on it.
    """

    asset_id: int
    flex_context: dict

    @classmethod
    def of(cls, asset_id: int, asset: dict) -> CampusContext:
        """
        Read the campus context out of an asset as the API returns it.

        The asset endpoint serialises the flex-context JSON column as a string rather than
        as a nested object, so it is decoded here; a dict is accepted too, in case a later
        FlexMeasures release nests it.

        :param asset_id:  Id of the asset.
        :param asset:     The asset as the API represents it.
        :returns:         The campus context.
        """
        stored = asset.get("flex_context") or {}
        return cls(asset_id=asset_id, flex_context=json.loads(stored) if isinstance(stored, str) else stored)

    def sensor_id(self, field: str) -> int | None:
        """
        Return the sensor a flex-context field points at, if it points at one.

        A flex-context field may hold a fixed quantity (`"1 MVA"`) instead of a sensor
        reference, and the OpenADR fields are absent altogether until the walkthrough
        wires them in, so both cases answer None.

        :param field:  Flex-context field name, e.g. `site-consumption-capacity`.
        :returns:      The sensor id, or None.
        """
        value = self.flex_context.get(field)
        if isinstance(value, dict) and "sensor" in value:
            return int(value["sensor"])
        return None

    @property
    def openadr_is_wired(self) -> bool:
        """Whether either OpenADR capacity-limit field points at a sensor."""
        return any(self.sensor_id(field) is not None for field in OPENADR_LIMIT_FIELDS)


def next_event_start(resolution: timedelta) -> datetime:
    """
    Return the next boundary a sensor of this resolution starts an event on.

    Scheduling from the next boundary rather than the current one keeps the whole plan in
    the future, so nothing in it is already water under the bridge by the time it is read.

    :param resolution:  Event resolution of the sensors being scheduled.
    :returns:           The next event start, in UTC.
    """
    now = datetime.now(UTC)
    elapsed = timedelta(seconds=now.timestamp() % resolution.total_seconds())
    return now - elapsed + resolution


def check_window_is_seeded(client: FlexMeasuresClient, campus: CampusContext, start: datetime, duration: timedelta) -> None:
    """
    Refuse to trigger a schedule for a window the demo data does not cover.

    seed_forecasts.py seeds a week from the moment it runs, so a walkthrough left open
    over a long weekend, or a second trigger made days after the first, can ask the
    scheduler to plan against prices that were never seeded. The scheduler would answer
    with an opaque failure, so this checks the price curve first and says what to do.

    :param client:            Logged-in client.
    :param campus:            The campus and its flex-context.
    :param start:             Start of the window about to be scheduled.
    :param duration:          Length of that window.
    :raises WindowError:      When the price curve has gaps over the window.
    """
    price_sensor_id = campus.sensor_id(PRICE_FIELD)
    if price_sensor_id is None:
        # Nothing to check against; the scheduler will report its own complaint.
        return
    # At the plan's own resolution, not the price sensor's hourly one: a window starting at
    # a quarter past the hour does not line up with hourly events, and the API answers a
    # misaligned request with a series of nulls that would look exactly like missing data.
    price = client.get_sensor_data(client.describe_sensor(price_sensor_id), start, duration, resolution=POWER_RESOLUTION)
    missing = [event_start for event_start, value in zip(price.event_starts(), price.values, strict=True) if value is None]
    if missing:
        msg = (
            f"The day-ahead price curve has no value for {len(missing)} of the {len(price.values)} intervals in "
            f"{start.isoformat()} + {duration} (first gap at {missing[0].isoformat()}).\n"
            "The demo data only covers a week from the moment seed_forecasts.py last ran. Re-seed it with:\n"
            "  docker compose exec server uv run --active --no-project /walkthrough/flexmeasures/seed_forecasts.py\n"
            "and start a fresh comparison with --fresh-window."
        )
        raise WindowError(msg)


def capture_context_series(client: FlexMeasuresClient, campus: CampusContext, start: datetime, duration: timedelta) -> list[SeriesRecord]:
    """
    Fetch the inputs that explain a plan: the price curve and any OpenADR capacity limits.

    Saved alongside the plan itself, so a comparison can hold both plans against the limit
    that caused the difference without going back to the server.

    :param client:    Logged-in client.
    :param campus:    The campus and its flex-context.
    :param start:     Start of the scheduled window.
    :param duration:  Length of that window.
    :returns:         One record per input series that exists.
    """
    records = []
    for field, role in {PRICE_FIELD: PRICE, **OPENADR_LIMIT_FIELDS}.items():
        sensor_id = campus.sensor_id(field)
        if sensor_id is None:
            continue
        sensor = client.describe_sensor(sensor_id)
        # At the plan's own resolution, so every panel of the figure shares one time grid.
        series = client.get_sensor_data(sensor, start, duration, resolution=POWER_RESOLUTION)
        records.append(SeriesRecord(role=role, sensor_id=sensor.id, sensor_label=sensor.label, series=series))
    return records


def run_schedule(client: FlexMeasuresClient, campus: CampusContext, label: str, start: datetime, duration: timedelta) -> ScheduleRun:
    """
    Trigger one schedule for the campus and collect everything worth keeping about it.

    :param client:              Logged-in client.
    :param campus:              The campus and its flex-context.
    :param label:               Which of the two walkthrough runs this is.
    :param start:               Start of the window to schedule.
    :param duration:            Length of that window.
    :returns:                   The finished run, ready to save.
    :raises FlexMeasuresError:  When the trigger is refused or the job does not finish.
    """
    print(f"Triggering a {duration} schedule for {CAMPUS_NAME} (asset {campus.asset_id}) from {start.isoformat()} ...")
    triggered_at = datetime.now(UTC)
    job_id = client.trigger_asset_schedule(campus.asset_id, start, duration)
    print(f"  job {job_id}; waiting for the scheduling worker ...")

    records = []
    for role, (asset_name, sensor_name) in PLAN_SENSORS.items():
        sensor: SensorRef = client.find_sensor(asset_name, sensor_name)
        series = client.await_schedule(sensor, job_id, duration, POLL_INTERVAL, SCHEDULE_TIMEOUT)
        records.append(SeriesRecord(role=role, sensor_id=sensor.id, sensor_label=sensor.label, series=series))
        print(f"  {ROLE_TITLES[role]}: {len(series.values)} values on {sensor.label}")

    records.extend(capture_context_series(client, campus, start, duration))

    return ScheduleRun(
        label=label,
        asset_name=CAMPUS_NAME,
        job_id=job_id,
        triggered_at=triggered_at,
        window_start=start,
        window_duration=duration,
        flex_context=campus.flex_context,
        series={record.role: record for record in records},
    )


# --- Orchestration -----------------------------------------------------------


def choose_window(
    output_dir: Path,
    label: str,
    requested_start: datetime | None,
    requested_duration: timedelta,
    *,
    fresh_window: bool,
) -> tuple[datetime, timedelta]:
    """
    Decide which window to schedule, reusing the other run's window when there is one.

    Two plans are only comparable interval by interval if they cover the same intervals,
    and the walkthrough's two runs are minutes or hours apart, so by default the second
    run adopts the first run's window rather than starting from its own now.

    :param output_dir:          Folder saved runs live in.
    :param label:               Label of the run about to be made.
    :param requested_start:     Start given on the command line, or None.
    :param requested_duration:  Duration given on the command line.
    :param fresh_window:        Ignore any saved window and start a new comparison.
    :returns:                   The window start and duration to use.
    """
    if requested_start is not None:
        return requested_start, requested_duration
    counterpart = next(other for other in LABELS if other != label)
    if not fresh_window:
        saved = ScheduleRun.load(output_dir, counterpart)
        if saved is not None:
            print(f"Reusing the window of the saved '{counterpart}' run, so the two plans line up interval by interval.")
            return saved.window_start, saved.window_duration
    return next_event_start(POWER_RESOLUTION), requested_duration


def report(output_dir: Path, *, plot: bool) -> bool:
    """
    Print the before-and-after comparison, and draw it, once both runs exist.

    :param output_dir:  Folder saved runs live in.
    :param plot:        Whether to also write the diagram.
    :returns:           Whether a comparison could be made.
    """
    before = ScheduleRun.load(output_dir, BEFORE_LABEL)
    after = ScheduleRun.load(output_dir, AFTER_LABEL)
    missing = [label for label, run in ((BEFORE_LABEL, before), (AFTER_LABEL, after)) if run is None]
    if before is None or after is None:
        print(f"\nNo comparison yet: still missing the '{', '.join(missing)}' run. Run this script again once that side of the walkthrough is done.")
        return False

    comparison = compare(before, after)
    print()
    print(render_report(comparison))
    if plot:
        path = output_dir / "comparison.html"
        path.write_text(render_html(comparison, ZoneInfo(SITE_TIMEZONE)), encoding="utf-8")
        print(f"Wrote {path}")
    return True


def parse_arguments() -> argparse.Namespace:
    """Define and read this script's command-line arguments."""
    parser = argparse.ArgumentParser(description=f"Trigger a schedule for {CAMPUS_NAME} and compare it with the other side of the OpenADR wiring.")
    parser.add_argument(
        "--label",
        choices=LABELS,
        default=None,
        help="Which run this is. Detected from the campus flex-context when not given.",
    )
    parser.add_argument(
        "--hours",
        type=float,
        default=DEFAULT_DURATION.total_seconds() / 3600,
        help=f"How many hours to schedule (default: {DEFAULT_DURATION.total_seconds() / 3600:.0f}); ignored when the other run's window is reused.",
    )
    parser.add_argument(
        "--start",
        type=datetime.fromisoformat,
        default=None,
        help="Start of the schedule, as an ISO 8601 datetime with a timezone. Defaults to the next quarter hour, or the other run's start.",
    )
    parser.add_argument(
        "--fresh-window",
        action="store_true",
        help="Do not reuse the other run's window; start a new comparison from now.",
    )
    parser.add_argument(
        "--compare-only",
        action="store_true",
        help="Do not trigger anything; just re-render the report and diagram from the saved runs.",
    )
    parser.add_argument("--no-plot", action="store_true", help="Skip the HTML diagram and print only the report.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help=f"Where to save runs (default: {DEFAULT_OUTPUT_DIR})")
    return parser.parse_args()


def main() -> None:
    """Trigger one schedule, save it, and compare it with the other run when there is one."""
    args = parse_arguments()
    output_dir: Path = args.output_dir

    if args.compare_only:
        report(output_dir, plot=not args.no_plot)
        return

    with FlexMeasuresClient() as client:
        print(f"Logging in to FlexMeasures at {FLEXMEASURES_URL} ...")
        client.log_in()

        asset_id = client.find_asset_id(CAMPUS_NAME)
        campus = CampusContext.of(asset_id, client.get_asset(asset_id))
        label = args.label or (AFTER_LABEL if campus.openadr_is_wired else BEFORE_LABEL)
        wiring = "wired into" if campus.openadr_is_wired else "absent from"
        print(f"OpenADR capacity limits are {wiring} the {CAMPUS_NAME} flex-context, so this is the '{label}' run.")

        start, duration = choose_window(output_dir, label, args.start, timedelta(hours=args.hours), fresh_window=args.fresh_window)
        check_window_is_seeded(client, campus, start, duration)

        run = run_schedule(client, campus, label, start, duration)
        path = run.save(output_dir)
        print(f"Saved the '{label}' plan to {path}")

    report(output_dir, plot=not args.no_plot)


if __name__ == "__main__":
    try:
        main()
    except (FlexMeasuresError, WindowError, ComparisonError) as exc:
        print(f"\n{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
