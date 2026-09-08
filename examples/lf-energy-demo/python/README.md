# Python helpers

Scripts for the [capacity-limits walkthrough](../README.md). `seed_events.py` and
`trigger_fetch.py` are run via [uv](https://docs.astral.sh/uv/) — no manual venv or
`pip install` needed.

| Script | Run where | Run with | Purpose |
| --- | --- | --- | --- |
| `seed_events.py` | Host machine | `uv run seed_events.py` | Create a 24h capacity-limit event on the VTN (BL OAuth client) |
| `compare_schedules.py` | Host machine | `uv run compare_schedules.py` | Trigger a `demo-campus` schedule and compare it with the other side of the OpenADR wiring |
| `trigger_fetch.py` | FlexMeasures container | `uv run --active --no-project trigger_fetch.py` | Run the fetch job immediately for `demo-ven` / `demo-poll` |
| `settings.py` | — | — | Shared URLs and form values |
| `flexmeasures_client.py` | — | — | REST client for the walkthrough's FlexMeasures instance |
| `schedule_comparison.py` | — | — | The saved-run file format and the before/after arithmetic |
| `comparison_diagram.py` | — | — | Renders a comparison as the self-contained `comparison.html` diagram |

`seed_events.py` and `compare_schedules.py` declare their dependencies inline (PEP 723)
and uv resolves them into an isolated, cached environment on first run.
`trigger_fetch.py` instead reuses the FlexMeasures container's own uv-managed venv
(`/app/.venv`), since it needs the `flexmeasures` and `flexmeasures_openadr3` packages
already installed there.

Run `compare_schedules.py` twice: once right after seeding forecasts and before any
OpenADR wiring exists (walkthrough Step 4), and once again after the OpenADR capacity
limits are wired into the `demo-campus` flex-context (Step 10). It works out which run is
which by reading that flex-context, saves each plan to `../schedule-runs/`, and prints a
before-and-after report plus `../schedule-runs/comparison.html` once both exist — a
self-contained page you open in a browser, with an inline SVG diagram drawn by
`comparison_diagram.py`; the page opens showing only the "Before OpenADR" plan, with
checkboxes above the diagram to bring in "With OpenADR" (or hide either one). It reuses the
first run's time window for the second
automatically, so the two plans stay comparable
interval by interval even if the manual UI steps in between (Steps 5-9) took a while;
pass `--fresh-window` to start over instead. If the seeded forecast/price data has aged out
(more than seven days since `seed_forecasts.py` last ran), it refuses to trigger and tells
you to re-seed.

See the [main walkthrough README](../README.md) for step-by-step instructions.
