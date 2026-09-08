"""
Run the OpenADR fetch job immediately for a configured polling schedule.

Mirrors the helper used in tests/test_ven_fetch_e2e.py (_run_fetch_job_immediately).
Intended to run inside the FlexMeasures server container, reusing its uv-managed
venv (/app/.venv) rather than installing dependencies of its own:

    docker compose exec server uv run --active --no-project /walkthrough/python/trigger_fetch.py
"""

from __future__ import annotations

import argparse
import sys

from flexmeasures.app import create as create_flexmeasures_app
from settings import POLLING_SCHEDULE_NAME, VEN_CLIENT_NAME

from flexmeasures_openadr3.utils.ven_clients import VenClientRepository
from flexmeasures_openadr3.utils.ven_jobs import _execute_fetch_events


def main() -> None:
    """Trigger an immediate OpenADR event fetch for a configured polling schedule."""
    parser = argparse.ArgumentParser(description="Trigger an immediate OpenADR event fetch for a polling schedule.")
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

        _execute_fetch_events(ven_client.id, config.name)
        print(f"Fetch complete for VEN '{args.ven_name}' schedule '{args.config_name}'.")


if __name__ == "__main__":
    main()
