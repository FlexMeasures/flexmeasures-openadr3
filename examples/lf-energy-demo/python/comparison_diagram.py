"""
Render a `Comparison` as a self-contained HTML page with an inline SVG diagram.

Follows an editorial diagram-design system rather than a plotting library's defaults:
one restrained accent colour on the focal series (the plan `with OpenADR`), everything
else in muted ink; hairline gridlines instead of a filled plot background; a legend as
a horizontal strip above the panels rather than floating inside them; Instrument Serif
for the page title, Geist for labels, Geist Mono for axis ticks and technical values.

That legend doubles as the page's only control: each plan gets a checkbox that shows or
hides it in every panel at once, so a reader can walk an audience from the plan the site
would have run to the plan OpenADR produced instead. The page opens showing only the
`before` plan. This is done with CSS classes on lines the SVG has already drawn, and a
handful of lines of vanilla JavaScript — the page stays a single self-contained file with
no scripted dependencies, and its default view is correct with JavaScript switched off.

The step lines are still drawn from the real per-interval data — this only replaces how
that data is turned into pixels, not what data is shown, and hiding a plan never changes
a panel's y-axis, which is always scaled to hold both. A step function's corners are
drawn sharp, not as rounded elbows: rounding here would misrepresent a genuine
instantaneous change in power between one quarter hour and the next.

No HTTP, no argument parsing and no arithmetic here — those live in flexmeasures_client.py,
compare_schedules.py and schedule_comparison.py respectively. This module turns a
`Comparison` into markup and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from xml.sax.saxutils import escape

from flexmeasures_client import TimeSeries
from schedule_comparison import (
    IMPORT_LIMIT,
    PRICE,
    ROLE_TITLES,
    SITE_POWER,
    ComparisonError,
)

if TYPE_CHECKING:
    from zoneinfo import ZoneInfo

    from schedule_comparison import Comparison, RoleComparison

# --- Design tokens -------------------------------------------------------------
# The diagram-design skill's default skin (references/style-guide.md), used as-is: this
# is a one-off demo utility, not a branded product surface, so the default editorial
# palette applies rather than a project-specific onboarding pass.

PAPER = "#f5f5f5"
CARD = "#ffffff"
INK = "#2d3142"
MUTED = "#4f5d75"
SOFT = "#7a8399"
RULE = "rgba(45,49,66,0.12)"
RULE_SOLID = "#bfc0c0"
ACCENT = "#eb6c36"
ACCENT_TINT = "rgba(235,108,54,0.10)"

# The two plans differ from the skill's generic "1-2 focal elements" rule only in which
# series counts as focal: `with OpenADR` is the plan being recommended, so it takes the
# single accent colour; `before` is context for comparison, so it stays muted rather than
# competing for attention. The limit is a constraint, not a series, so it takes ink instead
# of either.
COLOUR_BEFORE = MUTED
COLOUR_AFTER = ACCENT
COLOUR_LIMIT = INK
COLOUR_PRICE = INK

FONTS_LINK = "https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=Geist:wght@400;500;600&family=Geist+Mono:wght@400;500;600&display=swap"

# --- The two togglable plans -----------------------------------------------------
# The reader can show or hide either plan, across every panel at once, from the control bar
# the page draws above the diagram. That is done with CSS classes rather than by re-rendering
# anything: each plan's step lines carry a class, and hiding a plan is a class on the `<svg>`
# root. The names below are the single source of truth for those classes, the `data-series`
# attributes, the checkbox states and the CSS rules, so no two of them can drift apart.

SERIES_BEFORE = "before"
SERIES_AFTER = "after"

# Label per togglable plan, in the order the control bar lists them.
SERIES_LABELS = {SERIES_BEFORE: "Before OpenADR", SERIES_AFTER: "With OpenADR"}

# Which plans are on screen before the reader touches anything. Only `before` is: the page
# tells its story by starting from the plan the site would have run, and letting the reader
# lay `with OpenADR` over the top of it. Anything not listed here is hidden on first paint by
# a class on the `<svg>` itself, so the default view is right even with JavaScript disabled —
# the script only has to handle changes.
SERIES_SHOWN_INITIALLY = frozenset({SERIES_BEFORE})

DIAGRAM_ELEMENT_ID = "comparison-diagram"

# --- Canvas ----------------------------------------------------------------------
# A 4px grid throughout, per the skill's layout convention.

CANVAS_WIDTH = 1200
MARGIN_LEFT = 72
MARGIN_RIGHT = 32
MARGIN_TOP = 16
MARGIN_BOTTOM = 16
PANEL_TITLE_HEIGHT = 28
PANEL_GAP = 28
X_AXIS_HEIGHT = 28

# Plot-area height per kind of panel: the site's own grid connection is the headline, the
# two flexible groups show where the change came from, and price is context underneath.
SITE_PANEL_HEIGHT = 260
DEVICE_PANEL_HEIGHT = 140
PRICE_PANEL_HEIGHT = 120

Y_TICK_COUNT = 5
X_TICK_INTERVAL = timedelta(hours=3)

# Breathing room above and below the plans, as a fraction of their own span. A panel whose
# two plans barely move (or a price panel, whose whole domain is a fraction of a kW-sized
# unit) still needs a visible margin, sized as a fraction of the values' own magnitude
# rather than a fixed unit-specific constant — a margin sized for kW would swamp a
# EUR/kWh price panel, and the reverse would vanish on a power panel.
PANEL_MARGIN_FRACTION = 0.12
PANEL_MINIMUM_MARGIN_FRACTION = 0.08
PANEL_MINIMUM_MARGIN_ABSOLUTE = 0.01

# Below this, a capacity limit is flat enough over the window that shading it would just
# wash the whole panel rather than pointing at anything.
NEGLIGIBLE_LIMIT_RANGE_KW = 0.5


@dataclass(frozen=True, slots=True)
class Scale:
    """
    Maps a value in data units onto a pixel position within one panel's plot area.

    :param domain_low:   Lowest value the scale has to cover.
    :param domain_high:  Highest value the scale has to cover.
    :param pixel_low:    Pixel position `domain_low` maps to (the panel's bottom edge).
    :param pixel_high:   Pixel position `domain_high` maps to (the panel's top edge).
    """

    domain_low: float
    domain_high: float
    pixel_low: float
    pixel_high: float

    def __call__(self, value: float) -> float:
        """Map one value onto its pixel position."""
        span = self.domain_high - self.domain_low
        if span == 0:
            return (self.pixel_low + self.pixel_high) / 2
        fraction = (value - self.domain_low) / span
        return self.pixel_low + fraction * (self.pixel_high - self.pixel_low)


def _value_scale(values: list[float], top: float, bottom: float) -> Scale:
    """
    Build a y-scale that comfortably fits a set of values.

    :param values:  Every value the scale has to cover; must be non-empty.
    :param top:     Pixel y of the panel's top edge.
    :param bottom:  Pixel y of the panel's bottom edge.
    :returns:       A scale from data units to pixels, low value at the bottom.
    """
    lowest, highest = min(values), max(values)
    magnitude = max(abs(lowest), abs(highest), PANEL_MINIMUM_MARGIN_ABSOLUTE)
    margin = max(PANEL_MARGIN_FRACTION * (highest - lowest), PANEL_MINIMUM_MARGIN_FRACTION * magnitude)
    return Scale(domain_low=lowest - margin, domain_high=highest + margin, pixel_low=bottom, pixel_high=top)


def _time_scale(window_start: datetime, window_end: datetime, left: float, right: float) -> Scale:
    """
    Build an x-scale across the compared window.

    :param window_start:  Start of the window, timezone-aware.
    :param window_end:    End of the window, timezone-aware.
    :param left:          Pixel x of the panel's left edge.
    :param right:         Pixel x of the panel's right edge.
    :returns:              A scale from a POSIX timestamp to pixels.
    """
    return Scale(domain_low=window_start.timestamp(), domain_high=window_end.timestamp(), pixel_low=left, pixel_high=right)


def _at(time_scale: Scale, moment: datetime) -> float:
    """Map one moment onto its pixel x, through a time scale built on POSIX timestamps."""
    return time_scale(moment.timestamp())


def _runs(values: list[float | None]) -> list[list[int]]:
    """
    Group a series' indices into contiguous runs with no gap between them.

    A step line is drawn per run rather than across the whole series, so a sensor gap
    shows as a visible break instead of a line quietly jumping across it.

    :param values:  The series' values, in order.
    :returns:       Index runs, each strictly increasing and gap-free.
    """
    runs: list[list[int]] = []
    current: list[int] = []
    for index, value in enumerate(values):
        if value is None:
            if current:
                runs.append(current)
                current = []
            continue
        current.append(index)
    if current:
        runs.append(current)
    return runs


def _step_paths(series: TimeSeries, window_end: datetime, time_scale: Scale, value_scale: Scale) -> list[str]:
    """
    Render one series as one or more SVG step-line paths, in `steps-post` shape.

    Each value holds from its own event start until the next one (or, for the series'
    final value, until `window_end`), matching how a quarter-hourly plan is actually
    defined. Sharp corners, not rounded — the value genuinely changes in a single step.

    :param series:      The series to render.
    :param window_end:  End of the compared window, so the final step has somewhere to end.
    :param time_scale:  Scale from event start to pixel x.
    :param value_scale: Scale from value to pixel y.
    :returns:           One `<path>` `d` attribute per contiguous run of real values.
    """
    starts = list(series.event_starts())
    paths = []
    for run in _runs(series.values):
        x0 = _at(time_scale, starts[run[0]])
        y0 = value_scale(series.values[run[0]])
        commands = [f"M {x0:.1f} {y0:.1f}"]
        for position in run[1:]:
            x = _at(time_scale, starts[position])
            commands.append(f"H {x:.1f}")
            commands.append(f"V {value_scale(series.values[position]):.1f}")
        # Close the final step out to where it stops holding: the next event after the
        # run, or the window's own end when the run reaches the last event.
        last = run[-1]
        run_end = starts[last + 1] if last + 1 < len(starts) else window_end
        commands.append(f"H {_at(time_scale, run_end):.1f}")
        paths.append(" ".join(commands))
    return paths


def _line_markup(
    series: TimeSeries, window_end: datetime, time_scale: Scale, value_scale: Scale, colour: str, width: float, *, dash: str | None = None, css_class: str | None = None
) -> str:
    """
    Render a whole series as one or more `<path>` elements.

    :param series:      The series to render.
    :param window_end:  End of the compared window, so the final step has somewhere to end.
    :param time_scale:  Scale from event start to pixel x.
    :param value_scale: Scale from value to pixel y.
    :param colour:      Stroke colour.
    :param width:       Stroke width, in px.
    :param dash:        `stroke-dasharray` value, or None for a solid line.
    :param css_class:   Class to put on every path, so the page's control bar can show or
                        hide the whole series at once, or None for an untoggled line.
    :returns:            Markup for every contiguous run of real values in the series.
    """
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    class_attr = f' class="{css_class}"' if css_class else ""
    return "".join(
        f'<path d="{path}" fill="none" stroke="{colour}" stroke-width="{width}" stroke-linejoin="round"{dash_attr}{class_attr}/>'
        for path in _step_paths(series, window_end, time_scale, value_scale)
    )


def _series_class(series: str) -> str:
    """
    Return the class one plan's step lines carry, in every panel.

    :param series:  `SERIES_BEFORE` or `SERIES_AFTER`.
    :returns:       The class name.
    """
    return f"series-{series}"


def _hidden_class(series: str) -> str:
    """
    Return the class that, on the `<svg>` root, hides one plan across every panel.

    :param series:  `SERIES_BEFORE` or `SERIES_AFTER`.
    :returns:       The class name.
    """
    return f"hide-{series}"


def _y_ticks(value_scale: Scale, count: int = Y_TICK_COUNT) -> list[float]:
    """
    Choose evenly spaced gridline values covering a value scale's domain.

    :param value_scale:  The panel's y-scale.
    :param count:        How many gridlines to place.
    :returns:            Gridline values, evenly spaced, low to high.
    """
    span = value_scale.domain_high - value_scale.domain_low
    return [value_scale.domain_low + span * step / (count - 1) for step in range(count)]


def _x_ticks(window_start: datetime, window_end: datetime, timezone: ZoneInfo) -> list[datetime]:
    """
    Choose evenly spaced tick moments covering a window, aligned to the tick interval.

    :param window_start:  Start of the window, timezone-aware.
    :param window_end:    End of the window.
    :param timezone:      Timezone to align ticks to, so they land on round local hours.
    :returns:              Tick moments, in order.
    """
    local_start = window_start.astimezone(timezone)
    aligned = local_start.replace(minute=0, second=0, microsecond=0)
    hours_to_next_interval = (-aligned.hour) % (X_TICK_INTERVAL // timedelta(hours=1))
    first = aligned + timedelta(hours=hours_to_next_interval)
    ticks = []
    tick = first
    while tick <= window_end:
        if tick >= window_start:
            ticks.append(tick)
        tick += X_TICK_INTERVAL
    return ticks


def _text(x: float, y: float, content: str, *, size: int, colour: str, family: str = "Geist", weight: int = 400, anchor: str = "middle", tracking: float | None = None) -> str:
    """
    Render one text element with the design system's font roles.

    :param x:         Pixel x of the anchor point.
    :param y:         Pixel y of the text baseline.
    :param content:   Text to render; escaped for safe embedding.
    :param size:      Font size in px.
    :param colour:    Fill colour.
    :param family:    `Geist` (sans, names/titles), `Geist Mono` (technical/axis) or
                       `Instrument Serif` (page title only).
    :param weight:    Font weight.
    :param anchor:    SVG `text-anchor`.
    :param tracking:  Letter-spacing in em, for the small-caps eyebrow style.
    :returns:          One `<text>` element.
    """
    letter_spacing = f' letter-spacing="{tracking}em"' if tracking is not None else ""
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" fill="{colour}" font-size="{size}" font-weight="{weight}" '
        f"font-family=\"'{family}', {'monospace' if 'Mono' in family else 'sans-serif'}\" "
        f'text-anchor="{anchor}"{letter_spacing}>{escape(content)}</text>'
    )


def _panel_heights(comparison: Comparison) -> list[tuple[RoleComparison | None, float]]:
    """
    Decide which panels to draw and how tall each plot area should be.

    :param comparison:  The comparison being drawn.
    :returns:            `(role, plot_height)` for every panel, site first, price (role=None) last.
    """
    panels: list[tuple[RoleComparison | None, float]] = [(entry, SITE_PANEL_HEIGHT if entry.role == SITE_POWER else DEVICE_PANEL_HEIGHT) for entry in comparison.active_roles]
    panels.append((None, PRICE_PANEL_HEIGHT))
    return panels


def _draw_shading(comparison: Comparison, time_scale: Scale, top: float, bottom: float) -> str:
    """
    Wash the intervals in which the OpenADR import limit is tightened, on one panel.

    :param comparison:  The comparison being drawn.
    :param time_scale:  The panel's x-scale.
    :param top:         Pixel y of the panel's top edge.
    :param bottom:      Pixel y of the panel's bottom edge.
    :returns:            Rect markup for every shaded interval, possibly empty.
    """
    limit = comparison.import_limit
    if limit is None:
        return ""
    known = [value for value in limit.values if value is not None]
    if not known or max(known) - min(known) < NEGLIGIBLE_LIMIT_RANGE_KW:
        return ""
    ceiling = max(known)
    rects = []
    for event_start, value in zip(limit.event_starts(), limit.values, strict=True):
        if value is None or value >= ceiling:
            continue
        x0 = _at(time_scale, event_start)
        x1 = _at(time_scale, event_start + limit.resolution)
        rects.append(f'<rect x="{x0:.1f}" y="{top:.1f}" width="{x1 - x0:.1f}" height="{bottom - top:.1f}" fill="{RULE}"/>')
    return "".join(rects)


def _draw_role_panel(entry: RoleComparison, comparison: Comparison, top: float, height: float, time_scale: Scale, timezone: ZoneInfo) -> str:
    """
    Draw one device panel: shading, gridlines, both plans, the limits (site only), and its own x-axis.

    :param entry:        The role being drawn.
    :param comparison:   The comparison being drawn.
    :param top:          Pixel y of the panel's top edge.
    :param height:       Pixel height of the panel's plot area.
    :param time_scale:   The shared x-scale.
    :param timezone:     Timezone the x-axis is expressed in.
    :returns:             SVG markup for the whole panel.
    """
    bottom = top + height
    before_plan, after_plan = comparison.before.plan(entry.role), comparison.after.plan(entry.role)
    if before_plan is None or after_plan is None:
        # comparison.active_roles only ever holds roles both runs captured a plan for, so this
        # signals a caller bug (drawing a role outside that list) rather than missing demo data.
        msg = f"Role '{entry.role}' is missing a plan in one of the two runs; cannot draw its panel."
        raise ComparisonError(msg)
    values = [value for plan in (before_plan, after_plan) for value in plan.values if value is not None]
    value_scale = _value_scale(values, top, bottom)
    window_end = comparison.window.end

    parts = [_draw_shading(comparison, time_scale, top, bottom)]
    parts.append(_draw_gridlines(value_scale, time_scale))
    if value_scale.domain_low < 0 < value_scale.domain_high:
        zero_y = value_scale(0)
        parts.append(f'<line x1="{time_scale.pixel_low:.1f}" y1="{zero_y:.1f}" x2="{time_scale.pixel_high:.1f}" y2="{zero_y:.1f}" stroke="{RULE_SOLID}" stroke-width="0.8"/>')

    # Classed so the page's control bar can show or hide either plan in every panel at once.
    # The y-scale above is deliberately built from both plans whatever is on screen, so
    # toggling a plan off never rescales the axis underneath the one still showing.
    parts.append(_line_markup(before_plan, window_end, time_scale, value_scale, COLOUR_BEFORE, 1.4, css_class=_series_class(SERIES_BEFORE)))
    parts.append(_line_markup(after_plan, window_end, time_scale, value_scale, COLOUR_AFTER, 2.0, css_class=_series_class(SERIES_AFTER)))

    if entry.role == SITE_POWER:
        parts.append(_draw_limit(comparison.import_limit, time_scale, value_scale, mirrored=False))
        parts.append(_draw_limit(comparison.export_limit, time_scale, value_scale, mirrored=True))

    parts.append(_text(MARGIN_LEFT, top - 8, f"{entry.title}  ({entry.sensor_label})  · kW", size=13, colour=INK, weight=600, anchor="start"))
    parts.append(_draw_x_axis(time_scale, bottom, timezone))
    return "".join(parts)


def _draw_price_panel(comparison: Comparison, top: float, height: float, time_scale: Scale, timezone: ZoneInfo) -> str:
    """
    Draw the day-ahead price panel: what the plan without OpenADR was chasing.

    :param comparison:  The comparison being drawn.
    :param top:         Pixel y of the panel's top edge.
    :param height:      Pixel height of the panel's plot area.
    :param time_scale:  The shared x-scale.
    :param timezone:    Timezone the x-axis is expressed in.
    :returns:            SVG markup for the panel.
    """
    bottom = top + height
    price = comparison.price
    values = [value for value in (price.values if price is not None else []) if value is not None]
    value_scale = _value_scale(values or [0.0, 1.0], top, bottom)

    parts = [_draw_shading(comparison, time_scale, top, bottom)]
    parts.append(_draw_gridlines(value_scale, time_scale))
    if price is not None:
        parts.append(_line_markup(price, comparison.window.end, time_scale, value_scale, COLOUR_PRICE, 1.4))
    unit = f"  · {price.unit}" if price is not None else ""
    title = f"{ROLE_TITLES[PRICE]}  (what the plan without OpenADR was chasing){unit}"
    parts.append(_text(MARGIN_LEFT, top - 8, title, size=13, colour=INK, weight=600, anchor="start"))
    parts.append(_draw_x_axis(time_scale, bottom, timezone))
    return "".join(parts)


# Domain-span thresholds below which gridline labels need another decimal place to stay
# distinguishable from each other, e.g. a price panel's 0.02-0.28 EUR/kWh domain.
TICK_DECIMALS_BELOW_SPAN = {1: 2, 10: 1}


def _tick_decimals(value_scale: Scale) -> int:
    """
    Choose how many decimal places a panel's gridline labels need.

    A price panel's whole domain can sit inside a single kW-sized integer (0.02 to 0.28
    EUR/kWh), so a fixed zero-decimal format would round every tick to 0 or -0. Deciding
    from the domain's own span, rather than hardcoding it per panel, keeps this correct for
    any future panel without having to know its unit.

    :param value_scale:  The panel's y-scale.
    :returns:             Decimal places wide enough to distinguish adjacent gridlines.
    """
    span = value_scale.domain_high - value_scale.domain_low
    for threshold, decimals in sorted(TICK_DECIMALS_BELOW_SPAN.items()):
        if span < threshold:
            return decimals
    return 0


def _draw_gridlines(value_scale: Scale, time_scale: Scale) -> str:
    """
    Draw a panel's horizontal gridlines and their value labels.

    :param value_scale:  The panel's y-scale.
    :param time_scale:   The panel's x-scale, for the gridlines' horizontal extent.
    :returns:             SVG markup for the gridlines and their labels.
    """
    decimals = _tick_decimals(value_scale)
    parts = []
    for value in _y_ticks(value_scale):
        y = value_scale(value)
        parts.append(f'<line x1="{time_scale.pixel_low:.1f}" y1="{y:.1f}" x2="{time_scale.pixel_high:.1f}" y2="{y:.1f}" stroke="{RULE}" stroke-width="0.8"/>')
        parts.append(_text(MARGIN_LEFT - 12, y + 3, _format_tick(value, decimals), size=9, colour=SOFT, family="Geist Mono", anchor="end"))
    return "".join(parts)


def _format_tick(value: float, decimals: int) -> str:
    """
    Render one gridline value, without a `-0.00`-style negative-zero artefact.

    A tick that rounds to zero at the panel's own precision can still be a hair below zero
    (the margin can push a domain slightly past the real data), which `f"{value:.2f}"`
    would render as `-0.00` — technically correct, but reads as a mistake.

    :param value:     The value to render.
    :param decimals:  Decimal places, from `_tick_decimals`.
    :returns:          The formatted value, with `-0` folded into `0`.
    """
    formatted = f"{value:,.{decimals}f}"
    return formatted.removeprefix("-") if formatted.lstrip("-") == f"{0:,.{decimals}f}" else formatted


def _draw_x_axis(time_scale: Scale, bottom: float, timezone: ZoneInfo) -> str:
    """
    Draw one panel's x-axis ticks below its plot area.

    Every panel draws its own, rather than only the bottom-most one, so a reader looking at
    any single panel can read times off it directly instead of tracing back down to the
    price panel underneath it.

    :param time_scale:  The shared x-scale.
    :param bottom:      Pixel y of this panel's bottom edge.
    :param timezone:    Timezone to format tick labels in.
    :returns:            SVG markup for the tick labels.
    """
    window_start = datetime.fromtimestamp(time_scale.domain_low, tz=timezone)
    window_end = datetime.fromtimestamp(time_scale.domain_high, tz=timezone)
    parts = []
    for tick in _x_ticks(window_start, window_end, timezone):
        x = _at(time_scale, tick)
        parts.append(_text(x, bottom + 20, f"{tick.astimezone(timezone):%a %H:%M}", size=9, colour=SOFT, family="Geist Mono"))
    return "".join(parts)


def _draw_limit(limit: TimeSeries | None, time_scale: Scale, value_scale: Scale, *, mirrored: bool) -> str:
    """
    Draw one OpenADR capacity limit as a dashed threshold line, above both plans.

    Drawn after (so visually on top of) the plan lines, and in ink rather than either
    plan's colour: the `with OpenADR` plan is routinely pinned exactly on this limit
    inside the curtailment window — that coincidence is the whole point of the chart —
    and a line drawn first, underneath, would simply vanish beneath the solid plan line
    painted on top of it.

    :param limit:      The limit series, or None when the run captured none.
    :param time_scale:  The panel's x-scale.
    :param value_scale: The panel's y-scale.
    :param mirrored:    True for an export limit, stored positive but drawn on the
                        negative side of an import-positive panel.
    :returns:            SVG markup for the threshold line, possibly empty.
    """
    if limit is None:
        return ""
    sign = -1 if mirrored else 1
    window_end = limit.start + len(limit.values) * limit.resolution
    mirrored_values = [None if value is None else sign * value for value in limit.values]
    series = TimeSeries(start=limit.start, resolution=limit.resolution, unit=limit.unit, values=mirrored_values)
    visible = [value for value in mirrored_values if value is not None and value_scale.domain_low < value < value_scale.domain_high]
    if not visible:
        return ""
    return _line_markup(series, window_end, time_scale, value_scale, COLOUR_LIMIT, 2.0, dash="7,4")


def _join_titles(titles: list[str]) -> str:
    """
    Join panel titles into an English list, for the diagram's accessible description.

    :param titles:  Titles to join, in the order they are drawn.
    :returns:       `a`, `a and b`, or `a, b and c`; empty for no titles.
    """
    if not titles:
        return ""
    leading, final = titles[:-1], titles[-1]
    return f"{', '.join(leading)} and {final}" if leading else final


def render_svg(comparison: Comparison, timezone: ZoneInfo, slug: str) -> tuple[str, int]:
    """
    Render the whole diagram as one accessible, self-contained SVG.

    :param comparison:  The comparison to draw.
    :param timezone:    Timezone to express the x-axis in.
    :param slug:        Unique id prefix for this diagram's `<title>`/`<desc>`.
    :returns:            The SVG markup, and the total height it needs.
    """
    panels = _panel_heights(comparison)
    time_scale = _time_scale(comparison.window.start, comparison.window.end, MARGIN_LEFT, CANVAS_WIDTH - MARGIN_RIGHT)

    body = []
    cursor: float = MARGIN_TOP
    for entry, plot_height in panels:
        cursor += PANEL_TITLE_HEIGHT
        if entry is None:
            body.append(_draw_price_panel(comparison, cursor, plot_height, time_scale, timezone))
        else:
            body.append(_draw_role_panel(entry, comparison, cursor, plot_height, time_scale, timezone))
        cursor += plot_height
        # Every panel carries its own x-axis now, not just the bottom-most one, so a reader
        # can read times off any panel without tracing back up to the price panel underneath.
        cursor += X_AXIS_HEIGHT
        cursor += PANEL_GAP

    # The legend is not drawn here: it lives in the surrounding page as a control bar, because
    # two of its three entries are checkboxes and SVG has no checkbox of its own.
    total_height = cursor + MARGIN_BOTTOM

    # Named from the roles actually drawn rather than from a fixed sentence, so a run in which
    # the scheduler left a device standing still (and _panel_heights therefore drew no panel
    # for it) is not described as showing a panel that is not there.
    panel_titles = [entry.title for entry in comparison.active_roles]
    panel_titles.append(f"{ROLE_TITLES[PRICE]}, which is what the plan without OpenADR was optimising against")
    # Escaped once, by the caller below: pre-escaping here as well would render an asset named
    # `A&B` as `A&amp;amp;B`.
    title = f"What OpenADR changed about the plan for {comparison.after.asset_name}"
    desc = (
        f"Line chart comparing a FlexMeasures schedule for {comparison.after.asset_name} triggered before and after an "
        f"OpenADR capacity limit was wired into its flex-context, in stacked panels showing {_join_titles(panel_titles)}. "
        "Either plan can be shown or hidden with the checkboxes above the chart."
    )
    hidden = " ".join(_hidden_class(series) for series in SERIES_LABELS if series not in SERIES_SHOWN_INITIALLY)
    svg = (
        f'<svg id="{DIAGRAM_ELEMENT_ID}" class="{hidden}" role="img" aria-labelledby="{slug}-title {slug}-desc" '
        f'viewBox="0 0 {CANVAS_WIDTH} {total_height:.0f}" '
        f'xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto;">'
        f'<title id="{slug}-title">{escape(title)}</title>'
        f'<desc id="{slug}-desc">{escape(desc)}</desc>'
        f'<rect width="{CANVAS_WIDTH}" height="{total_height:.0f}" fill="{PAPER}"/>' + "".join(body) + "</svg>"
    )
    return svg, int(total_height)


@dataclass(frozen=True, slots=True)
class SummaryStat:
    """One number worth putting in a headline card, with its own framing."""

    eyebrow: str
    headline: str
    detail: str


def _summary_stats(comparison: Comparison) -> list[SummaryStat]:
    """
    Pick the three numbers that best summarise what OpenADR changed.

    :param comparison:  The comparison to summarise.
    :returns:            Three stats, varied in what they measure, for the card row.
    """
    site = comparison.role(SITE_POWER)
    stats = []
    if site is not None:
        stats.append(
            SummaryStat(
                "PEAK GRID IMPORT",
                f"{site.before.peak_import:,.0f} → {site.after.peak_import:,.0f} kW",
                f"{site.after.peak_import - site.before.peak_import:+,.0f} kW at the grid connection",
            )
        )
        if site.before.intervals_over_limit is not None and site.after.intervals_over_limit is not None:
            stats.append(
                SummaryStat(
                    "INTERVALS OVER THE OPENADR LIMIT",
                    f"{site.before.intervals_over_limit} → {site.after.intervals_over_limit}",
                    f"of {comparison.window.length}, {site.before.energy_over_limit or 0:,.0f} → {site.after.energy_over_limit or 0:,.0f} kWh above it",
                )
            )
        stats.append(
            SummaryStat(
                "ENERGY MOVED",
                f"{site.energy_shifted:,.0f} kWh",
                "shifted to other intervals at the grid connection",
            )
        )
    return stats


def _control_bar(comparison: Comparison) -> str:
    """
    Render the legend as an HTML control bar: a checkbox per togglable plan.

    In the page rather than in the SVG, because two of the three entries have to be
    checkboxes and SVG has none — so the legend and the controls would otherwise be two
    separate things saying the same thing twice. The capacity limit keeps a plain swatch: it
    is the constraint both plans are held against, not one of the plans, so it is context
    that stays on screen rather than something to switch off.

    :param comparison:  The comparison being drawn, to know whether a limit was captured.
    :returns:            Markup for the control bar.
    """
    filters = [
        f'<label class="filter"><input type="checkbox" id="toggle-{series}" data-series="{series}"'
        f"{' checked' if series in SERIES_SHOWN_INITIALLY else ''}>"
        f'<span class="swatch swatch-{series}"></span>{escape(label)}</label>'
        for series, label in SERIES_LABELS.items()
    ]
    if comparison.import_limit is not None or comparison.export_limit is not None:
        filters.append(f'<span class="filter static"><span class="swatch swatch-limit"></span>{escape(ROLE_TITLES[IMPORT_LIMIT])}</span>')
    return f'<div class="filters">{"".join(filters)}</div>'


def _toggle_rules() -> str:
    """
    Render the CSS that hides a plan when its class is on the `<svg>` root.

    Generated from SERIES_LABELS rather than written out, so a plan cannot be given a
    checkbox without also being given the rule that makes the checkbox do anything.

    :returns:  One CSS rule per togglable plan.
    """
    return "\n".join(f"  #{DIAGRAM_ELEMENT_ID}.{_hidden_class(series)} .{_series_class(series)} {{ display: none; }}" for series in SERIES_LABELS)


def render_html(comparison: Comparison, timezone: ZoneInfo) -> str:
    """
    Render the whole comparison as a self-contained HTML page.

    :param comparison:  The comparison to draw.
    :param timezone:    Timezone to express the x-axis and the header's dates in — the
                        demo site's own timezone, passed in rather than hardcoded here so
                        this module stays free of the sys.path trick hierarchy.py needs.
    :returns:            A complete HTML document as a string.
    """
    slug = "openadr-comparison"
    svg, _height = render_svg(comparison, timezone, slug)

    window = comparison.window
    subtitle = f"{window.start.astimezone(timezone):%a %d %b %H:%M} - {window.end.astimezone(timezone):%a %d %b %H:%M} ({window.length} x {window.resolution})"
    stats_html = "".join(
        f'<div class="card"><p class="eyebrow">{escape(stat.eyebrow)}</p><p class="headline">{escape(stat.headline)}</p><p class="detail">{escape(stat.detail)}</p></div>'
        for stat in _summary_stats(comparison)
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>What OpenADR changed about the plan for {escape(comparison.after.asset_name)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="{FONTS_LINK}" rel="stylesheet">
<style>
  :root {{
    --paper: {PAPER}; --card: {CARD}; --ink: {INK}; --muted: {MUTED}; --soft: {SOFT};
    --rule: {RULE}; --accent: {ACCENT}; --accent-tint: {ACCENT_TINT};
    --before: {COLOUR_BEFORE}; --after: {COLOUR_AFTER}; --limit: {COLOUR_LIMIT};
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; background: var(--paper); color: var(--ink);
    font-family: 'Geist', sans-serif; padding: 40px 48px 24px;
  }}
  .eyebrow {{
    font-family: 'Geist Mono', monospace; font-size: 11px; letter-spacing: 0.14em;
    text-transform: uppercase; color: var(--soft); margin: 0 0 8px;
  }}
  h1 {{ font-family: 'Instrument Serif', serif; font-weight: 400; font-size: 2rem; margin: 0 0 4px; }}
  .subtitle {{ color: var(--muted); font-size: 14px; margin: 0 0 28px; }}
  .cards {{ display: grid; grid-template-columns: 1.1fr 1fr 0.9fr; gap: 16px; margin-bottom: 32px; }}
  .card {{ background: var(--card); border: 1px solid var(--rule); border-radius: 6px; padding: 1.25rem; }}
  .card .eyebrow {{ margin-bottom: 10px; }}
  .card .headline {{ font-size: 22px; font-weight: 600; margin: 0 0 6px; }}
  .card .detail {{ font-size: 12px; color: var(--muted); margin: 0; }}
  .filters {{
    display: flex; flex-wrap: wrap; align-items: center; gap: 8px 28px;
    padding-bottom: 12px; margin-bottom: 12px; border-bottom: 1px solid var(--rule);
  }}
  .filter {{
    display: inline-flex; align-items: center; gap: 8px; cursor: pointer;
    font-family: 'Geist Mono', monospace; font-size: 10px; color: var(--ink);
  }}
  .filter.static {{ cursor: default; color: var(--muted); }}
  .filter input {{ width: 13px; height: 13px; margin: 0; accent-color: var(--accent); cursor: pointer; }}
  .swatch {{ width: 24px; height: 0; border-top: 2px solid transparent; }}
  .swatch-before {{ border-top-color: var(--before); }}
  .swatch-after {{ border-top-color: var(--after); }}
  .swatch-limit {{ border-top-color: var(--limit); border-top-style: dashed; }}
{_toggle_rules()}
  .diagram {{ margin-bottom: 24px; }}
  footer {{
    border-top: 1px solid var(--rule); padding-top: 12px; font-family: 'Geist Mono', monospace;
    font-size: 10px; color: var(--soft);
  }}
</style>
</head>
<body>
  <p class="eyebrow">LF ENERGY DEMO WALKTHROUGH</p>
  <h1>What OpenADR changed about the plan for {escape(comparison.after.asset_name)}</h1>
  <p class="subtitle">{escape(subtitle)}</p>
  <div class="cards">{stats_html}</div>
  {_control_bar(comparison)}
  <div class="diagram">{svg}</div>
  <footer>Before job {escape(comparison.before.job_id)} · After job {escape(comparison.after.job_id)} · generated by compare_schedules.py</footer>
  <script>
    document.querySelectorAll('.filters input[type="checkbox"]').forEach(function (checkbox) {{
      checkbox.addEventListener('change', function () {{
        document.getElementById('{DIAGRAM_ELEMENT_ID}').classList.toggle('hide-' + checkbox.dataset.series, !checkbox.checked);
      }});
    }});
  </script>
</body>
</html>
"""
