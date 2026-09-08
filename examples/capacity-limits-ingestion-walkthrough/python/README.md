# Python helpers

Scripts for the [capacity-limits walkthrough](../README.md). `seed_events.py` and
`trigger_fetch.py` are run via [uv](https://docs.astral.sh/uv/) — no manual venv or
`pip install` needed.

| Script | Run where | Run with | Purpose |
| --- | --- | --- | --- |
| `seed_events.py` | Host machine | `uv run seed_events.py` | Create a 24h capacity-limit event on the VTN (BL OAuth client) |
| `trigger_fetch.py` | FlexMeasures container | `uv run --active --no-project trigger_fetch.py` | Run the fetch job immediately for `demo-ven` / `demo-poll` |
| `settings.py` | — | — | Shared URLs and form values |

`seed_events.py` declares its dependencies inline (PEP 723) and uv resolves them into
an isolated, cached environment on first run. `trigger_fetch.py` instead reuses the
FlexMeasures container's own uv-managed venv (`/app/.venv`), since it needs the
`flexmeasures` and `flexmeasures_openadr3` packages already installed there.

See the [main walkthrough README](../README.md) for step-by-step instructions.
