# Capacity limits walkthrough

Hands-on example that mirrors the end-to-end test in `tests/test_ven_fetch_e2e.py`:

1. Start an OpenADR 3.1 VTN (OpenLEADR-rs) with Keycloak OAuth
2. Seed capacity-limit events on the VTN via Python (Business Logic client)
3. Configure a VEN client and polling schedule in the FlexMeasures UI
4. Fetch events and inspect beliefs on the linked sensors

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
- Python 3.12+ (for the seed script on your host)

---

## Step 1 — Start the stack

```bash
cd examples/capacity-limits-walkthrough
docker compose up -d --build
```

Wait until FlexMeasures is ready (first start runs DB migrations and creates a toy account):

```bash
docker compose logs -f server
```

Look for the gunicorn startup message, then open `http://localhost:5002` to verify the dashboard is visible (this can take up to 2 minutes during initial startup).

---

## Step 2 — Seed capacity-limit events on the VTN

The plugin has no CLI for VTN administration. Use the Business Logic OAuth client
(`test-client-id`) to create a 24-hour event with 96 fifteen-minute intervals with an import and export capacity limit event from OpenADR 3.1 for this example scenario.

```bash
cd python
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export OAUTHLIB_INSECURE_TRANSPORT=1
export OAUTHLIB_RELAX_TOKEN_SCOPE=1
python seed_events.py
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

## Step 3 — Log in to FlexMeasures

Open `http://localhost:5002` and sign in with the toy account created by Docker:

| Field | Value |
|-------|-------|
| Email | `toy-user@flexmeasures.io` |
| Password | `toy-password` |

Open **OpenADR 3 Configuration** in the main menu, or go directly to:

`http://localhost:5002/flexmeasures-openadr3/dashboard`

---

## Step 4 — Create a VEN client

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

## Step 5 — Add a polling schedule

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

Skip waiting for the daily schedule and run the fetch now:

```bash
docker compose exec server python /walkthrough/python/trigger_fetch.py
```

#### Option B — Wait for the scheduled job**

Set **Daily trigger time** to a UTC time 2–3 minutes from now, then save. Ensure the
`worker` container is running — it processes the `ingestion` queue where fetch jobs
are enqueued.

---

## Step 6 — Inspect sensor beliefs

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

---

## Troubleshooting

| Symptom | Check |
|---------|-------|
| Seed script cannot reach VTN | `docker compose ps` — is `openleadr-vtn` running? Wait ~30s after startup. |
| OAuth errors in seed script | Keycloak at `http://localhost:8080` — realm imported? |
| VEN client save fails | URLs must use Docker service names (`openleadr-vtn`, `keycloak`), not `localhost`. |
| Fetch job never runs | `docker compose ps worker` — worker must be up on the `forecasting` queue. |
| No beliefs after fetch | Re-run `seed_events.py`; event must exist on VTN before fetch. |
| HTTP blocked to VTN | `ALLOW_INSECURE_HTTP_VTN=true` is set in `docker-compose.yml`. |

## Reference values

All constants live in `python/settings.py`. OAuth clients are defined in
`keycloak/realm.json`:

| Client | ID | Secret | Used for |
|--------|----|--------|----------|
| Business Logic | `test-client-id` | `my-client-secret` | `seed_events.py` |
| VEN | `test-ven-1` | `my-client-secret` | FlexMeasures UI |

## Clean up

```bash
docker compose down -v
```

This removes containers and the FlexMeasures database volume.
