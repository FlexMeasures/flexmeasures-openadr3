# FlexMeasures asset hierarchy

Structural seeding for the [LF Energy demo walkthrough](../README.md): builds the demo
site as real `GenericAsset` and `Sensor` rows, so the OpenADR capacity limits fetched
elsewhere in the walkthrough have a site to constrain.

| File | Run where | Run with | Purpose |
| --- | --- | --- | --- |
| `seed_assets.py` | FlexMeasures container | `uv run --active --no-project /walkthrough/flexmeasures/seed_assets.py` | Create the demo asset hierarchy in the `Walkthrough Toy Account` |
| `hierarchy.py` | — | — | Names, capacities and flexibility limits of the demo site |

```bash
docker compose exec server uv run --active --no-project /walkthrough/flexmeasures/seed_assets.py
```

Like `../python/trigger_fetch.py`, this script reuses the container's own uv-managed venv
(`/app/.venv`) and reaches the database through the ORM, so it needs no API token. It is
idempotent: assets and sensors are fetched by name before being created, so re-running it
only reprints the summary.

## What it builds

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

## Ties into the rest of the walkthrough

Once a VEN client and polling schedule exist (walkthrough steps 5 and 6), re-run the script:
it points the campus' `site-consumption-capacity` and `site-production-capacity` at the
OpenADR import and export capacity-limit sensors, turning the fetched DR signal into a
scheduling constraint. Before then it reports the wiring as skipped.

## Before triggering a schedule

Each storage device reads its starting state of charge from its own `state of charge`
sensor. Record a recent value there (or pass `soc-at-start` in the scheduling request)
first, otherwise the scheduler stops with
`No recent state-of-charge value found for sensor <id>`.

See the [main walkthrough README](../README.md) for step-by-step instructions.
