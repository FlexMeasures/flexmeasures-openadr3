# LF Energy demo walkthrough

Hands-on example combining two [LF Energy](https://lfenergy.org/) projects — FlexMeasures
and OpenLEADR — into one demo stack. It mirrors the end-to-end test in
`tests/test_ven_fetch_e2e.py`, extended with a realistic three-level FlexMeasures asset
hierarchy so the fetched DR signal ends up constraining a real schedule instead of just
landing on a sensor:

1. Start an OpenADR 3.1 VTN (OpenLEADR-rs) with Keycloak OAuth, plus a FlexMeasures instance
2. Seed a demo campus / EVSE hub / office asset hierarchy in FlexMeasures
3. Seed a week of prices, forecasts and typical-usage profiles onto that hierarchy's sensors
4. Trigger a baseline schedule for the campus, before any OpenADR wiring
5. Seed capacity-limit events on the VTN via Python (Business Logic client)
6. Configure a VEN client and polling schedule in the FlexMeasures UI
7. Fetch events, then wire the resulting capacity-limit sensors into the site as a scheduling constraint
8. Trigger the same schedule again and compare it with the baseline, to see what OpenADR changed
9. Inspect beliefs on the linked sensors

## Stack

| Service | URL (host) | Purpose |
|---------|------------|---------|
| FlexMeasures | http://localhost:5002 | Web UI + plugin dashboard |
| OpenLEADR-rs VTN | http://localhost:3000 | Publishes DR events |
| Keycloak | http://localhost:8080 | OAuth for BL and VEN clients |
| PostgreSQL (FM) | localhost:5433 | FlexMeasures database |
| Redis | localhost:6380 | RQ job queue |

Three more services run alongside these with no exposed port, so they only show up in
`docker compose ps`, not in the table above: `worker` (processes the OpenADR fetch queue,
`ingestion`), `scheduling-worker` (processes triggered scheduling jobs, on its own
`scheduling` queue — Steps 4 and 10 need it), and `cron-scheduler` (evaluates polling
schedules and enqueues fetch jobs at their configured time). `docker compose up -d` starts
all three automatically; if a triggered schedule sits `QUEUED` forever, it's usually because
`scheduling-worker` isn't running (see [Troubleshooting](#troubleshooting)).

## Prerequisites

- Docker and Docker Compose
- [uv](https://docs.astral.sh/uv/) (for running the Python scripts, on your host and inside the container)

---

## Step 1 — Start the stack

```bash
cd examples/lf-energy-demo
docker compose up -d --build
```

Wait until FlexMeasures is ready (first start runs DB migrations and creates a toy account):

```bash
docker compose logs -f server
```

Look for the gunicorn startup message, then open `http://localhost:5002` to verify the dashboard is visible (this can take up to 2 minutes during initial startup).

---

## Step 2 — Seed the FlexMeasures asset hierarchy

`seed_assets.py` builds a realistic three-level demo site — a campus with an EVSE charging
hub and a grid-friendly office — as real `GenericAsset` and `Sensor` rows, so the capacity
limits fetched later in this walkthrough have a site to constrain. Like `trigger_fetch.py`
(Step 8), it runs inside the FlexMeasures container against the ORM directly, reusing the
container's own uv-managed venv, so it needs no API token:

```bash
docker compose exec server uv run --active --no-project /walkthrough/flexmeasures/seed_assets.py
```

This needs the `Walkthrough Toy Account`, which the `server` container's entrypoint creates
on first boot (Step 1) — if it isn't there yet, wait for the gunicorn startup message and
try again. The script is idempotent: assets and sensors are looked up by name before being
created, so it's safe to re-run any time, including once more in Step 9.

It builds:

```
demo-campus              building, 1 MVA grid connection point
├── demo-evse-hub        building, 400 kVA shared sub-connection
│   ├── demo-evse-01…06  one-way_evse, 22 kW
│   └── demo-evse-07…08  two-way_evse, 11 kW (V2G)
└── demo-office          building, 250 kVA sub-connection
    ├── demo-office-pv           solar, must-take production
    ├── demo-office-baseload     process, inflexible consumption
    └── demo-office-heat-pump    heat-storage, thermal buffer
```

`demo-office-heat-pump` models a heat pump plus the building's thermal mass as a
`heat-storage` device, since FlexMeasures has no dedicated heat-pump asset type. `demo-campus`
carries the flex-context (day-ahead prices, site capacity, breach prices), which FlexMeasures
reads upwards through the tree; every device below it carries its own flex-model entry, which
FlexMeasures gathers downwards, so triggering a schedule on `demo-campus` optimises the whole
site in one pass. Open `http://localhost:5002` and browse to the campus asset to see it — the
script's closing line prints a direct link.

See [`flexmeasures/README.md`](flexmeasures/README.md) for more detail on the hierarchy.

---

## Step 3 — Seed a week of prices, forecasts and usage profiles

The hierarchy from Step 2 has sensors but no data, so its charts are empty and a schedule
would have nothing to plan around — not even a price to optimise against.
`seed_forecasts.py` fills the coming seven days with synthetic but plausible data, and runs
in the container just like Step 2:

```bash
docker compose exec server uv run --active --no-project /walkthrough/flexmeasures/seed_forecasts.py
```

It writes three different kinds of belief, from two clearly-labelled data sources, because
they do not mean the same thing:

| Sensors | Data source | Meaning |
|---------|-------------|---------|
| `day-ahead prices`, `demo-office-baseload/power`, `demo-office-pv/power` | `LF Energy demo forecaster` (`forecaster`) | Real forecasts. The prices are what the scheduler optimises against; the other two are the site's `inflexible-consumption` and `inflexible-production`, which it reads as given and plans around. |
| `demo-evse-01…08/power`, `demo-office-heat-pump/power` | `LF Energy demo profiles` (`demo script`) | Reference profiles: what the site would do if nobody optimised it. These devices are dispatched by the scheduler, which writes its own beliefs over the same window from its own source. |
| `demo-evse-01…08/state of charge`, `demo-office-heat-pump/state of charge` | `LF Energy demo profiles` (`demo script`) | One current reading per storage device, so `soc-at-start` resolves and a schedule can be triggered straight away. |

The shapes are what you would expect of an office campus on the Dutch market: prices trough
overnight, peak as the country wakes up, dip in the middle of the day while the sun is on
the roofs and peak again in the early evening; the background load sits on a night floor of
servers and standby, climbs over the hour before opening, dips over lunch and falls away
after 18:00, with a much lower weekend; the PV follows a daylight bell curve whose height
varies from day to day with cloud cover; the heat pump pre-heats the building in the small
hours and only tops up while it is occupied; and the charge points fill up over the office
day, most of them plugging in around 08:00 and unplugging around 17:00, with the occasional
shorter stay for an early bird or a late worker.

The price curve is written to the public `day-ahead prices` sensor on FlexMeasures' own
`NL transmission zone` asset, which is what `demo-campus`'s flex-context already points at.
It is also the one thing the scheduler refuses to run without: with an empty price sensor it
stops at `Prices unknown for planning window` before it looks at anything else.

The values are deterministic — each profile is drawn from a generator seeded on the sensor
and the local calendar day — so a re-run merges the same rows back over themselves instead
of producing a different week. Re-run it whenever the week has moved on, or pass
`--days` to seed a shorter or longer horizon.

The aggregate `power` sensors of `demo-campus`, `demo-evse-hub` and `demo-office` are left
empty on purpose: those are the scheduler's output, and a synthetic aggregate there would
simply disagree with the schedule once you trigger one.

---

## Step 4 — Trigger a baseline schedule, before OpenADR

With a week of prices and forecasts in place but no OpenADR wiring yet, trigger one
schedule for `demo-campus` and save the plan it comes up with on its own — the "before"
half of the before-and-after comparison this walkthrough builds towards.

`compare_schedules.py` is a [uv script](https://docs.astral.sh/uv/guides/scripts/) like
`seed_events.py`: it runs on your host rather than in a container, and only ever talks HTTP
to FlexMeasures at `http://localhost:5002`.

```bash
cd python

uv run compare_schedules.py
```

From `examples/lf-energy-demo`, this moves you into `python/`; this and the other Python
steps below assume you're still there, and any later `docker compose` command needs you
back at `examples/lf-energy-demo` (`cd ..`).

The script triggers a schedule, waits for the scheduling job to finish, and saves the
result to `../schedule-runs/before-openadr.json`. It works out that this is the "before"
run itself, by reading `demo-campus`'s current flex-context: since the OpenADR
capacity-limit fields aren't wired in yet (that happens in Step 9), it labels the run
`before-openadr`. It then prints a note that there's nothing to compare yet — that's
expected, and gets resolved once you run it again in Step 10.

If this fails because a triggered schedule never leaves `QUEUED`, see
[Troubleshooting](#troubleshooting) — it usually means the `scheduling-worker` service
isn't running.

---

## Step 5 — Seed capacity-limit events on the VTN

The plugin has no CLI for VTN administration. Use the Business Logic OAuth client
(`test-client-id`) to create a 24-hour event with 96 fifteen-minute intervals with an import and export capacity limit event from OpenADR 3.1 for this example scenario.

`seed_events.py` is a [uv script](https://docs.astral.sh/uv/guides/scripts/) — its
dependencies are declared inline, so `uv run` resolves and caches them on first use
without any manual venv setup. It's still in `python/` from Step 4:

```bash
uv run seed_events.py
```

Expected output:

```
Created event id=… with 96 intervals.
  First interval start (UTC): …
  Last interval start  (UTC): …
  Import capacity is 400 kW, dropping to 70 kW from … for 10:00:00.
  That is Tue 08:00 to 18:00 local time (Europe/Amsterdam).
```

The event spans 24 hours. Import capacity stays generous (400 kW) for most of that day and
drops to a hard **70 kW** for a 10-hour window; export capacity stays generous throughout.

Unlike the event start, the curtailment window itself is **pinned to a fixed local
wall-clock window — 08:00 to 18:00 in `Europe/Amsterdam`, the demo site's whole office
day** — rather than offset from whenever the script happens to run, and rather than some
narrower slice of it. That's deliberate, not incidental: taking the office day whole
guarantees the curtailment overlaps the EVSE fleet's entire presence (arrivals centred just
after opening, the earliest departures mid-afternoon) and the heat pump's whole occupied
period, so nothing flexible is left sitting outside the ask with somewhere easy to hide. It
also reaches past the day-ahead price curve's midday solar dip at both ends — into the
morning ramp near opening and the evening ramp near closing — hours where the price signal
alone would not have discouraged load, and in the morning would have positively encouraged
filling up before the peak. A flat 70 kW cap held across all of that is unmistakably the
capacity limit's doing rather than price optimisation happening to agree with it.

One honest caveat: because the window now opens right at office opening, before the site's
rooftop PV has come up under a baseload that has already switched to its day level, a cloudy
morning can leave the site's own inflexible load at or just above 70 kW for the first
interval or two — load the scheduler cannot move out of the way no matter how it plans. That
residual breach is honest rather than a misconfiguration; a real congestion ask does not stop
at what the site finds convenient. So Step 10's report should normally show the intervals
over the limit falling to a low number, but not always cleanly to zero — how low depends on
the day's seeded cloud cover. In the representative run below, seeded weather happened to be
clear enough that it did fall to zero.

It's one clear, deliberate curtailment window rather than noise, precisely so that Step 10's
before-and-after comparison has one obvious thing to show. Re-run `seed_events.py` any time
you want fresh data, but remove the previous event from the VTN first — see
[Troubleshooting](#troubleshooting).

**Edge case:** if local time is already inside 08:00-18:00 when you run the script, the
pinned window would no longer fit inside the 24-hour horizon Steps 4 and 10 schedule
against, so the script falls back to a short-notice curtailment starting almost immediately
instead, and prints a note explaining why. Because the window is now a full 10 hours rather
than a narrow 4-hour slice, this fallback path is common rather than a rare edge case: the
office day only fits inside the coming 24-hour schedule horizon when the script happens to
run outside office hours, so a walkthrough run *during* the day typically takes the fallback.
The comparison still works, it just no longer
tells the "reaches into the price curve's morning and evening peaks" story above — the
short-notice window instead runs from roughly now for an office day's worth of hours, still
plugged-in-fleet-first but no longer aligned with the site's actual opening and closing
times.

---

## Step 6 — Log in to FlexMeasures

Open `http://localhost:5002` and sign in with the toy account created by Docker:

| Field | Value |
|-------|-------|
| Email | `toy-user@flexmeasures.io` |
| Password | `toy-password` |

Open **OpenADR 3 Configuration** in the main menu, or go directly to:

`http://localhost:5002/flexmeasures-openadr3/dashboard`

---

## Step 7 — Create a VEN client

Click **Add new VEN client** and fill in the form.

> Use **Docker-internal hostnames** for VTN and OAuth URLs. FlexMeasures resolves
> these from inside the `server` and `worker` containers.

| Field | Value |
|-------|-------|
| **Name** | `demo-ven` |
| **VTN URL** | `http://openleadr-vtn:3000` |
| **OAuth client ID** | `test-ven-1` |
| **OAuth client secret** | `my-client-secret` |
| **OAuth token URL** | `http://keycloak:8080/realms/integration-test-realm/protocol/openid-connect/token` |
| **Scopes** | *(leave empty — Keycloak default scopes are used)* |

Click **Create VEN client**.

These credentials come from `keycloak/realm.json`, this file is human readable and can be used to view the exact configuration of the configured OAUTH clients which communicate with an OpenADR 3.1 VTN.

---

## Step 8 — Add a polling schedule

On the dashboard, open **demo-ven** → **Add schedule** (or **Polling schedules** → **Add schedule**).

| Field | Value |
|-------|-------|
| **Name** | `demo-poll` |
| **Targets** | *(leave empty — fetch all events)* |
| **Daily trigger time (UTC)** | See options below |
| **Import capacity limits** | ✓ checked |
| **Export capacity limits** | ✓ checked |

Click **Save polling schedule**. The plugin creates two sensors on the VEN asset:

- `import-capacity-limit-demo-poll`
- `export-capacity-limit-demo-poll`

### When to run the fetch

The schedule runs **once per day** at the configured UTC time. For this walkthrough you have two options:

#### Option A — Trigger immediately (recommended)**

Skip waiting for the daily schedule and run the fetch now. If you're still in `python/`
from Step 4 or 5, `cd ..` first — this runs in the FlexMeasures container, from
`examples/lf-energy-demo`. `--active --no-project` tells `uv run` to reuse the container's
own venv (`/app/.venv`, where `flexmeasures` and `flexmeasures_openadr3` are already
installed) instead of resolving a fresh one:

```bash
docker compose exec server uv run --active --no-project /walkthrough/python/trigger_fetch.py
```

#### Option B — Wait for the scheduled job**

Set **Daily trigger time** to a UTC time 2–3 minutes from now, then save. Ensure the
`cron-scheduler` container is running — it's the dedicated process that evaluates
polling schedules and enqueues fetch jobs onto the `ingestion` queue at the
configured trigger time. The `worker` container must also be running — it's what
processes that queue. (This is a different queue, and a different container, from the
`scheduling-worker` that Steps 4 and 10 need for triggered schedules.)

---

## Step 9 — Wire the OpenADR capacity limits into the site

Now that `demo-ven`'s `demo-poll` schedule exists and has created its import/export
capacity-limit sensors, re-run the asset-seeding script from Step 2 (from
`examples/lf-energy-demo`; `cd ..` first if you're still in `python/`):

```bash
docker compose exec server uv run --active --no-project /walkthrough/flexmeasures/seed_assets.py
```

The asset hierarchy itself is unchanged by a re-run, but the script always re-evaluates the
OpenADR wiring: it points `demo-campus`'s `site-consumption-capacity` and
`site-production-capacity` flex-context fields at the import/export sensors it finds, turning
the fetched DR signal into an actual scheduling constraint. Its summary line reports what it
wired, e.g.:

```
OpenADR site capacity limits: site-consumption-capacity -> import-capacity-limit-demo-poll, site-production-capacity -> export-capacity-limit-demo-poll
```

If you run this before the VEN client or polling schedule exist (or before Step 2), it
reports the wiring as `not wired: …` instead of failing — that's expected until you reach
this step.

---

## Step 10 — Trigger the after-OpenADR schedule, see the comparison

With the capacity limits wired in, trigger `demo-campus` a second time with the same
script, so you can see exactly what changed:

```bash
cd python

uv run compare_schedules.py
```

The script reuses the exact time window from Step 4's run automatically, so the two plans
are comparable interval by interval even if you spent a while working through Steps 5–9 in
between — pass `--fresh-window` if you'd rather start a new comparison from now. It reads
`demo-campus`'s flex-context again, this time finds the OpenADR fields wired in from Step 9,
and labels this run `after-openadr`.

Once both runs exist, the script automatically prints a before-and-after report — peak
import/export, energy imported/exported, cost at day-ahead prices, how many intervals
exceed the OpenADR import limit, and the largest interval-by-interval changes at the grid
connection — and writes `../schedule-runs/comparison.html`: open it in a browser for a
multi-panel diagram overlaying both plans (the campus grid connection, the EVSE hub, and
the heat pump), with the day-ahead price and the OpenADR-curtailed window (dashed capacity
limit line, always visible) underneath. The diagram opens showing only the **Before
OpenADR** plan; a small control bar of checkboxes above the chart lets you bring in **With
OpenADR** as well (or instead — every combination of the two, including neither, is
reachable), and the toggle applies to every panel at once. That diagram is the clearest
single artifact of this whole walkthrough: switch on "With OpenADR" and, outside the
curtailment window, the two plans are drawn exactly on top of each other — they're
genuinely identical, the same price-driven optimum, not a rendering artifact — while inside
it you can see load pulled off the grid connection to respect the 70 kW import limit from
Step 5, with the heat pump's own panel showing it shifting its draw on the thermal buffer
rather than sitting idle. Because the window now spans the whole office day, that
"identical" stretch is confined to the hours outside 08:00-18:00, not most of the day.

A representative run against the seeded demo data looks like this:

```
peak import              129 kW -> 70.0 kW   (-59 kW, pinned exactly to the new limit)
intervals over limit     12 -> 0 (of 96)
worst breach             58.7 kW -> 0.0 kW
energy above limit       55.0 kWh -> 0.0 kWh
energy moved             51.1 kWh (site) + 36.9 kWh (EVSE hub) + 34.4 kWh (heat pump)
cost at day-ahead prices 102.7 EUR -> 104.4 EUR (+1.7 EUR)
```

The "before" plan arches up to ~129 kW through the curtailment window because, absent any
constraint, that's when the site's price-optimal plan happens to want to charge the most —
the fleet is plugged in and the office is occupied for the whole of it. The "with OpenADR"
plan is pinned flat at 70 kW for the same ten hours, with the deferred energy picked up in
cheaper intervals either side of the window — at the cost of just under 2 EUR across the
whole day, for a curtailment that fully honours a 12-interval, 55.0 kWh breach. As noted in
Step 5, that clean fall to zero intervals over the limit is typical but not guaranteed: a
cloudier morning in the seeded weather can leave a residual breach right at the window's
opening edge, before rooftop PV has caught up with the office's day-level baseload.

These numbers assume the schedule window falls on a weekday. The demo's charge points are
only 10% occupied on a weekend (vs. 85% on a weekday) and the office's heat demand drops too,
so running this step on a Saturday or Sunday will show a much weaker before-and-after
difference — there's little flexible load left for the curtailment to bite into. See
[Troubleshooting](#troubleshooting) if that's what you're seeing.

**If the script refuses to trigger**, naming a stale or missing price window, too much time
passed since `seed_forecasts.py` last ran — it only seeds seven days from the moment it's
run, and a walkthrough left overnight or over a weekend can outlive that window. Re-seed it
and start a fresh comparison:

```bash
docker compose exec server uv run --active --no-project /walkthrough/flexmeasures/seed_forecasts.py
```

then re-run both Step 4 and this step (or pass `--fresh-window` to skip straight to a new
comparison without repeating Step 4).

---

## Step 11 — Inspect sensor beliefs

After the fetch completes:

1. Go to **Polling schedules** for `demo-ven`
2. Click **Import sensor** or **Export sensor** next to `demo-poll`
3. On the sensor page, open the chart / beliefs view

You should see **96 beliefs** (one per 15-minute interval), sourced from **OpenADR 3 VTN**.
Each belief's `event_start` is the interval start time; values follow the shaped
import/export capacity limits set by `seed_events.py` (Step 5) — a generous 400 kW for most
of the day, dropping to a hard 70 kW over one deliberate 10-hour window pinned to
08:00-18:00 local time (the demo site's whole office day).

To verify from the worker logs:

```bash
docker compose logs worker | tail -20
```

Look for a line like:

```
Fetched DR events for VEN 'demo-ven' … stored 192 beliefs.
```

(192 = 96 import + 96 export intervals stored across both sensors.)

You can also open `demo-campus` in the FlexMeasures UI: its dashboard now plots the
connection capacity alongside the wired OpenADR sensors from Step 9, over the week of data
seeded in Step 3. From here, triggering a schedule on `demo-campus` will optimise the whole
hierarchy against the fetched capacity limits — which is exactly what Step 10 already did,
and its `comparison.html` is the fastest way to see the effect without reading beliefs one
sensor at a time.

---

## Troubleshooting

| Symptom | Check |
|---------|-------|
| Seed script cannot reach VTN | `docker compose ps` — is `openleadr-vtn` running? Wait ~30s after startup. |
| OAuth errors in seed script | Keycloak at `http://localhost:8080` — realm imported? |
| VEN client save fails | URLs must use Docker service names (`openleadr-vtn`, `keycloak`), not `localhost`. |
| Fetch job never runs | `docker compose ps` — both `cron-scheduler` (evaluates the schedule) and `worker` (processes the `ingestion` queue) must be up. |
| No beliefs after fetch | Re-run `seed_events.py`; event must exist on VTN before fetch. |
| HTTP blocked to VTN | `ALLOW_INSECURE_HTTP_VTN=true` is set in `docker-compose.yml`. |
| `seed_assets.py` fails with "No account named …" | The `Walkthrough Toy Account` is created by the `server` container's entrypoint on first boot; wait for `docker compose logs -f server` to show the gunicorn startup message, then re-run. |
| `seed_assets.py` reports OpenADR wiring as `not wired` | Expected until the VEN client and polling schedule exist (Steps 7-8); re-run it once they do (Step 9). |
| `seed_forecasts.py` fails with "No asset named …" | It seeds data onto the hierarchy from Step 2; run `seed_assets.py` first. |
| Scheduler fails with "Prices unknown for planning window" | The `day-ahead prices` sensor is empty over the schedule window; run `seed_forecasts.py` (Step 3), or re-run it if the seeded week has run out. |
| Scheduler fails with "No recent state-of-charge value found for sensor …" | Each storage-like device (the EVSEs and the heat pump) needs a recent belief on its `state of charge` sensor before you trigger a schedule. `seed_forecasts.py` (Step 3) records one per device at the quarter hour it runs in; if that was hours ago, re-run it, record a value manually, or pass `soc-at-start` in the scheduling request. |
| Charts are empty, or a schedule assumes zero load | Run `seed_forecasts.py` (Step 3), and re-run it once the seeded week has run out. |
| A triggered schedule (Step 4 or Step 10) stays `QUEUED` forever | `docker compose ps` — the `scheduling-worker` service must be running; check `docker compose logs scheduling-worker`. It's separate from `worker`, which only serves the `ingestion` queue. |
| `compare_schedules.py` refuses to trigger, naming a stale or missing price window | The seeded forecast/price data only covers seven days from whenever `seed_forecasts.py` last ran. Re-run it (see Step 10), or pass `--fresh-window` to start a new comparison instead of reusing Step 4's window. |
| `seed_events.py` run a second time fails on the next fetch with `OverlappingDemandResponseEventsError` | The previous event is still on the VTN; remove it first, or just don't re-run `seed_events.py` mid-walkthrough — one event is all this flow needs. |
| `seed_events.py` prints a note and the curtailment starts almost immediately instead of at 08:00 local time | Expected if you ran it while local time was already inside the pinned 08:00-18:00 window — that 10-hour office-day window no longer fits inside the 24-hour schedule horizon, so the script falls back to short notice. This is now the common case, not a rare edge case: the pinned window only fits inside the coming 24 hours when the script runs outside office hours, so a walkthrough run during the day typically takes this fallback. The comparison in Step 10 still works, it just no longer reaches into the price curve's morning and evening peaks the way the pinned window does. |
| Step 10's before-and-after comparison shows only a weak difference | Check what day the schedule window falls on: the EVSE fleet's weekend occupancy is only 10% (vs. 85% on weekdays) and heat demand is much lower too, so there's little flexible load left for the curtailment to bite into. Run the walkthrough on a weekday for the full effect. |
| `seed_assets.py` still fails with "No account named …" even after a fresh `docker compose down -v` | `fm-walkthrough-instance/.toy-account-created` is a host-side marker file that survives the volume reset, so the `server` container's entrypoint skips re-running `flexmeasures add toy-account` against the fresh database. Delete that marker file (or run `flexmeasures add toy-account --name 'Walkthrough Toy Account'` by hand in the container), then re-run `seed_assets.py`. |

## Reference values

All constants live in `python/settings.py`. OAuth clients are defined in
`keycloak/realm.json`:

| Client | ID | Secret | Used for |
|--------|----|--------|----------|
| Business Logic | `test-client-id` | `my-client-secret` | `seed_events.py` |
| VEN | `test-ven-1` | `my-client-secret` | FlexMeasures UI |

Asset names, capacities and flex-model parameters for the demo hierarchy live in
`flexmeasures/hierarchy.py`.

## Clean up

```bash
docker compose down -v
```

This removes containers and the FlexMeasures database volume.
