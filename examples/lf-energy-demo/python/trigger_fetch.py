"""
Run a configured polling schedule now, and wait for the worker to finish fetching.

Does what *Run now* on the VEN client's Automations page does (or
`flexmeasures jobs run-automation --automation <id>`), but looks the polling
schedule up by name. The `worker` service processes the queued job.
Intended to run inside the FlexMeasures server container, reusing its uv-managed
venv (/app/.venv) rather than installing dependencies of its own:

    docker compose exec server uv run --active --no-project /walkthrough/python/trigger_fetch.py
"""

from __future__ import annotations

import argparse
import sys
import time

from flexmeasures.app import create as create_flexmeasures_app
from flexmeasures.data.services.automations import run_automation
from rq.job import Job
from settings import POLLING_SCHEDULE_NAME, VEN_CLIENT_NAME

from flexmeasures_openadr3.utils.ven_clients import VenClientRepository

JOB_WAIT_SECONDS = 120


def main() -> None:
    """Run a configured polling schedule now."""
    parser = argparse.ArgumentParser(description="Run a polling schedule now.")
    parser.add_argument(
        "--ven-name",
        default=VEN_CLIENT_NAME,
        help=f"VEN client name (default: {VEN_CLIENT_NAME})",
    )
    parser.add_argument(
        "--config-name",
        default=POLLING_SCHEDULE_NAME,
        help=f"Polling schedule name (default: {POLLING_SCHEDULE_NAME})",
    )
    args = parser.parse_args()

    app = create_flexmeasures_app()

    with app.app_context():
        repository = VenClientRepository()
        ven_client = repository.find_by_name(args.ven_name)
        if ven_client is None:
            print(f"VEN client '{args.ven_name}' not found.", file=sys.stderr)
            raise SystemExit(1)

        config = ven_client.get_sensor_config(args.config_name)
        if config is None:
            print(
                f"Polling schedule '{args.config_name}' not found on VEN '{args.ven_name}'.",
                file=sys.stderr,
            )
            raise SystemExit(1)

        result = run_automation(config.automation)
        if result is None:
            print(f"Polling schedule '{args.config_name}' queued no job.", file=sys.stderr)
            raise SystemExit(1)
        job = Job.fetch(result["job_id"], connection=app.redis_connection)
        print(f"Queued fetch job {job.id} for VEN '{args.ven_name}' schedule '{args.config_name}' (automation {config.automation.id}).")

        deadline = time.monotonic() + JOB_WAIT_SECONDS
        while not (job.is_finished or job.is_failed) and time.monotonic() < deadline:
            time.sleep(1)
            job.refresh()

        if job.is_finished:
            saved = ", ".join(f"{row['n_rows']} on sensor {row['sensor_id']}" for row in job.return_value()) or "nothing new"
            print(f"Fetch complete: saved {saved}.")
        elif job.is_failed:
            print(f"Fetch failed:\n{job.latest_result().exc_string}", file=sys.stderr)
            raise SystemExit(1)
        else:
            print(f"Job still {job.get_status()} after {JOB_WAIT_SECONDS}s — is the `worker` service up?", file=sys.stderr)
            raise SystemExit(1)


if __name__ == "__main__":
    main()
