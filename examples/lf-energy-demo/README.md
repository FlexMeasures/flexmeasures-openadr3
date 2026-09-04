# LF Energy demo walkthrough

Hands-on example combining two [LF Energy](https://lfenergy.org/) projects — FlexMeasures
and OpenLEADR — into one demo stack. It mirrors the end-to-end test in
`tests/test_ven_fetch_e2e.py`, extended with a realistic three-level FlexMeasures asset
hierarchy so the fetched DR signal ends up constraining a real schedule instead of just
landing on a sensor:

1. Start an OpenADR 3.1 VTN (OpenLEADR-rs) with Keycloak OAuth, plus a FlexMeasures instance
2. Seed a demo campus / EVSE hub / office asset hierarchy in FlexMeasures
3. Seed capacity-limit events on the VTN via Python (Business Logic client)
4. Configure a VEN client and polling schedule in the FlexMeasures UI
5. Fetch events, then wire the resulting capacity-limit sensors into the site as a scheduling constraint
6. Inspect beliefs on the linked sensors

## Stack

| Service | URL (host) | Purpose |
|---------|------------|---------|
| FlexMeasures | http://localhost:5002 | Web UI + plugin dashboard |
| OpenLEADR-rs VTN | http://localhost:3000 | Publishes DR events |
| Keycloak | http://localhost:8080 | OAuth for BL and VEN clients |
| PostgreSQL (FM) | localhost:5433 | FlexMeasures database |
| Redis | localhost:6380 | RQ job queue |

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
(Step 6), it runs inside the FlexMeasures container against the ORM directly, reusing the
container's own uv-managed venv, so it needs no API token:

```bash
docker compose exec server uv run --active --no-project /walkthrough/flexmeasures/seed_assets.py
```

This needs the `Walkthrough Toy Account`, which the `server` container's entrypoint creates
on first boot (Step 1) — if it isn't there yet, wait for the gunicorn startup message and
try again. The script is idempotent: assets and sensors are looked up by name before being
created, so it's safe to re-run any time, including once more in Step 7.

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

## Step 3 — Seed capacity-limit events on the VTN

The plugin has no CLI for VTN administration. Use the Business Logic OAuth client
(`test-client-id`) to create a 24-hour event with 96 fifteen-minute intervals with an import and export capacity limit event from OpenADR 3.1 for this example scenario.

`seed_events.py` is a [uv script](https://docs.astral.sh/uv/guides/scripts/) — its
dependencies are declared inline, so `uv run` resolves and caches them on first use
without any manual venv setup.

```bash
cd python

export OAUTHLIB_INSECURE_TRANSPORT=1
export OAUTHLIB_RELAX_TOKEN_SCOPE=1
uv run seed_events.py
```

Expected output:

```
Created event id=… with 96 intervals.
  First interval start (UTC): …
  Last interval start  (UTC): …
```

The event starts **one hour from seed time** and spans 24 hours. Re-run
`seed_events.py` any time you want fresh data.

---

## Step 4 — Log in to FlexMeasures

Open `http://localhost:5002` and sign in with the toy account created by Docker:

| Field | Value |
|-------|-------|
| Email | `toy-user@flexmeasures.io` |
| Password | `toy-password` |

Open **OpenADR 3 Configuration** in the main menu, or go directly to:

`http://localhost:5002/flexmeasures-openadr3/dashboard`

---

## Step 5 — Create a VEN client

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

## Step 6 — Add a polling schedule

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

Skip waiting for the daily schedule and run the fetch now. `--active --no-project`
tells `uv run` to reuse the container's own venv (`/app/.venv`, where `flexmeasures`
and `flexmeasures_openadr3` are already installed) instead of resolving a fresh one:

```bash
docker compose exec server uv run --active --no-project /walkthrough/python/trigger_fetch.py
```

#### Option B — Wait for the scheduled job**

Set **Daily trigger time** to a UTC time 2–3 minutes from now, then save. Ensure the
`cron-scheduler` container is running — it's the dedicated process that evaluates
polling schedules and enqueues fetch jobs onto the `ingestion` queue at the
configured trigger time. The `worker` container must also be running — it's what
processes that queue.

---

## Step 7 — Wire the OpenADR capacity limits into the site

Now that `demo-ven`'s `demo-poll` schedule exists and has created its import/export
capacity-limit sensors, re-run the asset-seeding script from Step 2:

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

## Step 8 — Inspect sensor beliefs

After the fetch completes:

1. Go to **Polling schedules** for `demo-ven`
2. Click **Import sensor** or **Export sensor** next to `demo-poll`
3. On the sensor page, open the chart / beliefs view

You should see **96 beliefs** (one per 15-minute interval), sourced from **OpenADR 3 VTN**.
Each belief's `event_start` is the interval start time; values are random kW limits
between 0 and 100 (set by `seed_events.py`).

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
connection capacity alongside the wired OpenADR sensors from Step 7. From here, triggering a
schedule on `demo-campus` will optimise the whole hierarchy against the fetched capacity
limits — see the troubleshooting entry below about state-of-charge before you do.

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
| `seed_assets.py` reports OpenADR wiring as `not wired` | Expected until the VEN client and polling schedule exist (Steps 5-6); re-run it once they do (Step 7). |
| Scheduler fails with "No recent state-of-charge value found for sensor …" | Each storage-like device (the EVSEs and the heat pump) needs a recent belief on its `state of charge` sensor before you trigger a schedule; record one manually, or pass `soc-at-start` in the scheduling request. |

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
