from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from xml.sax.saxutils import escape

if TYPE_CHECKING:
    from zoneinfo import ZoneInfo

    from openadr3_client.oadr310.models.event.event import ExistingEvent
    from openadr3_client.oadr310.models.event.event_payload import EventPayloadType

# --- Design tokens -------------------------------------------------------------
# Matches comparison_diagram.py's skin, so a reader who has already opened
# ../schedule-runs/comparison.html finds the same look here.

PAPER = "#f5f5f5"
INK = "#2d3142"
MUTED = "#4f5d75"
SOFT = "#7a8399"
RULE = "rgba(45,49,66,0.12)"
RULE_SOLID = "#bfc0c0"
ACCENT = "#eb6c36"

FONTS_LINK = "https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=Geist:wght@400;500;600&family=Geist+Mono:wght@400;500;600&display=swap"

DIAGRAM_ELEMENT_ID = "event-diagram"

CANVAS_WIDTH = 1200
MARGIN_LEFT = 72
MARGIN_RIGHT = 32
MARGIN_TOP = 16
MARGIN_BOTTOM = 16
PANEL_TITLE_HEIGHT = 28
PANEL_GAP = 28
X_AXIS_HEIGHT = 28
PANEL_HEIGHT = 200

Y_TICK_COUNT = 5
X_TICK_INTERVAL = timedelta(hours=3)

# Breathing room above and below a panel's line, as a fraction of its own span -- sized
# off the values' own magnitude rather than a fixed unit-specific constant, so a panel
# whose line barely moves still gets a visible margin.
PANEL_MARGIN_FRACTION = 0.12
PANEL_MINIMUM_MARGIN_FRACTION = 0.08
PANEL_MINIMUM_MARGIN_ABSOLUTE = 0.01


@dataclass(frozen=True, slots=True)
class PayloadPoint:
    """One interval's value for a single payload type."""

    start: datetime
    duration: timedelta
    value: float


@dataclass(frozen=True, slots=True)
class PayloadSeries:
    """
    One payload type's values across every interval of an event, in order.

    :param payload_type:  The OpenADR payload type this series holds, e.g.
                           `IMPORT_CAPACITY_LIMIT`.
    :param unit:          The payload's unit, taken from the event's payload
                           descriptors, or None if it declared none.
    :param points:        The payload's value at each interval, sorted by start.
    """

    payload_type: EventPayloadType
    unit: str | None
    points: tuple[PayloadPoint, ...]

    @property
    def window_end(self) -> datetime:
        """End of the series' last interval."""
        last = self.points[-1]
        return last.start + last.duration


def payload_series(event: ExistingEvent) -> tuple[PayloadSeries, ...]:
    """
    Group an event's interval payloads by payload type, one series per type.

    Only the first value of a payload is plotted: every payload type this walkthrough
    seeds (IMPORT/EXPORT_CAPACITY_LIMIT) carries exactly one.

    :param event:  The event to read intervals from.
    :returns:      One series per payload type that appears in an interval, in the
                    order the event's own payload_descriptors list them.
    """
    units = {descriptor.payload_type: descriptor.units.value if descriptor.units is not None else None for descriptor in event.payload_descriptors or ()}
    order = list(units)
    points: dict[EventPayloadType, list[PayloadPoint]] = {}
    for interval in event.intervals or ():
        period = interval.interval_period or event.interval_period
        if period is None:
            continue
        for payload in interval.payloads:
            points.setdefault(payload.type, []).append(PayloadPoint(start=period.start, duration=period.duration, value=float(payload.values[0])))
            if payload.type not in order:
                order.append(payload.type)
    return tuple(
        PayloadSeries(payload_type=payload_type, unit=units.get(payload_type), points=tuple(sorted(points[payload_type], key=lambda point: point.start)))
        for payload_type in order
        if points.get(payload_type)
    )


@dataclass(frozen=True, slots=True)
class Scale:
    """Maps a value in data units onto a pixel position within one panel's plot area."""

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
    lowest, highest = min(values), max(values)
    magnitude = max(abs(lowest), abs(highest), PANEL_MINIMUM_MARGIN_ABSOLUTE)
    margin = max(PANEL_MARGIN_FRACTION * (highest - lowest), PANEL_MINIMUM_MARGIN_FRACTION * magnitude)
    return Scale(domain_low=lowest - margin, domain_high=highest + margin, pixel_low=bottom, pixel_high=top)


def _time_scale(window_start: datetime, window_end: datetime, left: float, right: float) -> Scale:
    return Scale(domain_low=window_start.timestamp(), domain_high=window_end.timestamp(), pixel_low=left, pixel_high=right)


def _at(time_scale: Scale, moment: datetime) -> float:
    return time_scale(moment.timestamp())


def _step_path(series: PayloadSeries, time_scale: Scale, value_scale: Scale) -> str:
    """Render one series as a single `steps-post` SVG path `d` attribute."""
    points = series.points
    x0 = _at(time_scale, points[0].start)
    y0 = value_scale(points[0].value)
    commands = [f"M {x0:.1f} {y0:.1f}"]
    for point in points[1:]:
        commands.append(f"H {_at(time_scale, point.start):.1f}")
        commands.append(f"V {value_scale(point.value):.1f}")
    commands.append(f"H {_at(time_scale, series.window_end):.1f}")
    return " ".join(commands)


def _text(x: float, y: float, content: str, *, size: int, colour: str, family: str = "Geist", weight: int = 400, anchor: str = "middle") -> str:
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" fill="{colour}" font-size="{size}" font-weight="{weight}" '
        f"font-family=\"'{family}', {'monospace' if 'Mono' in family else 'sans-serif'}\" "
        f'text-anchor="{anchor}">{escape(content)}</text>'
    )


def _y_ticks(value_scale: Scale, count: int = Y_TICK_COUNT) -> list[float]:
    span = value_scale.domain_high - value_scale.domain_low
    return [value_scale.domain_low + span * step / (count - 1) for step in range(count)]


def _x_ticks(window_start: datetime, window_end: datetime, timezone: ZoneInfo) -> list[datetime]:
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


def _format_tick(value: float, decimals: int = 0) -> str:
    """Render one gridline value, without a `-0`-style negative-zero artefact."""
    formatted = f"{value:,.{decimals}f}"
    return formatted.removeprefix("-") if formatted.lstrip("-") == f"{0:,.{decimals}f}" else formatted


def _draw_gridlines(value_scale: Scale, time_scale: Scale) -> str:
    parts = []
    for value in _y_ticks(value_scale):
        y = value_scale(value)
        parts.append(f'<line x1="{time_scale.pixel_low:.1f}" y1="{y:.1f}" x2="{time_scale.pixel_high:.1f}" y2="{y:.1f}" stroke="{RULE}" stroke-width="0.8"/>')
        parts.append(_text(MARGIN_LEFT - 12, y + 3, _format_tick(value), size=9, colour=SOFT, family="Geist Mono", anchor="end"))
    return "".join(parts)


def _draw_x_axis(time_scale: Scale, bottom: float, timezone: ZoneInfo) -> str:
    window_start = datetime.fromtimestamp(time_scale.domain_low, tz=timezone)
    window_end = datetime.fromtimestamp(time_scale.domain_high, tz=timezone)
    parts = []
    for tick in _x_ticks(window_start, window_end, timezone):
        x = _at(time_scale, tick)
        parts.append(_text(x, bottom + 20, f"{tick.astimezone(timezone):%a %H:%M}", size=9, colour=SOFT, family="Geist Mono"))
    return "".join(parts)


def _panel_title(series: PayloadSeries) -> str:
    label = series.payload_type.value.replace("_", " ").title()
    unit = f"  · {series.unit}" if series.unit else ""
    return f"{label}{unit}"


def _draw_panel(series: PayloadSeries, top: float, height: float, time_scale: Scale, timezone: ZoneInfo) -> str:
    bottom = top + height
    value_scale = _value_scale([point.value for point in series.points], top, bottom)
    parts = [_draw_gridlines(value_scale, time_scale)]
    if value_scale.domain_low < 0 < value_scale.domain_high:
        zero_y = value_scale(0)
        parts.append(f'<line x1="{time_scale.pixel_low:.1f}" y1="{zero_y:.1f}" x2="{time_scale.pixel_high:.1f}" y2="{zero_y:.1f}" stroke="{RULE_SOLID}" stroke-width="0.8"/>')
    parts.append(f'<path d="{_step_path(series, time_scale, value_scale)}" fill="none" stroke="{ACCENT}" stroke-width="2.0" stroke-linejoin="round"/>')
    parts.append(_text(MARGIN_LEFT, top - 8, _panel_title(series), size=13, colour=INK, weight=600, anchor="start"))
    parts.append(_draw_x_axis(time_scale, bottom, timezone))
    return "".join(parts)


def render_svg(series_list: tuple[PayloadSeries, ...], timezone: ZoneInfo, slug: str) -> tuple[str, int]:
    """
    Render every payload series as one accessible, self-contained SVG, stacked panels.

    :param series_list:  One series per payload type, e.g. from `payload_series`.
    :param timezone:     Timezone to express each panel's x-axis in.
    :param slug:         Unique id prefix for the diagram's `<title>`/`<desc>`.
    :returns:             The SVG markup, and the total height it needs.
    """
    if not series_list:
        msg = "Event has no payload series to draw."
        raise ValueError(msg)
    window_start = min(series.points[0].start for series in series_list)
    window_end = max(series.window_end for series in series_list)
    time_scale = _time_scale(window_start, window_end, MARGIN_LEFT, CANVAS_WIDTH - MARGIN_RIGHT)

    body = []
    cursor: float = MARGIN_TOP
    for series in series_list:
        cursor += PANEL_TITLE_HEIGHT
        body.append(_draw_panel(series, cursor, PANEL_HEIGHT, time_scale, timezone))
        cursor += PANEL_HEIGHT
        cursor += X_AXIS_HEIGHT
        cursor += PANEL_GAP
    total_height = cursor + MARGIN_BOTTOM

    titles = [_panel_title(series) for series in series_list]
    title = "What the seeded OpenADR event provisions"
    desc = f"Line chart showing {', '.join(titles)}, one panel per payload type, over the event's {len(series_list[0].points)}-interval window."
    svg = (
        f'<svg id="{DIAGRAM_ELEMENT_ID}" role="img" aria-labelledby="{slug}-title {slug}-desc" '
        f'viewBox="0 0 {CANVAS_WIDTH} {total_height:.0f}" '
        f'xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto;">'
        f'<title id="{slug}-title">{escape(title)}</title>'
        f'<desc id="{slug}-desc">{escape(desc)}</desc>'
        f'<rect width="{CANVAS_WIDTH}" height="{total_height:.0f}" fill="{PAPER}"/>' + "".join(body) + "</svg>"
    )
    return svg, int(total_height)


def render_html(event: ExistingEvent, timezone: ZoneInfo, *, subtitle: str) -> str:
    """
    Render a whole event's provisioned payloads as a self-contained HTML page.

    :param event:     The just-created event to draw.
    :param timezone:  Timezone to express the x-axis and header in -- the demo site's
                       own timezone, passed in rather than hardcoded here so this module
                       stays free of the sys.path trick hierarchy.py needs.
    :param subtitle:  One line of context to print under the title, e.g. the curtailment
                       window seed_events.py worked out.
    :returns:          A complete HTML document as a string.
    """
    slug = "openadr-event"
    series_list = payload_series(event)
    svg, _height = render_svg(series_list, timezone, slug)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{escape(event.event_name or event.id)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="{FONTS_LINK}" rel="stylesheet">
<style>
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; background: {PAPER}; color: {INK};
    font-family: 'Geist', sans-serif; padding: 40px 48px 24px;
  }}
  .eyebrow {{
    font-family: 'Geist Mono', monospace; font-size: 11px; letter-spacing: 0.14em;
    text-transform: uppercase; color: {SOFT}; margin: 0 0 8px;
  }}
  h1 {{ font-family: 'Instrument Serif', serif; font-weight: 400; font-size: 2rem; margin: 0 0 4px; }}
  .subtitle {{ color: {MUTED}; font-size: 14px; margin: 0 0 28px; }}
  .diagram {{ margin-bottom: 24px; }}
  footer {{
    border-top: 1px solid {RULE}; padding-top: 12px; font-family: 'Geist Mono', monospace;
    font-size: 10px; color: {SOFT};
  }}
</style>
</head>
<body>
  <p class="eyebrow">LF ENERGY DEMO WALKTHROUGH</p>
  <h1>{escape(event.event_name or event.id)}</h1>
  <p class="subtitle">{escape(subtitle)}</p>
  <div class="diagram">{svg}</div>
  <footer>Event {escape(event.id)} · program {escape(event.program_id)} · generated by seed_events.py</footer>
</body>
</html>
"""
