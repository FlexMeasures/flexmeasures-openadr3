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

# Public day-ahead price sensor on the `NL transmission zone` asset, also created by `flexmeasures add toy-account`.
PRICE_SENSOR_NAME = "day-ahead prices"

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

__all__ = [
    "ACCOUNT_NAME",
    "CAMPUS_BREACH_PRICE",
    "CAMPUS_NAME",
    "CAMPUS_POWER_CAPACITY",
    "CONTRACT_RESOLUTION",
    "EVSE_HUB_NAME",
    "EVSE_HUB_POWER_CAPACITY",
    "EVSE_SPECS",
    "FLEXMEASURES_URL",
    "OFFICE_BASELOAD_NAME",
    "OFFICE_HEAT_PUMP_CHARGING_EFFICIENCY",
    "OFFICE_HEAT_PUMP_COP",
    "OFFICE_HEAT_PUMP_NAME",
    "OFFICE_HEAT_PUMP_POWER_CAPACITY",
    "OFFICE_HEAT_PUMP_SOC_MAX",
    "OFFICE_HEAT_PUMP_SOC_MIN",
    "OFFICE_HEAT_PUMP_STORAGE_EFFICIENCY",
    "OFFICE_NAME",
    "OFFICE_POWER_CAPACITY",
    "OFFICE_PV_NAME",
    "OFFICE_PV_PEAK_POWER",
    "POLLING_SCHEDULE_NAME",
    "POWER_RESOLUTION",
    "PRICE_SENSOR_NAME",
    "SITE_LATITUDE",
    "SITE_LONGITUDE",
    "SITE_TIMEZONE",
    "SOC_RESOLUTION",
    "VEN_CLIENT_NAME",
    "EvseSpec",
]
