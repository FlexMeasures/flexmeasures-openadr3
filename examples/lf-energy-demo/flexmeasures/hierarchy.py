"""
Declarative description of the demo site that seed_assets.py builds.

Everything describing *what* the demo site looks like lives here, so the seeding
script can stay focused on *how* to persist it idempotently.
Connection settings the rest of the walkthrough already owns are reused from
``python/settings.py`` rather than duplicated.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

# The walkthrough's shared constants live in the sibling `python/` helper folder.
# Appending (rather than inserting) keeps that folder from shadowing anything already importable.
_WALKTHROUGH_PYTHON_DIR = Path(__file__).resolve().parent.parent / "python"
if str(_WALKTHROUGH_PYTHON_DIR) not in sys.path:
    sys.path.append(str(_WALKTHROUGH_PYTHON_DIR))

from settings import FLEXMEASURES_URL, POLLING_SCHEDULE_NAME, VEN_CLIENT_NAME  # noqa: E402

# The docker-compose `server` entrypoint creates this account via `flexmeasures add toy-account`.
ACCOUNT_NAME = "Walkthrough Toy Account"

# Amsterdam, matching the toy account's own assets so every demo asset shares one map view.
SITE_LATITUDE = 52.374
SITE_LONGITUDE = 4.88969
SITE_TIMEZONE = "Europe/Amsterdam"

# Every power sensor is quarter-hourly: both the Dutch imbalance settlement period
# and the resolution the plugin uses for OpenADR capacity-limit intervals.
POWER_RESOLUTION = timedelta(minutes=15)

# A state-of-charge sensor holds instantaneous readings, so the scheduler can align
# them with the boundaries of its own power time steps.
SOC_RESOLUTION = timedelta(0)

# A contracted connection capacity changes at most once per contract year.
CONTRACT_RESOLUTION = "P1Y"

# Sensor names and units are reused across assets, so every asset page reads the same,
# and both seeding scripts agree on which sensor to write to.
POWER_SENSOR_NAME = "power"
SOC_SENSOR_NAME = "state of charge"
GRID_CONNECTION_CAPACITY_SENSOR_NAME = "grid connection capacity"
INDOOR_TEMPERATURE_SENSOR_NAME = "indoor temperature"

POWER_UNIT = "kW"
ENERGY_UNIT = "kWh"
TEMPERATURE_UNIT = "°C"

# Public day-ahead price sensor on the `NL transmission zone` asset, also created by `flexmeasures add toy-account`.
PRICE_SENSOR_NAME = "day-ahead prices"

# A day-ahead market clears in hourly products.
PRICE_RESOLUTION = timedelta(hours=1)
PRICE_UNIT = "EUR/kWh"

# --- Level 1: the site -------------------------------------------------------
# One building with a single grid connection point, through which all demo power flows.

CAMPUS_NAME = "demo-campus"

# A 1 MVA connection is a realistic size for a small campus with a charging hub.
CAMPUS_POWER_CAPACITY = "1 MVA"

# Breaching the connection limit is expensive but not physically impossible, which is
# what lets the scheduler weigh a short breach against a price spike.
CAMPUS_BREACH_PRICE = "600 EUR/kW"

# --- Level 2a: the EVSE charging hub -----------------------------------------
# A sub-EMS whose flex-model entry caps the aggregate power of every charger behind it.

EVSE_HUB_NAME = "demo-evse-hub"

# The hub sits behind a 400 kVA sub-connection, well below the summed rating of its
# chargers, so the chargers have to share it.
EVSE_HUB_POWER_CAPACITY = "400 kVA"


@dataclass(frozen=True, slots=True)
class EvseSpec:
    """
    One charge point in the hub.

    :param name:                  Asset name, unique within the account.
    :param asset_type:            `one-way_evse` for charge-only, `two-way_evse` for V2G.
    :param power_capacity:        Nameplate rating, enforced as a hard limit.
    :param production_capacity:   Discharge limit; `0 kW` for charge-only, None to allow the full rating in both directions.
    :param soc_min:               Reserve the driver wants to keep in the car.
    :param soc_max:               Usable battery capacity of the connected car.
    :param roundtrip_efficiency:  Combined charger and battery losses.
    """

    name: str
    asset_type: str
    power_capacity: str
    production_capacity: str | None
    soc_min: str
    soc_max: str
    roundtrip_efficiency: str


# Six AC chargers plus two bidirectional ones: enough for the shared-capacity constraint
# to bite, few enough to stay readable on an asset page.
EVSE_SPECS: tuple[EvseSpec, ...] = (
    *(
        EvseSpec(
            name=f"demo-evse-{index:02d}",
            asset_type="one-way_evse",
            power_capacity="22 kW",
            production_capacity="0 kW",
            soc_min="0 kWh",
            soc_max="60 kWh",
            roundtrip_efficiency="92%",
        )
        for index in range(1, 7)
    ),
    *(
        EvseSpec(
            name=f"demo-evse-{index:02d}",
            asset_type="two-way_evse",
            power_capacity="11 kW",
            # No separate discharge limit: a V2G charge point may use its full rating either way.
            production_capacity=None,
            # A V2G driver keeps a reserve, so the car stays usable after a discharge window.
            soc_min="10 kWh",
            soc_max="77 kWh",
            roundtrip_efficiency="88%",
        )
        for index in range(7, 9)
    ),
)

# --- Level 2b: the grid-friendly office --------------------------------------
# A second sub-EMS with rooftop PV, a heat pump and a background load.

OFFICE_NAME = "demo-office"

# The office's own sub-connection, capping the combined power of everything behind it:
# the PV, the background load and the heat pump all reference the office as their group.
OFFICE_POWER_CAPACITY = "250 kVA"

OFFICE_PV_NAME = "demo-office-pv"

# 150 kWp of rooftop PV, treated as a must-take infeed rather than a curtailable device.
OFFICE_PV_PEAK_POWER = "150 kW"

# Lighting, servers and plug loads: the load the office cannot shift.
OFFICE_BASELOAD_NAME = "demo-office-baseload"

# FlexMeasures ships no `heat_pump` asset type, so the heat pump plus the building's
# thermal mass is modelled as a thermal buffer the scheduler charges and discharges,
# mirroring the `Heat Pump Template` asset that FlexMeasures itself seeds.
OFFICE_HEAT_PUMP_NAME = "demo-office-heat-pump"

# Electrical rating: what the heat pump draws from the office.
OFFICE_HEAT_PUMP_POWER_CAPACITY = "30 kW"

# Seasonal coefficient of performance, expressed the way FlexMeasures models it: as a
# one-way `charging-efficiency` from electricity to stored heat. That field is allowed to
# exceed 100%, precisely so it can carry a COP; `roundtrip-efficiency` is capped at 100%
# and would reject this value.
OFFICE_HEAT_PUMP_COP = 3.5
OFFICE_HEAT_PUMP_CHARGING_EFFICIENCY = f"{OFFICE_HEAT_PUMP_COP:.0%}"

# State of charge is therefore *thermal* energy: 30 kW electrical becomes 105 kW of heat,
# so this buffer holds about four hours of full-power charging.
OFFICE_HEAT_PUMP_SOC_MIN = "0 kWh"
OFFICE_HEAT_PUMP_SOC_MAX = "420 kWh"

# Thermal mass leaks: roughly 2% of the stored heat is lost per quarter hour.
OFFICE_HEAT_PUMP_STORAGE_EFFICIENCY = "98%"

# --- How the demo site behaves -----------------------------------------------
# Shape parameters for the week of synthetic data that seed_forecasts.py writes.
# They describe what the site typically *does*, the way the sections above describe what it *is*,
# so both live here and the seeding scripts stay focused on how to persist them.
# Every hour below is a local wall-clock hour in SITE_TIMEZONE, expressed as a float,
# so that 8.5 reads as 08:30.

# A week of quarter hours: long enough to schedule a full week against, short enough to seed in seconds.
FORECAST_HORIZON = timedelta(days=7)

# A Dutch day-ahead curve, in EUR/kWh: a night trough, a morning peak as the country wakes up,
# a dip in the middle of the day while the sun is on the roofs, and the highest hours in the early evening.
# The spread between the dip and the evening peak is what gives the scheduler something to optimise.
PRICE_NIGHT = 0.06
PRICE_MORNING_PEAK = 0.17
PRICE_MIDDAY_DIP = 0.02
PRICE_EVENING_PEAK = 0.21

PRICE_MORNING_PEAK_HOUR = 8.0
PRICE_MIDDAY_DIP_HOUR = 13.0
PRICE_EVENING_PEAK_HOUR = 19.0

# Width of each of those three features, in hours.
PRICE_FEATURE_SPREAD_HOURS = 2.5

# Demand is lower at the weekend, and so is the whole curve.
PRICE_WEEKEND_FACTOR = 0.8

# The level of the whole day, drawn once: mostly a matter of how much wind there is.
PRICE_DAY_LEVEL_RANGE = (0.75, 1.35)

PRICE_NOISE = 0.08

# The office is open on weekdays between these hours, which anchors every occupancy-driven profile below.
OFFICE_OPENING_HOUR = 8.0
OFFICE_CLOSING_HOUR = 18.0

# A building does not switch between its night and day load in a single quarter hour:
# the first arrivals and the last departures spread the change over about an hour on either side.
OFFICE_RAMP_HOURS = 1.0

# Servers, network gear, emergency lighting and standby losses: what the office draws when nobody is in.
OFFICE_BASELOAD_NIGHT_POWER = 18.0

# Lighting, workstations, ventilation and the kitchen, on top of the night floor.
OFFICE_BASELOAD_DAY_POWER = 85.0

# Weekends keep the night floor, plus a modest allowance for cleaning staff and weekend workers.
OFFICE_BASELOAD_WEEKEND_DAY_POWER = 26.0

# Lunch empties the desks for a while, which shows up as a shallow dip around this hour.
OFFICE_BASELOAD_LUNCH_HOUR = 12.75
OFFICE_BASELOAD_LUNCH_DIP = 0.12
OFFICE_BASELOAD_LUNCH_SPREAD_HOURS = 0.9

# Quarter-to-quarter jitter, as a fraction of the load: enough to look measured rather than drawn.
OFFICE_BASELOAD_NOISE = 0.05

# A fixed daylight window rather than a real ephemeris: the demo only ever spans a week,
# and these hours are about right for Amsterdam outside midwinter.
OFFICE_PV_SUNRISE_HOUR = 6.75
OFFICE_PV_SUNSET_HOUR = 20.5

# A rooftop array never reaches its nameplate rating in the field, because of angle, temperature and inverter losses.
OFFICE_PV_CLEAR_SKY_PEAK_FRACTION = 0.75

# Cloud cover, drawn once per day: from a thoroughly overcast day to a clear one.
OFFICE_PV_MIN_DAY_CLEARNESS = 0.25
OFFICE_PV_MAX_DAY_CLEARNESS = 1.0

# Passing clouds within a day, as a fraction of that day's clear-sky output.
OFFICE_PV_CLOUD_NOISE = 0.18

# The heat pump pre-heats the building before the first arrivals, which is the demo's
# clearest example of a load worth shifting: the scheduler will move it towards cheap hours.
OFFICE_HEAT_PUMP_PREHEAT_START_HOUR = 4.5
OFFICE_HEAT_PUMP_PREHEAT_POWER = 21.0

# During office hours the building gains incidental heat from people, lighting and equipment,
# so the heat pump only has to top the buffer up.
OFFICE_HEAT_PUMP_OCCUPIED_POWER = 7.0

# Outside those hours it runs at a low setback, replacing what the thermal mass leaks.
OFFICE_HEAT_PUMP_IDLE_POWER = 3.5

# An empty building is allowed to drift much further from its setpoint.
OFFICE_HEAT_PUMP_WEEKEND_FACTOR = 0.35

OFFICE_HEAT_PUMP_NOISE = 0.08

# Where the thermal buffer sits when the demo starts, as a fraction of its usable capacity.
OFFICE_HEAT_PUMP_INITIAL_SOC_RANGE = (0.35, 0.6)

# Commuters plug in around the start of the office day and leave around the end of it.
# Spread is a standard deviation; the bounds keep the tail of the distribution plausible.
EVSE_ARRIVAL_HOUR = 8.25
EVSE_ARRIVAL_SPREAD_HOURS = 0.6
EVSE_EARLIEST_ARRIVAL_HOUR = 7.0
EVSE_LATEST_ARRIVAL_HOUR = 10.0

EVSE_DEPARTURE_HOUR = 17.0
EVSE_DEPARTURE_SPREAD_HOURS = 0.9
EVSE_EARLIEST_DEPARTURE_HOUR = 15.0
EVSE_LATEST_DEPARTURE_HOUR = 19.5

# Nobody plugs in for less than this, so a short stay never becomes a zero-length session.
EVSE_MIN_SESSION_HOURS = 0.75

# How often a charge point is taken for the office day. Not every bay fills every day,
# and at the weekend the hub is nearly empty.
EVSE_WEEKDAY_OCCUPANCY = 0.85
EVSE_WEEKEND_OCCUPANCY = 0.1

# How much energy a commuter needs, as a fraction of the connected car's usable battery.
EVSE_SESSION_ENERGY_FRACTION = (0.25, 0.75)

# Most cars cannot accept the full rating of the charge point, because their onboard charger is the bottleneck.
EVSE_ACCEPTED_POWER_FRACTION = (0.5, 1.0)

# Where a connected car's battery sits when the demo starts, as a fraction of its usable capacity.
EVSE_INITIAL_SOC_RANGE = (0.2, 0.5)


@dataclass(frozen=True, slots=True)
class ShoulderSession:
    """
    A short charging session just outside the office day, for an early bird or a late worker.

    :param probability:         Chance that a given charge point sees this session on a given weekday.
    :param earliest_start_hour: Start of the window the session may begin in.
    :param latest_start_hour:   End of that window.
    :param min_duration_hours:  Shortest plausible stay.
    :param max_duration_hours:  Longest plausible stay.
    :param energy_fraction:     Energy taken, as a fraction of the connected car's usable battery.
    """

    probability: float
    earliest_start_hour: float
    latest_start_hour: float
    min_duration_hours: float
    max_duration_hours: float
    energy_fraction: tuple[float, float]


# Someone who beats the traffic, charges for an hour and unplugs before the commuters arrive.
EVSE_EARLY_SESSION = ShoulderSession(
    probability=0.18,
    earliest_start_hour=5.75,
    latest_start_hour=7.25,
    min_duration_hours=0.75,
    max_duration_hours=2.0,
    energy_fraction=(0.08, 0.2),
)

# Someone who stays late, or who tops up before driving home.
EVSE_LATE_SESSION = ShoulderSession(
    probability=0.22,
    earliest_start_hour=17.5,
    latest_start_hour=19.5,
    min_duration_hours=0.75,
    max_duration_hours=2.5,
    energy_fraction=(0.08, 0.25),
)

__all__ = [
    "ACCOUNT_NAME",
    "CAMPUS_BREACH_PRICE",
    "CAMPUS_NAME",
    "CAMPUS_POWER_CAPACITY",
    "CONTRACT_RESOLUTION",
    "ENERGY_UNIT",
    "EVSE_ACCEPTED_POWER_FRACTION",
    "EVSE_ARRIVAL_HOUR",
    "EVSE_ARRIVAL_SPREAD_HOURS",
    "EVSE_DEPARTURE_HOUR",
    "EVSE_DEPARTURE_SPREAD_HOURS",
    "EVSE_EARLIEST_ARRIVAL_HOUR",
    "EVSE_EARLIEST_DEPARTURE_HOUR",
    "EVSE_EARLY_SESSION",
    "EVSE_HUB_NAME",
    "EVSE_HUB_POWER_CAPACITY",
    "EVSE_INITIAL_SOC_RANGE",
    "EVSE_LATEST_ARRIVAL_HOUR",
    "EVSE_LATEST_DEPARTURE_HOUR",
    "EVSE_LATE_SESSION",
    "EVSE_MIN_SESSION_HOURS",
    "EVSE_SESSION_ENERGY_FRACTION",
    "EVSE_SPECS",
    "EVSE_WEEKDAY_OCCUPANCY",
    "EVSE_WEEKEND_OCCUPANCY",
    "FLEXMEASURES_URL",
    "FORECAST_HORIZON",
    "GRID_CONNECTION_CAPACITY_SENSOR_NAME",
    "INDOOR_TEMPERATURE_SENSOR_NAME",
    "OFFICE_BASELOAD_DAY_POWER",
    "OFFICE_BASELOAD_LUNCH_DIP",
    "OFFICE_BASELOAD_LUNCH_HOUR",
    "OFFICE_BASELOAD_LUNCH_SPREAD_HOURS",
    "OFFICE_BASELOAD_NAME",
    "OFFICE_BASELOAD_NIGHT_POWER",
    "OFFICE_BASELOAD_NOISE",
    "OFFICE_BASELOAD_WEEKEND_DAY_POWER",
    "OFFICE_CLOSING_HOUR",
    "OFFICE_HEAT_PUMP_CHARGING_EFFICIENCY",
    "OFFICE_HEAT_PUMP_COP",
    "OFFICE_HEAT_PUMP_IDLE_POWER",
    "OFFICE_HEAT_PUMP_INITIAL_SOC_RANGE",
    "OFFICE_HEAT_PUMP_NAME",
    "OFFICE_HEAT_PUMP_NOISE",
    "OFFICE_HEAT_PUMP_OCCUPIED_POWER",
    "OFFICE_HEAT_PUMP_POWER_CAPACITY",
    "OFFICE_HEAT_PUMP_PREHEAT_POWER",
    "OFFICE_HEAT_PUMP_PREHEAT_START_HOUR",
    "OFFICE_HEAT_PUMP_SOC_MAX",
    "OFFICE_HEAT_PUMP_SOC_MIN",
    "OFFICE_HEAT_PUMP_STORAGE_EFFICIENCY",
    "OFFICE_HEAT_PUMP_WEEKEND_FACTOR",
    "OFFICE_NAME",
    "OFFICE_OPENING_HOUR",
    "OFFICE_POWER_CAPACITY",
    "OFFICE_PV_CLEAR_SKY_PEAK_FRACTION",
    "OFFICE_PV_CLOUD_NOISE",
    "OFFICE_PV_MAX_DAY_CLEARNESS",
    "OFFICE_PV_MIN_DAY_CLEARNESS",
    "OFFICE_PV_NAME",
    "OFFICE_PV_PEAK_POWER",
    "OFFICE_PV_SUNRISE_HOUR",
    "OFFICE_PV_SUNSET_HOUR",
    "OFFICE_RAMP_HOURS",
    "POLLING_SCHEDULE_NAME",
    "POWER_RESOLUTION",
    "POWER_SENSOR_NAME",
    "POWER_UNIT",
    "PRICE_DAY_LEVEL_RANGE",
    "PRICE_EVENING_PEAK",
    "PRICE_EVENING_PEAK_HOUR",
    "PRICE_FEATURE_SPREAD_HOURS",
    "PRICE_MIDDAY_DIP",
    "PRICE_MIDDAY_DIP_HOUR",
    "PRICE_MORNING_PEAK",
    "PRICE_MORNING_PEAK_HOUR",
    "PRICE_NIGHT",
    "PRICE_NOISE",
    "PRICE_RESOLUTION",
    "PRICE_SENSOR_NAME",
    "PRICE_UNIT",
    "PRICE_WEEKEND_FACTOR",
    "SITE_LATITUDE",
    "SITE_LONGITUDE",
    "SITE_TIMEZONE",
    "SOC_RESOLUTION",
    "SOC_SENSOR_NAME",
    "TEMPERATURE_UNIT",
    "VEN_CLIENT_NAME",
    "EvseSpec",
    "ShoulderSession",
]
