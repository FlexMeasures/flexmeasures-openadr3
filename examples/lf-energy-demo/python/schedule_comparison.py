"""
What a demo schedule run is, and how two of them compare.

The walkthrough triggers the same schedule twice: once before the OpenADR capacity
limits are wired into the campus flex-context, and once after. Both runs write their
beliefs to the same sensors from the same scheduler data source, so FlexMeasures' own
charts blend them into a single line and its schedule endpoint only ever reports the
most recent one. Each run therefore saves its own plan to a small JSON file the moment
it has it, and this module owns both that file format and the arithmetic that turns two
of those files into a statement about what the OpenADR signals changed.

No HTTP and no plotting here: fetching lives in flexmeasures_client.py, the command-line
entry point and the figure in compare_schedules.py.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from flexmeasures_client import TimeSeries

if TYPE_CHECKING:
    from pathlib import Path

# The two runs the walkthrough makes, named after where they sit in it.
BEFORE_LABEL = "before-openadr"
AFTER_LABEL = "after-openadr"
LABELS = (BEFORE_LABEL, AFTER_LABEL)

# Roles a series can play in the comparison. Keyed by role rather than by sensor id, so a
# saved run stays readable against a re-seeded instance where the ids have changed.
SITE_POWER = "site-power"
EVSE_HUB_POWER = "evse-hub-power"
HEAT_PUMP_POWER = "heat-pump-power"
PRICE = "consumption-price"
IMPORT_LIMIT = "site-consumption-capacity"
EXPORT_LIMIT = "site-production-capacity"

# Titles for the roles, used by both the printed report and the figure.
ROLE_TITLES = {
    SITE_POWER: "Campus grid connection",
    EVSE_HUB_POWER: "EVSE charging hub",
    HEAT_PUMP_POWER: "Office heat pump",
    PRICE: "Day-ahead price",
    IMPORT_LIMIT: "OpenADR import capacity limit",
    EXPORT_LIMIT: "OpenADR export capacity limit",
}

# The roles the scheduler itself decides, and therefore the ones a comparison is about.
PLAN_ROLES = (SITE_POWER, EVSE_HUB_POWER, HEAT_PUMP_POWER)

SECONDS_PER_HOUR = 3600

# How many of the largest per-interval changes the report spells out individually.
TOP_MOVEMENTS = 5

# Below this, a change is rounding noise rather than a decision the scheduler made.
NEGLIGIBLE_KW = 0.01


class ComparisonError(ValueError):
    """Two saved runs cannot be compared."""


@dataclass(frozen=True, slots=True)
class SeriesRecord:
    """
    One saved time series, with enough provenance to label it later.

    :param role:         Role the series plays in the comparison, e.g. `site-power`.
    :param sensor_id:    Sensor the values came from.
    :param sensor_label: `asset/sensor` path of that sensor.
    :param series:       The values themselves.
    """

    role: str
    sensor_id: int
    sensor_label: str
    series: TimeSeries

    def to_json(self) -> dict:
        """Render as JSON-serialisable data."""
        return {
            "role": self.role,
            "sensor-id": self.sensor_id,
            "sensor-label": self.sensor_label,
            "start": self.series.start.isoformat(),
            "resolution-seconds": int(self.series.resolution.total_seconds()),
            "unit": self.series.unit,
            "values": self.series.values,
        }

    @classmethod
    def from_json(cls, data: dict) -> SeriesRecord:
        """Rebuild from what to_json wrote."""
        return cls(
            role=data["role"],
            sensor_id=int(data["sensor-id"]),
            sensor_label=data["sensor-label"],
            series=TimeSeries(
                start=datetime.fromisoformat(data["start"]),
                resolution=timedelta(seconds=data["resolution-seconds"]),
                unit=data["unit"],
                values=list(data["values"]),
            ),
        )


@dataclass(frozen=True, slots=True)
class ScheduleRun:
    """
    One triggered schedule, as saved to disk.

    :param label:            Which of the two walkthrough runs this is.
    :param asset_name:       Name of the scheduled asset, e.g. `demo-campus`.
    :param job_id:           Id of the scheduling job that produced it.
    :param triggered_at:     When the trigger was sent, which is what separates the two
                             runs' beliefs in the database.
    :param window_start:     Start of the scheduled window.
    :param window_duration:  Length of the scheduled window.
    :param flex_context:     The scheduled asset's flex-context at trigger time, kept so a
                             saved run can say for itself whether OpenADR was wired in.
    :param series:           Saved series, keyed by role.
    """

    label: str
    asset_name: str
    job_id: str
    triggered_at: datetime
    window_start: datetime
    window_duration: timedelta
    flex_context: dict
    series: dict[str, SeriesRecord]

    @property
    def window_end(self) -> datetime:
        """End of the scheduled window."""
        return self.window_start + self.window_duration

    def plan(self, role: str) -> TimeSeries | None:
        """
        Return the series saved for a role, if this run captured one.

        :param role:  One of the module's role constants.
        :returns:     The series, or None when the run has nothing for that role.
        """
        record = self.series.get(role)
        return None if record is None else record.series

    def to_json(self) -> dict:
        """Render as JSON-serialisable data."""
        return {
            "label": self.label,
            "asset-name": self.asset_name,
            "job-id": self.job_id,
            "triggered-at": self.triggered_at.isoformat(),
            "window-start": self.window_start.isoformat(),
            "window-duration-seconds": int(self.window_duration.total_seconds()),
            "flex-context": self.flex_context,
            "series": [record.to_json() for record in self.series.values()],
        }

    @classmethod
    def from_json(cls, data: dict) -> ScheduleRun:
        """Rebuild from what to_json wrote."""
        records = [SeriesRecord.from_json(entry) for entry in data["series"]]
        return cls(
            label=data["label"],
            asset_name=data["asset-name"],
            job_id=data["job-id"],
            triggered_at=datetime.fromisoformat(data["triggered-at"]),
            window_start=datetime.fromisoformat(data["window-start"]),
            window_duration=timedelta(seconds=data["window-duration-seconds"]),
            flex_context=data["flex-context"],
            series={record.role: record for record in records},
        )

    def save(self, directory: Path) -> Path:
        """
        Write this run to `<label>.json`, replacing any earlier run with the same label.

        :param directory:  Folder to write into; created when missing.
        :returns:          Path of the file written.
        """
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.label}.json"
        path.write_text(json.dumps(self.to_json(), indent=2) + "\n", encoding="utf-8")
        return path

    @staticmethod
    def load(directory: Path, label: str) -> ScheduleRun | None:
        """
        Read back a saved run, if one exists.

        :param directory:  Folder saved runs live in.
        :param label:      Which run to read.
        :returns:          The run, or None when it has not been made yet.
        """
        path = directory / f"{label}.json"
        if not path.exists():
            return None
        return ScheduleRun.from_json(json.loads(path.read_text(encoding="utf-8")))


@dataclass(frozen=True, slots=True)
class Window:
    """
    A stretch of equally-spaced intervals two runs have in common.

    :param start:       Start of the first shared interval.
    :param resolution:  Length of every interval.
    :param length:      Number of intervals.
    """

    start: datetime
    resolution: timedelta
    length: int

    @property
    def hours_per_interval(self) -> float:
        """Length of one interval, in hours, for turning power into energy."""
        return self.resolution.total_seconds() / SECONDS_PER_HOUR

    @property
    def end(self) -> datetime:
        """End of the last interval."""
        return self.start + self.length * self.resolution

    def event_starts(self) -> list[datetime]:
        """Start of every interval, in order."""
        return [self.start + index * self.resolution for index in range(self.length)]


def overlap(first: TimeSeries, second: TimeSeries) -> Window:
    """
    Find the intervals two series share.

    Two runs normally cover exactly the same window, because the second run reuses the
    first run's. This still intersects them, so a user who deliberately started a fresh
    window still gets a comparison over whatever the two have in common.

    :param first:              One series.
    :param second:             The other.
    :returns:                  The shared window.
    :raises ComparisonError:   When the resolutions differ, or the windows do not overlap.
    """
    if first.resolution != second.resolution:
        msg = f"Cannot compare series recorded at different resolutions ({first.resolution} and {second.resolution})."
        raise ComparisonError(msg)
    start = max(first.start, second.start)
    end = min(first.end, second.end)
    if end <= start:
        msg = f"The two runs do not overlap in time ({first.start.isoformat()} to {first.end.isoformat()} versus {second.start.isoformat()} to {second.end.isoformat()})."
        raise ComparisonError(msg)
    return Window(start=start, resolution=first.resolution, length=int((end - start) / first.resolution))


def values_over(series: TimeSeries, window: Window) -> list[float | None]:
    """
    Slice a series down to a window, padding with None where it does not reach.

    :param series:  The series to slice.
    :param window:  The window to express it over.
    :returns:       One value per interval of the window.
    """
    offset = int((window.start - series.start) / series.resolution)
    sliced: list[float | None] = []
    for index in range(window.length):
        source_index = offset + index
        sliced.append(series.values[source_index] if 0 <= source_index < len(series.values) else None)
    return sliced


@dataclass(frozen=True, slots=True)
class PlanMetrics:
    """
    What one plan does over a window, in the terms a site operator cares about.

    :param peak_import:            Highest power drawn from the grid, in kW.
    :param peak_import_at:         When that happens.
    :param peak_export:            Highest power fed back, in kW, as a positive number.
    :param peak_export_at:         When that happens.
    :param energy_imported:        Total energy drawn over the window, in kWh.
    :param energy_exported:        Total energy fed back over the window, in kWh.
    :param cost:                   Cost of the plan against the day-ahead price, or None
                                   when no price series was captured.
    :param currency:               Currency the cost is expressed in, or None.
    :param intervals_over_limit:   Intervals in which the plan exceeds an OpenADR capacity
                                   limit, or None when no limit was captured.
    :param worst_overshoot:        Largest single breach of that limit, in kW.
    :param energy_over_limit:      Total energy drawn above that limit, in kWh.
    """

    peak_import: float
    peak_import_at: datetime
    peak_export: float
    peak_export_at: datetime
    energy_imported: float
    energy_exported: float
    cost: float | None
    currency: str | None
    intervals_over_limit: int | None
    worst_overshoot: float | None
    energy_over_limit: float | None


def _price_per_kwh(price: TimeSeries | None, window: Window) -> tuple[list[float | None], str | None]:
    """
    Express a price series over the window, one price per interval.

    A day-ahead price sensor is hourly while the plan is quarter-hourly, so each interval
    takes the price of the hour it falls in.

    :param price:   The price series, or None when the run captured none.
    :param window:  Window to express it over.
    :returns:       One price per interval and the currency, or Nones when there is no price.
    """
    if price is None:
        return [None] * window.length, None
    prices: list[float | None] = []
    for event_start in window.event_starts():
        index = int((event_start - price.start) / price.resolution)
        prices.append(price.values[index] if 0 <= index < len(price.values) else None)
    # A FlexMeasures energy price unit reads as `<currency>/<energy unit>`, e.g. `EUR/kWh`.
    return prices, price.unit.partition("/")[0]


def measure(plan: TimeSeries, window: Window, price: TimeSeries | None, import_limit: TimeSeries | None) -> PlanMetrics:
    """
    Summarise one plan over a window.

    Values are consumption-positive throughout, so a positive value is import from the
    grid and a negative one is export to it.

    :param plan:          The scheduled power series.
    :param window:        Window to summarise over.
    :param price:         Day-ahead price series to cost the plan against, if captured.
    :param import_limit:  OpenADR import capacity limit to test the plan against, if captured.
    :returns:             The plan's summary.
    """
    event_starts = window.event_starts()
    powers = [value or 0.0 for value in values_over(plan, window)]
    hours = window.hours_per_interval

    peak_index = max(range(window.length), key=lambda index: powers[index])
    trough_index = min(range(window.length), key=lambda index: powers[index])

    prices, currency = _price_per_kwh(price, window)
    priced = [power * hours * price_value for power, price_value in zip(powers, prices, strict=True) if price_value is not None]

    limits = values_over(import_limit, window) if import_limit is not None else [None] * window.length
    overshoots = [power - limit for power, limit in zip(powers, limits, strict=True) if limit is not None and power - limit > NEGLIGIBLE_KW]

    return PlanMetrics(
        peak_import=powers[peak_index],
        peak_import_at=event_starts[peak_index],
        peak_export=-powers[trough_index],
        peak_export_at=event_starts[trough_index],
        energy_imported=sum(power for power in powers if power > 0) * hours,
        energy_exported=-sum(power for power in powers if power < 0) * hours,
        cost=sum(priced) if priced else None,
        currency=currency if priced else None,
        intervals_over_limit=None if import_limit is None else len(overshoots),
        worst_overshoot=None if import_limit is None else max(overshoots, default=0.0),
        energy_over_limit=None if import_limit is None else sum(overshoots) * hours,
    )


@dataclass(frozen=True, slots=True)
class RoleComparison:
    """
    Before and after, for one role.

    :param role:            Role being compared.
    :param sensor_label:    `asset/sensor` path the values came from.
    :param before:          Summary of the plan without OpenADR.
    :param after:           Summary of the plan with OpenADR.
    :param deltas:          Per-interval change, after minus before, in kW.
    :param energy_shifted:  Energy the second plan moved to other intervals, in kWh.
    """

    role: str
    sensor_label: str
    before: PlanMetrics
    after: PlanMetrics
    deltas: list[float]
    energy_shifted: float

    @property
    def title(self) -> str:
        """Human-readable name of the role."""
        return ROLE_TITLES.get(self.role, self.role)

    @property
    def is_idle(self) -> bool:
        """
        Whether this device stands still in both plans.

        A device the scheduler never dispatches has nothing to say about the OpenADR
        signals, so the report and the figure leave it out rather than showing two flat
        lines on top of each other. Every flexible device in the demo site now carries an
        energy requirement, so this should not normally trigger; it stays as a guard,
        because a device whose requirement happens to fall outside the scheduled window —
        a charge point whose bay is empty all day, say — legitimately stands still.
        """
        return all(metrics.energy_imported == 0 and metrics.energy_exported == 0 for metrics in (self.before, self.after))


@dataclass(frozen=True, slots=True)
class Comparison:
    """
    The full before-and-after story of one demo.

    :param before:        The run made before the OpenADR wiring.
    :param after:         The run made after it.
    :param window:        Intervals the two runs have in common.
    :param roles:         One comparison per role both runs captured.
    :param import_limit:  The OpenADR import capacity limit both plans are held against.
    :param export_limit:  The OpenADR export capacity limit.
    :param price:         The day-ahead price both plans were optimised against.
    """

    before: ScheduleRun
    after: ScheduleRun
    window: Window
    roles: list[RoleComparison]
    import_limit: TimeSeries | None
    export_limit: TimeSeries | None
    price: TimeSeries | None

    @property
    def windows_agree(self) -> bool:
        """Whether both runs scheduled exactly the window that is being compared."""
        return (
            self.before.window_start == self.after.window_start == self.window.start
            and self.before.window_duration == self.after.window_duration == self.window.length * self.window.resolution
        )

    @property
    def active_roles(self) -> list[RoleComparison]:
        """The roles worth showing: every device that moves in at least one of the plans."""
        return [entry for entry in self.roles if not entry.is_idle]

    @property
    def idle_roles(self) -> list[RoleComparison]:
        """The roles the scheduler left standing still in both plans."""
        return [entry for entry in self.roles if entry.is_idle]

    def role(self, role: str) -> RoleComparison | None:
        """
        Return the comparison for one role, if both runs captured it.

        :param role:  One of the module's role constants.
        :returns:     The role comparison, or None.
        """
        return next((entry for entry in self.roles if entry.role == role), None)


def compare(before: ScheduleRun, after: ScheduleRun) -> Comparison:
    """
    Work out what the OpenADR signals changed about the plan.

    :param before:            The run made before the OpenADR wiring.
    :param after:             The run made after it.
    :returns:                 The comparison.
    :raises ComparisonError:  When the two runs share no site-power series to compare.
    """
    before_site = before.plan(SITE_POWER)
    after_site = after.plan(SITE_POWER)
    if before_site is None or after_site is None:
        msg = f"Both runs need a '{SITE_POWER}' series to be comparable; re-run the trigger for whichever run is missing it."
        raise ComparisonError(msg)

    window = overlap(before_site, after_site)
    # The capacity limits belong to the OpenADR-wired run by definition; the price is the
    # same seeded curve in both, so either run's copy will do.
    import_limit = after.plan(IMPORT_LIMIT)
    export_limit = after.plan(EXPORT_LIMIT)
    price = after.plan(PRICE) or before.plan(PRICE)

    roles = []
    for role in PLAN_ROLES:
        before_plan, after_plan = before.plan(role), after.plan(role)
        if before_plan is None or after_plan is None:
            continue
        before_values = [value or 0.0 for value in values_over(before_plan, window)]
        after_values = [value or 0.0 for value in values_over(after_plan, window)]
        deltas = [after_value - before_value for before_value, after_value in zip(before_values, after_values, strict=True)]
        # The OpenADR limit is a limit on the *site's* grid connection, so only the site is
        # held against it. A charge point behind rooftop PV may well draw more than the site
        # limit while the site itself stays under it, so reporting a "breach" for the hub or
        # the heat pump would contradict the site's own line and read as a broken demo.
        role_limit = import_limit if role == SITE_POWER else None
        roles.append(
            RoleComparison(
                role=role,
                sensor_label=after.series[role].sensor_label,
                before=measure(before_plan, window, price, role_limit),
                after=measure(after_plan, window, price, role_limit),
                # Halved because every kWh moved out of one interval reappears in another,
                # so summing the absolute changes would count each move twice.
                deltas=deltas,
                energy_shifted=sum(abs(delta) for delta in deltas) * window.hours_per_interval / 2,
            )
        )

    return Comparison(before=before, after=after, window=window, roles=roles, import_limit=import_limit, export_limit=export_limit, price=price)


def _format_change(before: float | None, after: float | None, unit: str) -> str:
    """
    Render a before/after pair as `before -> after (change)`.

    :param before:  Value without OpenADR, or None when it could not be measured.
    :param after:   Value with OpenADR, or None.
    :param unit:    Unit to append to each number.
    :returns:       A single column of the report.
    """
    if before is None or after is None:
        return "n/a"
    change = after - before
    return f"{before:,.1f} {unit} -> {after:,.1f} {unit} ({change:+,.1f})"


def _render_header(comparison: Comparison) -> list[str]:
    """
    Render what the comparison is of, plus any caveat about it.

    :param comparison:  The comparison to describe.
    :returns:           The opening lines of the report.
    """
    window = comparison.window
    lines = [
        "=" * 78,
        f"OpenADR impact on the plan for {comparison.after.asset_name}",
        "=" * 78,
        f"Window     {window.start.isoformat()} + {window.length} x {window.resolution} ({window.length * window.hours_per_interval:.0f} h)",
        f"Before     triggered {comparison.before.triggered_at.isoformat()}, job {comparison.before.job_id}",
        f"After      triggered {comparison.after.triggered_at.isoformat()}, job {comparison.after.job_id}",
    ]
    if not comparison.windows_agree:
        lines.append("Note       the two runs did not schedule the same window; comparing the overlap only.")
    if comparison.import_limit is None:
        lines.append("Note       the OpenADR run carries no import capacity limit, so no breach counts are shown.")
    elif all(value is None for value in comparison.import_limit.values):
        lines.append(
            "Note       the OpenADR import capacity limit sensor holds no value over this window, so the scheduler had "
            "nothing to respect. Check that seed_events.py ran and that the fetch picked its event up."
        )
    lines.extend(f"Note       {entry.title.lower()} stands still in both plans, so it is left out below." for entry in comparison.idle_roles)
    lines.append("")
    return lines


def _render_role(entry: RoleComparison, window: Window) -> list[str]:
    """
    Render one device's before-and-after numbers.

    :param entry:   The role to describe.
    :param window:  Window the numbers cover.
    :returns:       That role's block of the report.
    """
    lines = [
        f"{entry.title}  ({entry.sensor_label})",
        f"  peak import      {_format_change(entry.before.peak_import, entry.after.peak_import, 'kW')}",
        f"                   before at {entry.before.peak_import_at.isoformat()}, after at {entry.after.peak_import_at.isoformat()}",
        f"  peak export      {_format_change(entry.before.peak_export, entry.after.peak_export, 'kW')}",
        f"  energy imported  {_format_change(entry.before.energy_imported, entry.after.energy_imported, 'kWh')}",
        f"  energy exported  {_format_change(entry.before.energy_exported, entry.after.energy_exported, 'kWh')}",
    ]
    if entry.before.cost is not None and entry.after.cost is not None:
        lines.append(f"  cost at day-ahead prices  {_format_change(entry.before.cost, entry.after.cost, entry.after.currency or '')}")
    if entry.before.intervals_over_limit is not None and entry.after.intervals_over_limit is not None:
        lines.append(f"  intervals above the OpenADR import limit  {entry.before.intervals_over_limit} -> {entry.after.intervals_over_limit} (of {window.length})")
        lines.append(f"  worst breach     {_format_change(entry.before.worst_overshoot, entry.after.worst_overshoot, 'kW')}")
        lines.append(f"  energy above it  {_format_change(entry.before.energy_over_limit, entry.after.energy_over_limit, 'kWh')}")
    lines.append(f"  energy moved to other intervals  {entry.energy_shifted:,.1f} kWh")
    lines.append("")
    return lines


def _render_largest_changes(site: RoleComparison, window: Window) -> list[str]:
    """
    Render the intervals in which the grid connection changed the most.

    :param site:    Comparison of the site's own power.
    :param window:  Window the changes cover.
    :returns:       The closing block of the report.
    """
    event_starts = window.event_starts()
    ranked = sorted(range(window.length), key=lambda index: site.deltas[index])
    lines = [f"Largest changes at the grid connection (after minus before, top {TOP_MOVEMENTS} each way)"]
    for heading, indices in (("  down", ranked[:TOP_MOVEMENTS]), ("  up  ", list(reversed(ranked[-TOP_MOVEMENTS:])))):
        lines.extend(f"{heading}  {event_starts[index].isoformat()}  {site.deltas[index]:+9,.1f} kW" for index in indices if abs(site.deltas[index]) > NEGLIGIBLE_KW)
    lines.append("")
    return lines


def render_report(comparison: Comparison) -> str:
    """
    Render the comparison as plain text.

    This doubles as the figure's table view: every number the chart shows is also written
    out here, so the comparison survives being read in a terminal, a log or a screen reader.

    :param comparison:  The comparison to render.
    :returns:           The report, ready to print.
    """
    lines = _render_header(comparison)
    for entry in comparison.active_roles:
        lines.extend(_render_role(entry, comparison.window))
    site = comparison.role(SITE_POWER)
    if site is not None:
        lines.extend(_render_largest_changes(site, comparison.window))
    return "\n".join(lines)
