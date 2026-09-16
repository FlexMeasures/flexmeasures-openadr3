"""
Seed a week of synthetic beliefs onto the demo site's sensors.

Gives the hierarchy that seed_assets.py builds something to show and something to
schedule against: a price curve to optimise against, a forecast for every load the
scheduler cannot move, a typical-usage reference profile for every device it can move,
and a current state of charge for every storage device, so a schedule can be triggered
right after this script finishes.

Like seed_assets.py, it runs inside the FlexMeasures server container, reusing its
uv-managed venv (/app/.venv) and talking to the database through the ORM, so no API
token flow is needed:

    docker compose exec server uv run --active --no-project /walkthrough/flexmeasures/seed_forecasts.py

Three kinds of belief are written, by two clearly-labelled data sources, because they do
not mean the same thing:

- *Forecasts*, by the `LF Energy demo forecaster` source: everything the scheduler reads as
  given and plans around. The day-ahead prices are what it optimises against. The office's
  background load and rooftop PV are the site's `inflexible-consumption` and
  `inflexible-production`. And each flexible device's *energy requirement* is a forecast
  too — the building's heat demand, and every charge point's power availability and
  departure requirement — because those describe the weather, the building and the drivers
  rather than anything the scheduler decides. They are genuine forecasts: their belief time
  precedes their knowledge time.
- *Reference profiles*, by the `LF Energy demo profiles` source, on the charge points and
  the heat pump. Those are the devices the scheduler dispatches, so a fixed week of power
  values is not a forecast of anything: it is what the site would do unscheduled, kept
  only so the charts are not empty before the first schedule runs. The scheduler writes
  its own beliefs, from its own source, onto the same sensors.
- *Measurements* of state of charge, by the same profiles source, one per storage device
  at the current quarter hour, which is what the storage scheduler looks for when it
  resolves `soc-at-start` from a sensor.

The aggregate power sensors of the campus, the hub and the office are deliberately left
empty: they are the scheduler's output, and seeding a synthetic aggregate there would
make those charts disagree with the schedule.

The values are deterministic. Every profile is generated per sensor and per local
calendar day from a seeded random generator keyed on both, so a given day always gets the
same numbers no matter when the script runs, and re-running it merges the same rows back
over themselves rather than drifting.

This script only ever runs after seed_assets.py, so it imports that script's account and
price-sensor lookups rather than duplicating them and risking the two drifting apart.
"""

from __future__ import annotations

import argparse
import math
import random
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import partial

import pandas as pd
from flexmeasures.app import create as create_flexmeasures_app
from flexmeasures.data import db
from flexmeasures.data.models.data_sources import DataSource
from flexmeasures.data.models.generic_assets import GenericAsset
from flexmeasures.data.models.time_series import Sensor, TimedBelief
from flexmeasures.data.models.user import Account
from flexmeasures.data.services.data_sources import get_or_create_source
from flexmeasures.utils.unit_utils import ur
from hierarchy import (
    ACCOUNT_NAME,
    CAMPUS_NAME,
    ENERGY_UNIT,
    EVSE_ACCEPTED_POWER_FRACTION,
    EVSE_ARRIVAL_HOUR,
    EVSE_ARRIVAL_SPREAD_HOURS,
    EVSE_AVAILABILITY_SENSOR_NAME,
    EVSE_DEPARTURE_HOUR,
    EVSE_DEPARTURE_SOC_FRACTION,
    EVSE_DEPARTURE_SPREAD_HOURS,
    EVSE_EARLIEST_ARRIVAL_HOUR,
    EVSE_EARLIEST_DEPARTURE_HOUR,
    EVSE_EARLY_SESSION,
    EVSE_INITIAL_SOC_RANGE,
    EVSE_LATE_SESSION,
    EVSE_LATEST_ARRIVAL_HOUR,
    EVSE_LATEST_DEPARTURE_HOUR,
    EVSE_MIN_SESSION_HOURS,
    EVSE_SESSION_ENERGY_FRACTION,
    EVSE_SPECS,
    EVSE_WEEKDAY_OCCUPANCY,
    EVSE_WEEKEND_OCCUPANCY,
    FLEXMEASURES_URL,
    FORECAST_HORIZON,
    HEAT_DEMAND_SENSOR_NAME,
    OFFICE_BASELOAD_DAY_POWER,
    OFFICE_BASELOAD_LUNCH_DIP,
    OFFICE_BASELOAD_LUNCH_HOUR,
    OFFICE_BASELOAD_LUNCH_SPREAD_HOURS,
    OFFICE_BASELOAD_NAME,
    OFFICE_BASELOAD_NIGHT_POWER,
    OFFICE_BASELOAD_NOISE,
    OFFICE_BASELOAD_WEEKEND_DAY_POWER,
    OFFICE_CLOSING_HOUR,
    OFFICE_HEAT_PUMP_HEAT_DEMAND_MORNING,
    OFFICE_HEAT_PUMP_HEAT_DEMAND_NIGHT,
    OFFICE_HEAT_PUMP_HEAT_DEMAND_NOISE,
    OFFICE_HEAT_PUMP_HEAT_DEMAND_OCCUPIED,
    OFFICE_HEAT_PUMP_HEAT_DEMAND_WEEKEND_FACTOR,
    OFFICE_HEAT_PUMP_IDLE_POWER,
    OFFICE_HEAT_PUMP_INITIAL_SOC_RANGE,
    OFFICE_HEAT_PUMP_NAME,
    OFFICE_HEAT_PUMP_NOISE,
    OFFICE_HEAT_PUMP_OCCUPIED_POWER,
    OFFICE_HEAT_PUMP_POWER_CAPACITY,
    OFFICE_HEAT_PUMP_PREHEAT_POWER,
    OFFICE_HEAT_PUMP_PREHEAT_START_HOUR,
    OFFICE_HEAT_PUMP_SOC_MAX,
    OFFICE_HEAT_PUMP_WEEKEND_FACTOR,
    OFFICE_OPENING_HOUR,
    OFFICE_PV_CLEAR_SKY_PEAK_FRACTION,
    OFFICE_PV_CLOUD_NOISE,
    OFFICE_PV_MAX_DAY_CLEARNESS,
    OFFICE_PV_MIN_DAY_CLEARNESS,
    OFFICE_PV_NAME,
    OFFICE_PV_PEAK_POWER,
    OFFICE_PV_SUNRISE_HOUR,
    OFFICE_PV_SUNSET_HOUR,
    OFFICE_RAMP_HOURS,
    POWER_RESOLUTION,
    POWER_SENSOR_NAME,
    POWER_UNIT,
    PRICE_DAY_LEVEL_RANGE,
    PRICE_EVENING_PEAK,
    PRICE_EVENING_PEAK_HOUR,
    PRICE_FEATURE_SPREAD_HOURS,
    PRICE_MIDDAY_DIP,
    PRICE_MIDDAY_DIP_HOUR,
    PRICE_MORNING_PEAK,
    PRICE_MORNING_PEAK_HOUR,
    PRICE_NIGHT,
    PRICE_NOISE,
    PRICE_RESOLUTION,
    PRICE_SENSOR_NAME,
    PRICE_WEEKEND_FACTOR,
    SITE_TIMEZONE,
    SOC_MINIMA_SENSOR_NAME,
    SOC_SENSOR_NAME,
    EvseSpec,
    ShoulderSession,
)
from seed_assets import find_account, get_or_create_day_ahead_price_sensor
from sqlalchemy import select

# The two sources are named after this walkthrough and typed by what they stand for, so
# nobody mistakes either for the plugin's own `OpenADR 3 VTN` source of fetched DR signals.
FORECAST_SOURCE_NAME = "LF Energy demo forecaster"
FORECAST_SOURCE_TYPE = "forecaster"

REFERENCE_SOURCE_NAME = "LF Energy demo profiles"
REFERENCE_SOURCE_TYPE = "demo script"

# Monday is 0, so anything from here on is the weekend.
SATURDAY = 5

MINUTES_PER_HOUR = 60
SECONDS_PER_HOUR = 3600

# Watts are far below the accuracy of anything synthetic; rounding also keeps re-runs writing identical rows.
BELIEF_PRECISION = 3

# Generates one local calendar day of values, one per event start, from a generator seeded on that day.
DayProfile = Callable[[date, pd.DatetimeIndex, random.Random], list[float]]


@dataclass(frozen=True, slots=True)
class ChargingSession:
    """
    One car, plugged into one charge point, for one continuous stay.

    :param start:       Local time the car arrives.
    :param end:         Local time it leaves.
    :param energy:      Energy the driver wants delivered during the stay, in the sensor's
                        energy unit. Shapes the unscheduled reference profile only.
    :param power:       Power the car accepts, which is at most the charge point's rating.
    :param target_soc:  State of charge the car has to reach by departure, in the sensor's
                        energy unit, or None for a stay that carries no requirement. This
                        is what the scheduler is actually held to. Only commuter stays have
                        one: an early bird plugged in for an hour is topping up, and a stay
                        that short could not honour a full battery anyway.
    """

    start: pd.Timestamp
    end: pd.Timestamp
    energy: float
    power: float
    target_soc: float | None


@dataclass
class ForecastReport:
    """What the run wrote, per kind of belief."""

    window_start: pd.Timestamp
    window_end: pd.Timestamp
    belief_time: pd.Timestamp
    forecasts: list[str] = field(default_factory=list)
    reference_profiles: list[str] = field(default_factory=list)
    states_of_charge: list[str] = field(default_factory=list)

    def record_profile(self, sensor: Sensor, count: int, *, is_forecast: bool) -> None:
        """
        Note a seeded power profile.

        :param sensor:       Sensor the profile was written to.
        :param count:        Number of beliefs written.
        :param is_forecast:  True for a genuine forecast, False for a typical-usage reference profile.
        """
        target = self.forecasts if is_forecast else self.reference_profiles
        target.append(f"{sensor.generic_asset.name}/{sensor.name}: {count} beliefs")

    def record_state_of_charge(self, sensor: Sensor, value: float) -> None:
        """
        Note a seeded state-of-charge reading.

        :param sensor:  State-of-charge sensor the reading was written to.
        :param value:   The recorded value, in the sensor's own unit.
        """
        self.states_of_charge.append(f"{sensor.generic_asset.name}/{sensor.name}: {value:.1f} {sensor.unit}")


def quantity_in(quantity: str, unit: str) -> float:
    """
    Read one of hierarchy.py's quantity strings as a plain number in the given unit.

    :param quantity:  A quantity with its unit, e.g. `22 kW`.
    :param unit:      Unit to express the result in, e.g. the unit of the sensor being written to.
    :returns:         The magnitude in that unit.
    """
    return float(ur.Quantity(quantity).to(unit).magnitude)


def find_asset(account: Account, name: str) -> GenericAsset:
    """
    Look up one asset of the demo hierarchy.

    :param account:      Account that owns the demo hierarchy.
    :param name:         Asset name, as declared in hierarchy.py.
    :returns:            The matching asset.
    :raises ValueError:  When the asset does not exist, which means the hierarchy was never seeded.
    """
    asset = db.session.execute(select(GenericAsset).filter_by(name=name, account_id=account.id)).scalar_one_or_none()
    if asset is None:
        msg = (
            f"No asset named '{name}' in account '{account.name}'. "
            "Seed the hierarchy first with "
            "`docker compose exec server uv run --active --no-project /walkthrough/flexmeasures/seed_assets.py`."
        )
        raise ValueError(msg)
    return asset


def find_sensor(asset: GenericAsset, name: str) -> Sensor:
    """
    Look up one sensor of an asset of the demo hierarchy.

    :param asset:        Asset the sensor belongs to.
    :param name:         Sensor name, as declared in hierarchy.py.
    :returns:            The matching sensor.
    :raises ValueError:  When the sensor does not exist, which means the hierarchy is out of date.
    """
    sensor = db.session.execute(select(Sensor).filter_by(name=name, generic_asset_id=asset.id)).scalar_one_or_none()
    if sensor is None:
        msg = f"Asset '{asset.name}' has no sensor named '{name}'. Re-run seed_assets.py to bring the hierarchy up to date."
        raise ValueError(msg)
    return sensor


def scheduling_window(horizon: timedelta) -> tuple[pd.Timestamp, pd.Timestamp]:
    """
    Return the window to seed, starting at the quarter hour the run falls in.

    Starting at the current quarter hour rather than the next one means a schedule
    triggered straight after this run finds data from its very first time step.

    :param horizon:  How far ahead to seed.
    :returns:        Window start and end, in UTC.
    """
    start = pd.Timestamp.now(tz="UTC").floor(POWER_RESOLUTION)
    return start, start + horizon


def forecast_issue_time(window_start: pd.Timestamp) -> pd.Timestamp:
    """
    Return the belief time to stamp the seeded profiles with: local midnight of the run's day.

    A belief counts as a forecast when its belief time precedes the sensor's knowledge time.
    These power sensors use timely-beliefs' default knowledge horizon, which places knowledge
    time at the end of the event, so any belief time at or before the start of the window
    makes the whole window read as a forecast rather than as a measurement.
    Local midnight is used rather than the current moment because it does not move within a
    run day, which is what lets a re-run merge the same rows back over themselves: belief time
    is part of a belief's key.

    :param window_start:  Start of the seeded window, in UTC.
    :returns:             The belief time, in UTC.
    """
    return window_start.tz_convert(SITE_TIMEZONE).normalize().tz_convert("UTC")


def local_days(window: tuple[pd.Timestamp, pd.Timestamp]) -> list[date]:
    """
    List every local calendar day the window touches.

    :param window:  Window start and end.
    :returns:       The local dates, in order.
    """
    first = window[0].tz_convert(SITE_TIMEZONE).date()
    last = window[1].tz_convert(SITE_TIMEZONE).date()
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]


def local_event_starts(day: date, resolution: timedelta) -> pd.DatetimeIndex:
    """
    Return every event start of one local calendar day, at the given resolution.

    Built from the day's two local midnights rather than by adding 24 hours, so the two
    days a year that are 23 or 25 hours long still come out whole.

    :param day:         A local calendar date.
    :param resolution:  Length of one event.
    :returns:           Localised event starts covering that day.
    """
    midnight = pd.Timestamp(day, tz=SITE_TIMEZONE)
    next_midnight = pd.Timestamp(day + timedelta(days=1), tz=SITE_TIMEZONE)
    return pd.date_range(midnight, next_midnight, freq=resolution, inclusive="left")


def at_local_hour(day: date, hour: float) -> pd.Timestamp:
    """
    Turn a local wall-clock hour of a day into a timestamp.

    :param day:   A local calendar date.
    :param hour:  Hour of that day, as a float, so 8.5 means 08:30.
    :returns:     The localised timestamp.
    """
    wall_clock = pd.Timestamp(day) + pd.Timedelta(hours=hour)
    return wall_clock.tz_localize(SITE_TIMEZONE, ambiguous=True, nonexistent="shift_forward")


def local_hour(timestamp: pd.Timestamp) -> float:
    """
    Return the local wall-clock hour of a localised timestamp, as a float.

    :param timestamp:  A timestamp already localised to the site's timezone.
    :returns:          Hour of the day, so 08:30 becomes 8.5.
    """
    return timestamp.hour + timestamp.minute / MINUTES_PER_HOUR


def jitter(rng: random.Random, spread: float) -> float:
    """
    Return a multiplier just above or below one, to keep a drawn profile from looking drawn.

    :param rng:     The profile's seeded random generator.
    :param spread:  Half-width of the multiplier, as a fraction.
    :returns:       A factor in [1 - spread, 1 + spread].
    """
    return rng.uniform(1 - spread, 1 + spread)


def bell(hour: float, centre: float, spread: float) -> float:
    """
    Return a bell curve over the hour of the day, peaking at one.

    :param hour:    Local hour to evaluate.
    :param centre:  Hour the curve peaks at.
    :param spread:  Width of the curve, in hours.
    :returns:       A factor in (0, 1].
    """
    return math.exp(-(((hour - centre) / spread) ** 2))


def clamp(value: float, lowest: float, highest: float) -> float:
    """
    Keep a drawn value inside a plausible range.

    :param value:    The drawn value.
    :param lowest:   Lower bound.
    :param highest:  Upper bound.
    :returns:        The value, moved onto the nearest bound if it fell outside.
    """
    return max(lowest, min(highest, value))


def occupancy_fraction(hour: float) -> float:
    """
    Return how much of the office's day-time load is switched on at a given local hour.

    Ramps up over the hour before opening and back down over the hour after closing,
    because a building's first arrivals and last departures are spread out.

    :param hour:  Local hour to evaluate.
    :returns:     A factor in [0, 1].
    """
    opening_ramp_start = OFFICE_OPENING_HOUR - OFFICE_RAMP_HOURS
    closing_ramp_end = OFFICE_CLOSING_HOUR + OFFICE_RAMP_HOURS
    if hour <= opening_ramp_start or hour >= closing_ramp_end:
        return 0.0
    if hour < OFFICE_OPENING_HOUR:
        return (hour - opening_ramp_start) / OFFICE_RAMP_HOURS
    if hour <= OFFICE_CLOSING_HOUR:
        return 1.0
    return (closing_ramp_end - hour) / OFFICE_RAMP_HOURS


def day_ahead_price_day(day: date, timestamps: pd.DatetimeIndex, rng: random.Random) -> list[float]:
    """
    Generate one day of day-ahead prices, in EUR/kWh.

    A night trough with a morning peak, a solar-driven dip in the middle of the day and the
    day's highest hours in the early evening, all lifted or flattened by a level drawn once
    per day. That spread is what the whole demo turns on: it is why the scheduler bothers to
    move the heat pump's pre-heat and the hub's charging at all.

    :param day:         The local calendar day.
    :param timestamps:  Every local event start of that day.
    :param rng:         The profile's seeded random generator.
    :returns:           One price per timestamp.
    """
    level = rng.uniform(*PRICE_DAY_LEVEL_RANGE)
    if day.weekday() >= SATURDAY:
        level *= PRICE_WEEKEND_FACTOR
    values = []
    for timestamp in timestamps:
        hour = local_hour(timestamp)
        price = PRICE_NIGHT
        price += (PRICE_MORNING_PEAK - PRICE_NIGHT) * bell(hour, PRICE_MORNING_PEAK_HOUR, PRICE_FEATURE_SPREAD_HOURS)
        price += (PRICE_EVENING_PEAK - PRICE_NIGHT) * bell(hour, PRICE_EVENING_PEAK_HOUR, PRICE_FEATURE_SPREAD_HOURS)
        price -= (PRICE_NIGHT - PRICE_MIDDAY_DIP) * bell(hour, PRICE_MIDDAY_DIP_HOUR, PRICE_FEATURE_SPREAD_HOURS)
        values.append(price * level * jitter(rng, PRICE_NOISE))
    return values


def office_baseload_day(day: date, timestamps: pd.DatetimeIndex, rng: random.Random) -> list[float]:
    """
    Generate one day of the office's background load, in kW.

    A night floor of servers and standby losses that the working day is stacked on top of,
    with a shallow dip while the desks are empty over lunch. Weekends keep the floor plus
    a small allowance for cleaning staff.

    :param day:         The local calendar day.
    :param timestamps:  Every local event start of that day.
    :param rng:         The profile's seeded random generator.
    :returns:           One power value per timestamp.
    """
    day_power = OFFICE_BASELOAD_DAY_POWER if day.weekday() < SATURDAY else OFFICE_BASELOAD_WEEKEND_DAY_POWER
    values = []
    for timestamp in timestamps:
        hour = local_hour(timestamp)
        power = OFFICE_BASELOAD_NIGHT_POWER + (day_power - OFFICE_BASELOAD_NIGHT_POWER) * occupancy_fraction(hour)
        power *= 1 - OFFICE_BASELOAD_LUNCH_DIP * bell(hour, OFFICE_BASELOAD_LUNCH_HOUR, OFFICE_BASELOAD_LUNCH_SPREAD_HOURS)
        values.append(power * jitter(rng, OFFICE_BASELOAD_NOISE))
    return values


def office_pv_day(_day: date, timestamps: pd.DatetimeIndex, rng: random.Random) -> list[float]:
    """
    Generate one day of rooftop PV production, in kW, positive because the sensor records production as positive.

    A half sine between sunrise and sunset, scaled by that day's cloud cover, which is
    drawn once so the week holds a mix of clear and overcast days.

    :param _day:        The local calendar day; the shape only depends on the hour, so it is unused.
    :param timestamps:  Every local event start of that day.
    :param rng:         The profile's seeded random generator.
    :returns:           One power value per timestamp.
    """
    nameplate = quantity_in(OFFICE_PV_PEAK_POWER, POWER_UNIT)
    clear_sky_peak = nameplate * OFFICE_PV_CLEAR_SKY_PEAK_FRACTION
    clearness = rng.uniform(OFFICE_PV_MIN_DAY_CLEARNESS, OFFICE_PV_MAX_DAY_CLEARNESS)
    daylight_hours = OFFICE_PV_SUNSET_HOUR - OFFICE_PV_SUNRISE_HOUR
    values = []
    for timestamp in timestamps:
        hour = local_hour(timestamp)
        if not OFFICE_PV_SUNRISE_HOUR <= hour <= OFFICE_PV_SUNSET_HOUR:
            values.append(0.0)
            continue
        elevation = math.sin(math.pi * (hour - OFFICE_PV_SUNRISE_HOUR) / daylight_hours)
        # An inverter cannot pass more than the array's rating, however bright the cloud edge.
        values.append(min(nameplate, max(0.0, clear_sky_peak * clearness * elevation * jitter(rng, OFFICE_PV_CLOUD_NOISE))))
    return values


def office_heat_pump_day(day: date, timestamps: pd.DatetimeIndex, rng: random.Random) -> list[float]:
    """
    Generate one day of the heat pump's unscheduled electrical demand, in kW.

    Most of the work happens before the building fills up: the buffer is charged overnight
    and topped up during the day, when people, lighting and equipment already contribute
    heat. This is only a baseline for the charts, since the scheduler decides the real
    dispatch, and moving exactly this pre-heat block towards cheap hours is the point of
    the demo.

    :param day:         The local calendar day.
    :param timestamps:  Every local event start of that day.
    :param rng:         The profile's seeded random generator.
    :returns:           One power value per timestamp.
    """
    setback = 1.0 if day.weekday() < SATURDAY else OFFICE_HEAT_PUMP_WEEKEND_FACTOR
    capacity = quantity_in(OFFICE_HEAT_PUMP_POWER_CAPACITY, POWER_UNIT)
    values = []
    for timestamp in timestamps:
        hour = local_hour(timestamp)
        if OFFICE_HEAT_PUMP_PREHEAT_START_HOUR <= hour < OFFICE_OPENING_HOUR:
            power = OFFICE_HEAT_PUMP_PREHEAT_POWER
        elif OFFICE_OPENING_HOUR <= hour < OFFICE_CLOSING_HOUR:
            power = OFFICE_HEAT_PUMP_OCCUPIED_POWER
        else:
            power = OFFICE_HEAT_PUMP_IDLE_POWER
        values.append(min(capacity, power * setback * jitter(rng, OFFICE_HEAT_PUMP_NOISE)))
    return values


def commuter_session(spec: EvseSpec, day: date, rng: random.Random) -> ChargingSession:
    """
    Draw the office-hours stay of one commuter at one charge point.

    :param spec:  Rating and battery limits of the charge point.
    :param day:   The local calendar day.
    :param rng:   The profile's seeded random generator.
    :returns:     The stay, which always lasts at least the minimum session length.
    """
    arrival_hour = clamp(rng.gauss(EVSE_ARRIVAL_HOUR, EVSE_ARRIVAL_SPREAD_HOURS), EVSE_EARLIEST_ARRIVAL_HOUR, EVSE_LATEST_ARRIVAL_HOUR)
    departure_hour = clamp(rng.gauss(EVSE_DEPARTURE_HOUR, EVSE_DEPARTURE_SPREAD_HOURS), EVSE_EARLIEST_DEPARTURE_HOUR, EVSE_LATEST_DEPARTURE_HOUR)
    departure_hour = max(departure_hour, arrival_hour + EVSE_MIN_SESSION_HOURS)
    return ChargingSession(
        start=at_local_hour(day, arrival_hour),
        end=at_local_hour(day, departure_hour),
        energy=quantity_in(spec.soc_max, ENERGY_UNIT) * rng.uniform(*EVSE_SESSION_ENERGY_FRACTION),
        power=quantity_in(spec.power_capacity, POWER_UNIT) * rng.uniform(*EVSE_ACCEPTED_POWER_FRACTION),
        target_soc=quantity_in(spec.soc_max, ENERGY_UNIT) * rng.uniform(*EVSE_DEPARTURE_SOC_FRACTION),
    )


def shoulder_session(session: ShoulderSession, spec: EvseSpec, day: date, rng: random.Random) -> ChargingSession:
    """
    Draw the short stay of an early bird or a late worker at one charge point.

    :param session:  The window and length such a stay may have.
    :param spec:     Rating and battery limits of the charge point.
    :param day:      The local calendar day.
    :param rng:      The profile's seeded random generator.
    :returns:        The stay.
    """
    start_hour = rng.uniform(session.earliest_start_hour, session.latest_start_hour)
    duration_hours = rng.uniform(session.min_duration_hours, session.max_duration_hours)
    return ChargingSession(
        start=at_local_hour(day, start_hour),
        end=at_local_hour(day, start_hour + duration_hours),
        energy=quantity_in(spec.soc_max, ENERGY_UNIT) * rng.uniform(*session.energy_fraction),
        power=quantity_in(spec.power_capacity, POWER_UNIT) * rng.uniform(*EVSE_ACCEPTED_POWER_FRACTION),
        target_soc=None,
    )


def without_overlaps(sessions: list[ChargingSession]) -> list[ChargingSession]:
    """
    Drop any stay that would have two cars in one bay at once.

    A charge point serves one car at a time, so a drawn early or late stay that runs into
    the commuter's stay simply did not happen.

    :param sessions:  Candidate stays, in chronological order of their start.
    :returns:         The stays that fit behind one another.
    """
    kept: list[ChargingSession] = []
    for session in sessions:
        if kept and session.start < kept[-1].end:
            continue
        kept.append(session)
    return kept


def plan_charging_sessions(spec: EvseSpec, day: date, rng: random.Random) -> list[ChargingSession]:
    """
    Draw one local day's stays at one charge point.

    Every draw is made in the same order whatever its outcome, so the day stays reproducible.

    :param spec:  Rating and battery limits of the charge point.
    :param day:   The local calendar day.
    :param rng:   The profile's seeded random generator.
    :returns:     Non-overlapping stays, in chronological order.
    """
    is_weekday = day.weekday() < SATURDAY
    candidates: list[ChargingSession] = []
    if is_weekday and rng.random() < EVSE_EARLY_SESSION.probability:
        candidates.append(shoulder_session(EVSE_EARLY_SESSION, spec, day, rng))
    if rng.random() < (EVSE_WEEKDAY_OCCUPANCY if is_weekday else EVSE_WEEKEND_OCCUPANCY):
        candidates.append(commuter_session(spec, day, rng))
    if is_weekday and rng.random() < EVSE_LATE_SESSION.probability:
        candidates.append(shoulder_session(EVSE_LATE_SESSION, spec, day, rng))
    return without_overlaps(candidates)


def charging_power(sessions: list[ChargingSession], timestamp: pd.Timestamp, step_hours: float) -> float:
    """
    Return the power drawn at one time step, given the day's stays.

    A car charges at the power it accepts until it has what the driver asked for, then sits
    plugged in at zero, which is what an AC charging session actually looks like.

    :param sessions:    The day's non-overlapping stays.
    :param timestamp:   Local event start of the time step.
    :param step_hours:  Length of one time step, in hours.
    :returns:           Power in the charge point's power unit, never above what the car accepts.
    """
    for session in sessions:
        if not session.start <= timestamp < session.end:
            continue
        delivered = session.power * (timestamp - session.start).total_seconds() / SECONDS_PER_HOUR
        remaining = session.energy - delivered
        if remaining <= 0:
            return 0.0
        # The final step delivers only what is left, so the session's energy comes out exact.
        return min(session.power, remaining / step_hours)
    return 0.0


def charge_point_day(spec: EvseSpec, day: date, timestamps: pd.DatetimeIndex, rng: random.Random) -> list[float]:
    """
    Generate one day of charging power at one charge point, in kW.

    The two bidirectional charge points are kept charge-only here: discharging is a
    scheduling decision, not typical usage, so inventing it would misrepresent the site.

    No stay drawn here can outlast its day, so a day can be generated on its own.

    :param spec:        Rating and battery limits of the charge point.
    :param day:         The local calendar day.
    :param timestamps:  Every local event start of that day.
    :param rng:         The profile's seeded random generator.
    :returns:           One power value per timestamp.
    """
    sessions = plan_charging_sessions(spec, day, rng)
    step_hours = POWER_RESOLUTION.total_seconds() / SECONDS_PER_HOUR
    return [charging_power(sessions, timestamp, step_hours) for timestamp in timestamps]


def charge_point_availability_day(spec: EvseSpec, day: date, timestamps: pd.DatetimeIndex, rng: random.Random) -> list[float]:
    """
    Generate one day of the power a charge point may move, in kW.

    Zero while the bay is empty and the connected car's accepted power while it is not, so
    the flex-model's `power-capacity` stops the scheduler charging a bay with no car in it.
    It caps both directions, which is what also keeps the two V2G charge points from
    discharging a car that has already driven away.

    The day's stays are drawn exactly as charge_point_day draws them — same seeded
    generator, same call, same order — so a charge point's availability, its departure
    requirement and its reference profile always describe the same cars.

    :param spec:        Rating and battery limits of the charge point.
    :param day:         The local calendar day.
    :param timestamps:  Every local event start of that day.
    :param rng:         The profile's seeded random generator.
    :returns:           One power ceiling per timestamp.
    """
    sessions = plan_charging_sessions(spec, day, rng)
    return [next((session.power for session in sessions if session.start <= timestamp < session.end), 0.0) for timestamp in timestamps]


def charge_point_soc_minima_day(spec: EvseSpec, day: date, timestamps: pd.DatetimeIndex, rng: random.Random) -> list[float]:
    """
    Generate one day of the state of charge a charge point's car has to have reached, in kWh.

    Zero for all but one quarter hour of each commuter stay: the last quarter hour that ends
    before the car leaves carries the state of charge the driver expects to find. Confining
    the requirement to that single interval is deliberate — it is what leaves the scheduler
    free to deliver the energy in whichever quarter hours of the stay are cheapest, which is
    the flexibility the day-ahead price and the OpenADR limit then compete over. A
    requirement that applied throughout the stay would instead force charging on arrival.

    Every interval gets an explicit value rather than being left empty, because FlexMeasures
    fills gaps in a sensor-referenced flex-model field: a blank interval would inherit the
    departure requirement and quietly oblige the car to stay full all day.

    :param spec:        Rating and battery limits of the charge point.
    :param day:         The local calendar day.
    :param timestamps:  Every local event start of that day.
    :param rng:         The profile's seeded random generator.
    :returns:           One required state of charge per timestamp.
    """
    sessions = plan_charging_sessions(spec, day, rng)
    requirements = {}
    for session in sessions:
        if session.target_soc is None:
            continue
        # The last event that ends within the stay, so the requirement is met while the car
        # is still plugged in rather than a quarter hour after it has left.
        within = [timestamp for timestamp in timestamps if timestamp + POWER_RESOLUTION <= session.end]
        if within:
            requirements[max(within)] = session.target_soc
    return [requirements.get(timestamp, 0.0) for timestamp in timestamps]


def office_heat_demand_day(day: date, timestamps: pd.DatetimeIndex, rng: random.Random) -> list[float]:
    """
    Generate one day of the building's heat demand, in thermal kW.

    This is the drain on the heat pump's thermal buffer, and therefore the reason the
    scheduler runs the heat pump at all. It follows the building's day: the heaviest call is
    bringing a cooled-down building back up to temperature before the first arrivals, it
    falls back once people, lighting and equipment are contributing heat of their own, and
    it drops to envelope losses overnight. Weekends are much lower, because nobody is coming
    in and the building is allowed to drift.

    Expressed in thermal kW to match the thermal state of charge, so it is the heat pump's
    coefficient of performance that decides what this costs electrically.

    :param day:         The local calendar day.
    :param timestamps:  Every local event start of that day.
    :param rng:         The profile's seeded random generator.
    :returns:           One heat demand per timestamp.
    """
    setback = 1.0 if day.weekday() < SATURDAY else OFFICE_HEAT_PUMP_HEAT_DEMAND_WEEKEND_FACTOR
    values = []
    for timestamp in timestamps:
        hour = local_hour(timestamp)
        if OFFICE_HEAT_PUMP_PREHEAT_START_HOUR <= hour < OFFICE_OPENING_HOUR:
            demand = OFFICE_HEAT_PUMP_HEAT_DEMAND_MORNING
        elif OFFICE_OPENING_HOUR <= hour < OFFICE_CLOSING_HOUR:
            demand = OFFICE_HEAT_PUMP_HEAT_DEMAND_OCCUPIED
        else:
            demand = OFFICE_HEAT_PUMP_HEAT_DEMAND_NIGHT
        values.append(demand * setback * jitter(rng, OFFICE_HEAT_PUMP_HEAT_DEMAND_NOISE))
    return values


def generate_profile(
    profile_key: str,
    day_profile: DayProfile,
    window: tuple[pd.Timestamp, pd.Timestamp],
    resolution: timedelta = POWER_RESOLUTION,
) -> dict[pd.Timestamp, float]:
    """
    Generate a profile over the whole window, one local calendar day at a time.

    Each day is generated whole, from a generator seeded on the profile and that day, and
    only then cut back to the window. That is what makes a given day's values independent
    of the moment the script runs: a run at noon writes the same afternoon as a run at dawn.

    :param profile_key:   Stable name of the profile, e.g. the asset's name; part of the seed.
    :param day_profile:   Callable generating one local day of values.
    :param window:        Window start and end, in UTC.
    :param resolution:    Resolution of the target sensor's events.
    :returns:             Event start mapped to value, covering the window.
    """
    values: dict[pd.Timestamp, float] = {}
    for day in local_days(window):
        timestamps = local_event_starts(day, resolution)
        rng = random.Random(f"{profile_key}|{day.isoformat()}")
        generated = zip(timestamps, day_profile(day, timestamps, rng), strict=True)
        values.update({timestamp: value for timestamp, value in generated if window[0] <= timestamp < window[1]})
    return values


def store_profile(sensor: Sensor, source: DataSource, values: dict[pd.Timestamp, float], belief_time: pd.Timestamp) -> int:
    """
    Write one generated profile to a sensor.

    Merging rather than adding is what makes a re-run an upsert: a belief's key is its
    event start, belief time, source and cumulative probability, all of which this run
    reproduces exactly.

    :param sensor:       Sensor to write to.
    :param source:       Data source the beliefs are attributed to.
    :param values:       Event start mapped to value, in the sensor's own unit.
    :param belief_time:  When the profile is claimed to have been drawn up.
    :returns:            Number of beliefs written.
    """
    for event_start, value in values.items():
        db.session.merge(
            TimedBelief(
                sensor=sensor,
                source=source,
                event_value=round(value, BELIEF_PRECISION),
                event_start=event_start.to_pydatetime(),
                belief_time=belief_time.to_pydatetime(),
            )
        )
    db.session.flush()
    return len(values)


def store_state_of_charge(
    sensor: Sensor,
    source: DataSource,
    capacity: str,
    fraction_range: tuple[float, float],
    moment: pd.Timestamp,
) -> float:
    """
    Record where a storage device stands right now.

    The storage scheduler resolves `soc-at-start` by looking for a belief within four time
    steps of the schedule start, so without this the very first schedule stops with
    "No recent state-of-charge value found". State of charge is instantaneous, so belief
    time equals event start, which makes this a measurement rather than a forecast.

    Unlike the power profiles, this reading is keyed on the current quarter hour: a re-run
    within the same quarter hour rewrites it, a later run records a newer one alongside it.

    :param sensor:          The device's state-of-charge sensor.
    :param source:          Data source the reading is attributed to.
    :param capacity:        The device's usable capacity, as declared in hierarchy.py.
    :param fraction_range:  Plausible band for the reading, as a fraction of that capacity.
    :param moment:          The quarter hour to record the reading at.
    :returns:               The recorded value, in the sensor's own unit.
    """
    rng = random.Random(f"{sensor.generic_asset.name}|{SOC_SENSOR_NAME}|{moment.isoformat()}")
    value = round(quantity_in(capacity, sensor.unit) * rng.uniform(*fraction_range), BELIEF_PRECISION)
    db.session.merge(
        TimedBelief(
            sensor=sensor,
            source=source,
            event_value=value,
            event_start=moment.to_pydatetime(),
            belief_time=moment.to_pydatetime(),
        )
    )
    db.session.flush()
    return value


def seed_day_ahead_prices(forecaster: DataSource, report: ForecastReport) -> None:
    """
    Seed the price sensor the campus' flex-context schedules against.

    Without prices over the planning window the scheduler stops with "Prices unknown for
    planning window" before it looks at anything else, so this is what makes the rest of the
    seeded week schedulable. Prices are the one profile written to a public asset rather than
    to the demo account: the sensor lives on the `NL transmission zone` that FlexMeasures
    ships. The sensor itself is fetched through seed_assets.py, so both scripts cannot end up
    disagreeing about its unit, resolution or knowledge horizon.

    :param forecaster:  Data source for genuine forecasts.
    :param report:      Run report to record the outcome in.
    """
    sensor = get_or_create_day_ahead_price_sensor()
    values = generate_profile(PRICE_SENSOR_NAME, day_ahead_price_day, (report.window_start, report.window_end), PRICE_RESOLUTION)
    count = store_profile(sensor, forecaster, values, report.belief_time)
    report.record_profile(sensor, count, is_forecast=True)


def seed_office(account: Account, forecaster: DataSource, profiles: DataSource, report: ForecastReport) -> None:
    """
    Seed the office's three devices.

    The background load and the PV are the site's `inflexible-consumption` and
    `inflexible-production`, so what they get is a forecast the scheduler plans around.
    The heat pump is dispatched by the scheduler, so what it gets is a reference profile
    plus a current state of charge — and a heat demand forecast, which is not a reference
    profile but an input: it is the drain on the thermal buffer that obliges the scheduler
    to run the heat pump in the first place.

    :param account:     Account that owns the demo hierarchy.
    :param forecaster:  Data source for genuine forecasts.
    :param profiles:    Data source for synthetic reference profiles and readings.
    :param report:      Run report to record the outcome in.
    """
    window = (report.window_start, report.window_end)

    for asset_name, day_profile in ((OFFICE_BASELOAD_NAME, office_baseload_day), (OFFICE_PV_NAME, office_pv_day)):
        sensor = find_sensor(find_asset(account, asset_name), POWER_SENSOR_NAME)
        count = store_profile(sensor, forecaster, generate_profile(asset_name, day_profile, window), report.belief_time)
        report.record_profile(sensor, count, is_forecast=True)

    heat_pump = find_asset(account, OFFICE_HEAT_PUMP_NAME)
    power = find_sensor(heat_pump, POWER_SENSOR_NAME)
    count = store_profile(power, profiles, generate_profile(OFFICE_HEAT_PUMP_NAME, office_heat_pump_day, window), report.belief_time)
    report.record_profile(power, count, is_forecast=False)

    heat_demand = find_sensor(heat_pump, HEAT_DEMAND_SENSOR_NAME)
    values = generate_profile(HEAT_DEMAND_SENSOR_NAME, office_heat_demand_day, window)
    count = store_profile(heat_demand, forecaster, values, report.belief_time)
    report.record_profile(heat_demand, count, is_forecast=True)

    state_of_charge = find_sensor(heat_pump, SOC_SENSOR_NAME)
    value = store_state_of_charge(state_of_charge, profiles, OFFICE_HEAT_PUMP_SOC_MAX, OFFICE_HEAT_PUMP_INITIAL_SOC_RANGE, report.window_start)
    report.record_state_of_charge(state_of_charge, value)


def seed_charge_points(account: Account, forecaster: DataSource, profiles: DataSource, report: ForecastReport) -> None:
    """
    Seed every charge point of the hub with a typical week and a connected car's state of charge.

    Three profiles per charge point, all drawn from the same stays so they cannot disagree
    about which cars turned up, but meaning three different things:

    - the *reference* power profile, what the bay would draw unscheduled, which the
      scheduler overwrites;
    - the *availability* forecast, the power the bay may move, which is zero whenever no car
      is plugged in;
    - the *departure requirement* forecast, the state of charge each commuter's car has to
      have reached by the time they leave.

    The last two are forecasts of driver behaviour that the scheduler plans around, not
    reference profiles it replaces, so they are attributed to the forecaster.

    :param account:     Account that owns the demo hierarchy.
    :param forecaster:  Data source for genuine forecasts.
    :param profiles:    Data source for synthetic reference profiles and readings.
    :param report:      Run report to record the outcome in.
    """
    window = (report.window_start, report.window_end)

    for spec in EVSE_SPECS:
        charge_point = find_asset(account, spec.name)

        power = find_sensor(charge_point, POWER_SENSOR_NAME)
        day_profile = partial(charge_point_day, spec)
        count = store_profile(power, profiles, generate_profile(spec.name, day_profile, window), report.belief_time)
        report.record_profile(power, count, is_forecast=False)

        # Keyed on the charge point's own name, like the reference profile above, so all
        # three profiles are generated from the same seeded draw of the day's stays.
        for sensor_name, generator in (
            (EVSE_AVAILABILITY_SENSOR_NAME, charge_point_availability_day),
            (SOC_MINIMA_SENSOR_NAME, charge_point_soc_minima_day),
        ):
            sensor = find_sensor(charge_point, sensor_name)
            values = generate_profile(spec.name, partial(generator, spec), window)
            count = store_profile(sensor, forecaster, values, report.belief_time)
            report.record_profile(sensor, count, is_forecast=True)

        state_of_charge = find_sensor(charge_point, SOC_SENSOR_NAME)
        value = store_state_of_charge(state_of_charge, profiles, spec.soc_max, EVSE_INITIAL_SOC_RANGE, report.window_start)
        report.record_state_of_charge(state_of_charge, value)


def print_summary(report: ForecastReport, campus: GenericAsset) -> None:
    """
    Print what the run wrote, grouped by what the beliefs mean.

    :param report:  Run report gathered while seeding.
    :param campus:  Top-level site asset, to link to in the closing hint.
    """
    print(f"Seeded {report.window_start.isoformat()} through {report.window_end.isoformat()}, believed as of {report.belief_time.isoformat()}.")
    print(f"Forecasts ({FORECAST_SOURCE_NAME}), which the scheduler plans around:")
    for line in report.forecasts:
        print(f"  + {line}")
    print(f"Reference profiles ({REFERENCE_SOURCE_NAME}), which the scheduler will override:")
    for line in report.reference_profiles:
        print(f"  ~ {line}")
    print(f"Current state of charge ({REFERENCE_SOURCE_NAME}), which the scheduler starts from:")
    for line in report.states_of_charge:
        print(f"  = {line}")
    print(f"Next: open {FLEXMEASURES_URL}/assets/{campus.id} to see the week, then trigger a schedule.")


def main() -> None:
    """Seed the demo site's synthetic profiles and print a summary of the result."""
    parser = argparse.ArgumentParser(description="Seed synthetic forecasts and usage profiles for the LF Energy demo site.")
    parser.add_argument(
        "--account-name",
        default=ACCOUNT_NAME,
        help=f"Account owning the demo assets (default: {ACCOUNT_NAME})",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=FORECAST_HORIZON.days,
        help=f"How many days ahead to seed (default: {FORECAST_HORIZON.days})",
    )
    args = parser.parse_args()

    app = create_flexmeasures_app()

    with app.app_context():
        account = find_account(args.account_name)
        campus = find_asset(account, CAMPUS_NAME)

        window = scheduling_window(timedelta(days=args.days))
        report = ForecastReport(window_start=window[0], window_end=window[1], belief_time=forecast_issue_time(window[0]))

        forecaster = get_or_create_source(source=FORECAST_SOURCE_NAME, source_type=FORECAST_SOURCE_TYPE)
        profiles = get_or_create_source(source=REFERENCE_SOURCE_NAME, source_type=REFERENCE_SOURCE_TYPE)

        seed_day_ahead_prices(forecaster, report)
        seed_office(account, forecaster, profiles, report)
        seed_charge_points(account, forecaster, profiles, report)

        db.session.commit()
        print_summary(report, campus)


if __name__ == "__main__":
    try:
        main()
    except ValueError as exc:
        print(f"Failed to seed the demo site's profiles: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
