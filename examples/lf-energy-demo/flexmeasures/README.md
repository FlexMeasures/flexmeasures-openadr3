# FlexMeasures asset hierarchy

Seeding for the [LF Energy demo walkthrough](../README.md): builds the demo site as real
`GenericAsset` and `Sensor` rows, so the OpenADR capacity limits fetched elsewhere in the
walkthrough have a site to constrain, and fills those sensors with a week of synthetic
data, so the site has something to show and something to schedule against.

| File | Run where | Run with | Purpose |
| --- | --- | --- | --- |
| `seed_assets.py` | FlexMeasures container | `uv run --active --no-project /walkthrough/flexmeasures/seed_assets.py` | Create the demo asset hierarchy in the `Walkthrough Toy Account` |
| `seed_forecasts.py` | FlexMeasures container | `uv run --active --no-project /walkthrough/flexmeasures/seed_forecasts.py` | Fill that hierarchy's sensors with a week of prices, forecasts, reference profiles and current states of charge |
| `hierarchy.py` | — | — | Names, capacities, flexibility limits and typical-usage shapes of the demo site |

```bash
docker compose exec server uv run --active --no-project /walkthrough/flexmeasures/seed_assets.py
docker compose exec server uv run --active --no-project /walkthrough/flexmeasures/seed_forecasts.py
```

Like `../python/trigger_fetch.py`, both scripts reuse the container's own uv-managed venv
(`/app/.venv`) and reach the database through the ORM, so they need no API token.

`hierarchy.py` holds everything declarative: what the site is made of, and what it
typically does. The two scripts stay focused on how to persist that.

## What `seed_assets.py` builds

```text
demo-campus (building, 1 MVA connection)
├── demo-evse-hub (building, 400 kVA shared sub-connection)
│   ├── demo-evse-01 … 06 (one-way_evse, 22 kW)
│   └── demo-evse-07 … 08 (two-way_evse, 11 kW, V2G)
└── demo-office (building, 250 kVA sub-connection)
    ├── demo-office-pv (solar, must-take production)
    ├── demo-office-baseload (process, inflexible consumption)
    └── demo-office-heat-pump (heat-storage, thermal buffer)
```

The campus holds the flex-context (day-ahead prices, site capacity, breach prices), which
FlexMeasures reads upwards through the tree. Each device holds a flex-model entry, which
FlexMeasures gathers downwards, so triggering a schedule on `demo-campus` covers the whole
site in one optimisation.

The heat pump follows FlexMeasures' own `Heat Pump Template`: its coefficient of
performance is the flex-model's `charging-efficiency` (the one-way conversion from
electricity to stored heat, which is allowed to exceed 100%, unlike `roundtrip-efficiency`
which the charge points use). Its `power` sensor is therefore electrical while its
`state of charge` sensor is thermal.

### The sensors that give flexible devices something to do

A storage device whose flex-model only bounds its state of charge (`soc-min`/`soc-max`) has
no reason to ever consume: an empty buffer costs nothing, so the cheapest plan is to leave
it idle at 0 kW forever, regardless of price or any capacity signal. That was true of 7 of
the demo's 9 flexible devices until three extra sensors were added, each of which drives a
flex-model field that gives the scheduler an actual energy requirement to plan around:

| Sensor | Unit | On | Drives | Purpose |
| --- | --- | --- | --- | --- |
| `power availability` | kW | each charge point | `power-capacity` | Zero while the bay is empty, the connected car's accepted power while occupied — replaces a static nameplate rating, which would let the scheduler charge an empty bay at 3am. |
| `minimum state of charge` | kWh | each charge point | `soc-minima` | The departure requirement, but recorded as nonzero in *only the single quarter-hour interval the car leaves in* — every other interval is explicitly `0.0`, not blank. This is the charge point's reason to consume at all: without it, an empty battery costs nothing and the cheapest plan is never to charge. |
| `heat demand` | kW (thermal) | `demo-office-heat-pump` | `soc-usage` | The actual drain on the thermal buffer, following the building's day: heaviest just before the first arrivals, falling back once the building is occupied and gaining incidental heat, low overnight and much lower at weekends. |

Two details matter for `minimum state of charge`: FlexMeasures gap-fills blank intervals on
a sensor forward from the last recorded value, so leaving the non-departure intervals blank
would silently oblige the car to stay full all day instead of only at departure — they are
written as `0.0` on purpose. And the campus flex-context carries a matching
`soc-minima-breach-price: "5 EUR/kWh"`, so a stay too short for its deficit produces a
costed shortfall instead of an infeasible schedule.

All three of these sensors are **forecasts**, written by `LF Energy demo forecaster` — they
describe the weather, building and driver behaviour the scheduler plans around, the same
category as prices and the office's inflexible load. That's different from the charge
points' and heat pump's own `power` sensors, which stay reference profiles: those describe
what the site would do unscheduled, whereas the scheduler now genuinely dispatches these
devices against the requirement.

It is idempotent: assets and sensors are fetched by name before being created, so
re-running it only reprints the summary.

## What `seed_forecasts.py` writes

Seven days of quarter-hourly data, from the quarter hour the run falls in. Three kinds of
belief, from two data sources, because they do not mean the same thing:

| Sensors | Data source | Kind |
| --- | --- | --- |
| `NL transmission zone/day-ahead prices`, `demo-office-baseload/power`, `demo-office-pv/power`, `demo-evse-01…08/power availability`, `demo-evse-01…08/minimum state of charge`, `demo-office-heat-pump/heat demand` | `LF Energy demo forecaster` (type `forecaster`) | Forecast |
| `demo-evse-01…08/power`, `demo-office-heat-pump/power` | `LF Energy demo profiles` (type `demo script`) | Reference profile |
| `demo-evse-01…08/state of charge`, `demo-office-heat-pump/state of charge` | `LF Energy demo profiles` (type `demo script`) | Measurement, one per device |

The distinction is deliberate. The price curve is what the scheduler optimises against, and
the office's background load and its rooftop PV are the site's `inflexible-consumption` and
`inflexible-production`: the scheduler cannot move them, so it needs to know what they will
do, and a forecast is exactly the right thing to give it. Each flexible device's *energy
requirement* — a charge point's power availability and departure requirement, the heat
pump's heat demand — is a forecast for the same reason: it describes the weather, the
building and the drivers, not anything the scheduler decides, so it belongs to the
forecaster too, even though it lives on the same devices the scheduler dispatches. The
charge points' and the heat pump's own `power` sensors are what the scheduler *does* move,
so a fixed week of power values for them is not a forecast of anything — it is what the site
would do unscheduled, kept only so the charts are not empty before the first schedule runs.
The scheduler writes its own beliefs, from its own source, over the same window.

All three energy-requirement sensors are generated from the same seeded draw of each day's
charging/heating stays as their device's reference profile, so a charge point's availability,
its departure requirement and its unscheduled power profile can never disagree about which
car showed up. The departure requirement (`minimum state of charge`) is written as an
explicit `0.0` in every quarter hour except the one the car leaves in, never left blank:
FlexMeasures gap-fills a blank interval on a sensor-referenced flex-model field forward from
the last recorded value, so a blank would inherit the departure figure and quietly oblige
the car to stay full all day.

Prices are the only profile written outside the demo account: the `day-ahead prices` sensor
lives on FlexMeasures' public `NL transmission zone` asset, which is what the campus'
flex-context points at. `seed_forecasts.py` fetches that sensor through `seed_assets.py`, so
the two scripts cannot end up disagreeing about its unit, resolution or knowledge horizon.
It is also the one input the scheduler refuses to run without: with an empty price sensor it
stops at `Prices unknown for planning window` before it evaluates anything else.

Both source names are specific to this walkthrough, so neither can be confused with the
plugin's own `OpenADR 3 VTN` source of genuinely fetched DR signals.

A belief counts as a forecast when its belief time precedes the sensor's knowledge time.
These power sensors use timely-beliefs' default knowledge horizon, which puts knowledge
time at the end of the event, so stamping the whole window as believed at local midnight
of the run day makes it read as a forecast throughout. That belief time is also fixed
within a run day, which is what lets a re-run merge the same rows back over themselves —
belief time is part of a belief's key.

The aggregate `power` sensors of `demo-campus`, `demo-evse-hub` and `demo-office` are left
empty on purpose: they are the scheduler's output, and a synthetic aggregate there would
disagree with the schedule as soon as one is triggered.

### The shapes

- **`day-ahead prices`** trough overnight, peak as the country wakes up, dip in the middle
  of the day while the sun is on the roofs and peak again in the early evening, with the
  level of the whole day drawn once and the weekend flatter. Roughly 0.02 to 0.28 EUR/kWh —
  that spread is what makes the scheduler bother to move anything.
- **`demo-office-baseload`** sits on a night floor of servers, network gear and standby
  losses, climbs over the hour before opening, dips shallowly over lunch and falls away
  over the hour after closing. Weekends keep the floor plus a small allowance for cleaning
  staff.
- **`demo-office-pv`** follows a half sine between sunrise and sunset, scaled by a cloud
  cover drawn once per day, so the week holds a mix of clear and overcast days. Values are
  positive, because the sensor records production as positive, and never exceed the
  array's nameplate rating.
- **`demo-office-heat-pump`** pre-heats the building in the small hours and only tops the
  buffer up while the building is occupied and generating its own incidental heat, with a
  deep setback at the weekend. This reference profile is what an unscheduled pre-heat block
  would look like; the scheduler decides the real dispatch against the `heat demand`
  forecast instead (see above), which is the drain on the same thermal buffer and is what
  actually obliges the scheduler to run the heat pump at all. Heaviest just before the first
  arrivals, falling back once the building is occupied and gaining incidental heat, low
  overnight and much lower at weekends — expressed in thermal kW, so it's the heat pump's
  coefficient of performance that decides what meeting it costs electrically.
- **`demo-evse-01 … 08`** see a commuter arrive around 08:00 and leave around 17:00 on most
  weekdays, with an occasional shorter stay before or after for an early bird or a late
  worker, and a near-empty hub at the weekend. Arrival, departure, energy demand and the
  power the car accepts are all drawn per charge point per day, so the hub's aggregate
  looks like a hub rather than like eight identical rectangles. No session ever exceeds its
  charge point's rating, and the two bidirectional charge points stay charge-only:
  discharging is a scheduling decision, not typical usage. The same drawn stays also produce
  each charge point's `power availability` and `minimum state of charge` forecasts (see
  above); only commuter stays carry a departure requirement, since a short stay before or
  after the working day is a top-up, not a promise of a full battery.

**General lesson:** a FlexMeasures storage device's flex-model needs an actual energy
requirement — `soc-usage`, `soc-minima`, or `soc-targets` — not just `soc-min`/`soc-max`
bounds, or the scheduler has no reason to ever move it: an empty buffer costs nothing, so
leaving it idle is always at least as cheap as running it. That was true of the heat pump
and of 6 of the 8 charge points in an earlier version of this demo, all of which sat at
0 kW in every schedule regardless of price or capacity signal, and is why every flexible
device here carries one of these fields now.

### Determinism

Every profile is drawn from a generator seeded on the sensor and the local calendar day,
and each day is generated whole before being cut back to the window. A given day therefore
always gets the same numbers, whether the script runs at dawn or at noon, and a re-run
merges identical rows back over themselves rather than drifting. This is a heavier form of
idempotency than `seed_assets.py`'s fetch-or-create — the week is regenerated on every run
— but the result is the same week.

The state-of-charge readings are the one exception: they are keyed on the quarter hour the
run falls in, so a re-run within that quarter hour rewrites them, while a later run records
a newer reading alongside the old one, which is what a fresh measurement should do.

Pass `--days` to seed a shorter or longer horizon, and `--account-name` to target an
account other than the walkthrough's toy account.

## Ties into the rest of the walkthrough

Once a VEN client and polling schedule exist (walkthrough steps 7 and 8), re-run
`seed_assets.py`: it points the campus' `site-consumption-capacity` and
`site-production-capacity` at the OpenADR import and export capacity-limit sensors, turning
the fetched DR signal into a scheduling constraint. Before then it reports the wiring as
skipped.

`../python/compare_schedules.py` triggers a schedule against this hierarchy twice —
once before this wiring exists and once after (walkthrough Steps 4 and 10) — and compares
the two plans, so you can see exactly what the capacity limits changed rather than just
that a sensor now holds new values.

## Before triggering a schedule

Each storage device reads its starting state of charge from its own `state of charge`
sensor, looking for a value within four time steps of the schedule start. `seed_forecasts.py`
records one per device at the quarter hour it runs in, so trigger the schedule shortly
after seeding — otherwise re-run the script, record a value manually, or pass `soc-at-start`
in the scheduling request. Without one, the scheduler stops with
`No recent state-of-charge value found for sensor <id>`.

See the [main walkthrough README](../README.md) for step-by-step instructions.
