# Running this walkthrough as a live demo

A leaner companion to the [main walkthrough](README.md), reordered for presenting it live:
seed everything that has no UI equivalent up front, then do as much as possible in the
FlexMeasures UI in front of an audience. Each step below is tagged **[UI]** or **[CLI]**.
Field values, form labels and troubleshooting are not repeated here — see the main README
for those; this doc is about *what to click versus what to run*, and in what order.

## At a glance

| # | Step | Where |
|---|------|-------|
| 1 | Seed assets + a week of forecasts | **[CLI]** |
| 2 | Trigger the baseline ("before") schedule | **[CLI]** |
| 3 | Seed the capacity-limit event on the VTN | **[CLI]** — no UI equivalent |
| 4 | Log in, open OpenADR 3 Configuration | **[UI]** |
| 5 | Create the VEN client | **[UI]** |
| 6 | Add the polling schedule, then trigger the fetch | **[UI]** + **[CLI]** — no UI trigger |
| 7 | Show the fetched OpenADR sensor values | **[UI]** |
| 8 | Nest the VEN asset under the campus | **[UI]** (or **[CLI]**) |
| 9 | Wire the flex-context live | **[UI]** |
| 10 | Trigger the after-OpenADR schedule | **[CLI]** — no UI equivalent |
| 11 | Show the shift in power usage | **not available in the FlexMeasures UI** — custom HTML report |

## 1. Seed assets and forecasts — [CLI]

Same as the main README's Steps 2-3:

```bash
cd examples/lf-energy-demo
docker compose exec server uv run --active --no-project /walkthrough/flexmeasures/seed_assets.py
docker compose exec server uv run --active --no-project /walkthrough/flexmeasures/seed_forecasts.py
```

## 2. Trigger the baseline ("before") schedule — [CLI]

```bash
cd python
uv run compare_schedules.py
```

**Do this now, before wiring anything.** This is easy to get wrong when reordering steps
for a live flow: there is no "before" plan to compare against later unless you trigger it
*before* the flex-context is wired in Step 9. If you skip this and only trigger once at the
end, Step 11's comparison report will refuse to run (it needs both a `before-openadr` and
an `after-openadr` saved run).

## 3. Seed the capacity-limit event on the VTN — [CLI], no UI equivalent

Still in `python/`:

```bash
uv run seed_events.py
```

The plugin has no VTN-administration CLI or UI — this talks to the OpenLEADR-rs VTN
directly via the Business Logic OAuth client. There is nothing to show on screen here
beyond the script's own printed summary.

## 4. Log in, open OpenADR 3 Configuration — [UI]

`http://localhost:5002`, sign in with the toy account, then **OpenADR 3 Configuration** in
the main menu (or `/flexmeasures-openadr3/dashboard` directly).

## 5. Create the VEN client — [UI]

**Add new VEN client**, fill in the form (name `demo-ven`, VTN URL, OAuth client
id/secret/token URL — see the main README's Step 7 table for exact values), **Create VEN
client**.

## 6. Add the polling schedule — [UI], then trigger the fetch — [CLI]

On the dashboard, **demo-ven** → **Add schedule**: name `demo-poll`, check both import and
export capacity limits, **Save polling schedule**.

There's no UI button to run the fetch immediately — trigger it from the host, `cd ..` back
to `examples/lf-energy-demo` first:

```bash
docker compose exec server uv run --active --no-project /walkthrough/python/trigger_fetch.py
```

## 7. Show the fetched OpenADR sensor values — [UI]

Genuinely built into FlexMeasures, nothing custom here: **Polling schedules** for
`demo-ven` → **Import sensor** or **Export sensor** next to `demo-poll` → the sensor's
chart/beliefs view. You should see 96 beliefs, sourced from **OpenADR 3 VTN**.

## 8. Nest the VEN asset under the campus — [UI] (or [CLI])

Without this step, Step 9's sensor search cannot find `demo-ven`'s sensors: FlexMeasures'
"Edit flex-context" picker only searches an asset's own descendants, and `demo-ven` is
otherwise created as an unrelated top-level asset.

**Option A (UI) — move it live:** every asset page has a view switcher next to its
breadcrumb (**Context** / **Graphs** / **Properties** / **Audit Log** / **Status**).
Open `demo-ven`'s asset page — find it via **Assets** in the main nav, since the OpenADR
plugin's own pages don't link out to it — switch to **Properties**, and in the "Edit asset" panel on
the left set **Parent Asset Id** to `demo-campus (ID: ...)` — it appears in that dropdown
automatically, since it's an ordinary asset-edit field listing every other asset in the
account, not something specific to VEN clients. Click **Save**.

**Option B (CLI):**

```bash
docker compose exec server uv run --active --no-project /walkthrough/flexmeasures/seed_assets.py --skip-openadr-wiring
```

Nests `demo-ven` under `demo-campus` the same way, and additionally leaves
`site-consumption-capacity`/`site-production-capacity` unset (with `--skip-openadr-wiring`)
so Step 9 has something real to do — useful for a non-interactive run, or as a fallback if
you'd rather not click through Option A live.

Either way, you'll see `demo-ven` appear in `demo-campus`'s "Structure" tab from now on.

## 9. Wire the flex-context live — [UI]

Open `demo-campus`'s asset page, **Edit flex-context**:

1. Select `site-consumption-capacity` → **Add field** → search for and pick
   `import-capacity-limit-demo-poll`.
2. Select `site-production-capacity` → **Add field** → search for and pick
   `export-capacity-limit-demo-poll`.
3. **Save**.

This is the step the whole demo has been building toward: the DR signal fetched from the
VTN is now a real scheduling constraint, wired in without touching a script. It's also
safe: the modal loads `demo-campus`'s *entire* existing flex-context (day-ahead prices,
`site-power-capacity`, breach prices, and so on) into the editor first, and **Add field**
only adds to that in-memory object — **Save** always PATCHes the whole thing back, so it
cannot clobber the fields `seed_assets.py` already set. (That's not automatic if you ever
drive the same API by hand instead of the UI — a partial `PATCH .../assets/8` with just the
two new fields silently wipes the rest of the flex-context. The UI form doesn't have that
failure mode.)

## 10. Trigger the after-OpenADR schedule — [CLI], no UI equivalent

```bash
cd python
uv run compare_schedules.py
```

FlexMeasures has **no "Trigger schedule" button anywhere in its UI** — the only trigger
button in the whole product is "Trigger forecast" on a sensor page, which is a different
thing. Triggering a schedule is an API call (`POST .../schedules/trigger`) plus a queued
job, full stop; this script is the only way to do it in this walkthrough. It reads
`demo-campus`'s flex-context again, finds the fields from Step 9, and labels this run
`after-openadr`.

This absence isn't specific to `demo-campus` — there is no "Trigger schedule" button on
*any* asset's page, campus or child. You also don't need one: triggering the top-level
`demo-campus` already schedules the whole hierarchy in one pass. The script's own output
shows this — one trigger call produces a plan for the campus's own grid connection *and*
its children:

```
Campus grid connection: 96 values on demo-campus/power
EVSE charging hub: 96 values on demo-evse-hub/power
Office heat pump: 96 values on demo-office-heat-pump/power
```

## 11. Show the shift in power usage — not available in the FlexMeasures UI

This is the one thing you cannot show natively in FlexMeasures, for a structural reason
worth explaining to an audience rather than working around: both triggered runs write
their schedules as beliefs to the *same* sensors, from the *same* `scheduler` data source.
Any FlexMeasures chart — or the schedule-fetch API endpoint itself — only ever shows the
most recent one; there's no concept of "run A" vs "run B" to overlay. (See
`python/schedule_comparison.py`'s module docstring for the full explanation.)

That's why `compare_schedules.py` saves each run to its own file
(`../schedule-runs/{before,after}-openadr.json`) the moment it's triggered, and why, once
both exist (after this step), it automatically prints a before-and-after report and writes
`../schedule-runs/comparison.html` — open that in a browser for the multi-panel diagram
with a toggle between "Before OpenADR" and "With OpenADR". This is a custom artifact of
this repo, not a FlexMeasures feature.

If you still want *something* on screen inside FlexMeasures itself: `demo-campus`'s own
asset dashboard plots the connection capacity alongside the wired OpenADR sensors over the
seeded week — a real UI feature, just without a before/after toggle.
