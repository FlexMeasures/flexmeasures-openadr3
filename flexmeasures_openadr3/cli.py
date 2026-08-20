from __future__ import annotations

import threading

import click
from flask import Flask, current_app
from flask.cli import with_appcontext
from rq.cron import CronScheduler
from sqlalchemy.exc import OperationalError, ProgrammingError

from flexmeasures_openadr3.utils.ven_clients import VenClientRepository
from flexmeasures_openadr3.utils.ven_jobs import VEN_CRON_RESYNC_CHANNEL, VenFetchJobScheduler

_FALLBACK_RESYNC_INTERVAL_SECONDS = 5 * 60


def _resync_or_warn(job_scheduler: VenFetchJobScheduler) -> None:
    """Rebuild the cron job list, tolerating a not-yet-migrated database."""
    try:
        job_scheduler.resync_all()
    except (OperationalError, ProgrammingError):
        current_app.logger.warning("flexmeasures-openadr3: could not load VEN clients (database tables may not exist yet — run migrations first). Skipping this resync.")


def _listen_for_resync(app: Flask, job_scheduler: VenFetchJobScheduler) -> None:
    """
    Rebuild the cron job list on every resync notification.

    Also resyncs on a periodic fallback interval, so a missed pub/sub message
    (e.g. during a scheduler restart) self-heals instead of silently drifting.
    """
    with app.app_context():
        pubsub = app.redis_connection.pubsub()
        pubsub.subscribe(VEN_CRON_RESYNC_CHANNEL)
        while True:
            message = pubsub.get_message(timeout=_FALLBACK_RESYNC_INTERVAL_SECONDS)
            if message is not None and message["type"] != "message":
                continue
            _resync_or_warn(job_scheduler)


@click.command("oadr3-run-cron-scheduler")
@with_appcontext
def oadr3_run_cron_scheduler() -> None:
    """
    Run the dedicated OpenADR 3 VEN fetch-events cron scheduler process.

    Exactly one instance of this command may run at a time: rq.cron's
    CronScheduler keeps its job list in-process with no cross-process
    coordination, so VEN client/sensor config changes made in the web app are
    applied here by rebuilding the job list from the database, triggered via
    Redis pub/sub notifications.
    """
    app = current_app._get_current_object()  # noqa: SLF001
    cron_scheduler = CronScheduler(connection=app.redis_connection)
    repository = VenClientRepository()
    job_scheduler = VenFetchJobScheduler(repository, cron_scheduler=cron_scheduler)

    _resync_or_warn(job_scheduler)

    threading.Thread(
        target=_listen_for_resync,
        args=(app, job_scheduler),
        daemon=True,
        name="oadr3-cron-resync-listener",
    ).start()

    cron_scheduler.start()
