"""Integration tests for the polling-schedule pages of the plugin UI."""

from __future__ import annotations

from typing import TYPE_CHECKING

from flexmeasures.data.models.automations import Automation
from sqlalchemy import select

from flexmeasures_openadr3.automations import FETCH_EVENTS_AUTOMATION_TYPE

if TYPE_CHECKING:
    from flask import Flask
    from flask.testing import FlaskClient
    from flask_sqlalchemy import SQLAlchemy
    from flexmeasures.data.models.user import User

    from flexmeasures_openadr3.utils.ven_clients import VenClient


def _logged_in_client(app: Flask, user: User) -> FlaskClient:
    client = app.test_client()
    with client.session_transaction() as session:
        session["_user_id"] = user.fs_uniquifier
        session["_fresh"] = True
    return client


def test_create_edit_and_list_polling_schedule(
    app: Flask,
    fresh_db: SQLAlchemy,
    logged_in_prosumer: User,
    created_ven_client: VenClient,
) -> None:
    """A polling schedule created and edited through the UI is an automation with the submitted cron string and timezone."""
    client = _logged_in_client(app, logged_in_prosumer)
    ven_id = created_ven_client.id

    new_form = client.get(f"/flexmeasures-openadr3/ven-clients/{ven_id}/sensor-configs/new")
    assert new_form.status_code == 200
    # As in FlexMeasures' own automation form, the timezone defaults to the asset's and suggests timezone names.
    assert f'value="{created_ven_client.asset.timezone}"'.encode() in new_form.data
    assert b'<option value="Europe/Amsterdam">' in new_form.data

    response = client.post(
        f"/flexmeasures-openadr3/ven-clients/{ven_id}/sensor-configs",
        data={
            "name": "ui-poll",
            "targets": "target-a, target-b",
            "cronstr": "*/15 * * * *",
            "timezone": "Europe/Amsterdam",
            "fetch_import_capacity_limits": "on",
        },
    )
    assert response.status_code == 302

    automation = fresh_db.session.scalars(select(Automation).filter_by(asset_id=ven_id, type=FETCH_EVENTS_AUTOMATION_TYPE)).one()
    assert automation.name == "ui-poll"
    assert automation.cronstr == "*/15 * * * *"
    assert automation.timezone == "Europe/Amsterdam"

    edit_page = client.get(f"/flexmeasures-openadr3/ven-clients/{ven_id}/sensor-configs/ui-poll")
    assert edit_page.status_code == 200
    assert b"*/15 * * * *" in edit_page.data

    response = client.post(
        f"/flexmeasures-openadr3/ven-clients/{ven_id}/sensor-configs/ui-poll/update",
        data={
            "name": "ui-poll",
            "targets": "target-a",
            "cronstr": "0 6 * * *",
            "timezone": "UTC",
            "fetch_import_capacity_limits": "on",
            "fetch_export_capacity_limits": "on",
        },
    )
    assert response.status_code == 302
    fresh_db.session.refresh(automation)
    assert automation.cronstr == "0 6 * * *"
    assert automation.timezone == "UTC"

    overview = client.get(f"/flexmeasures-openadr3/ven-clients/{ven_id}/sensor-configs")
    assert overview.status_code == 200
    assert b"0 6 * * *" in overview.data
    assert f"/assets/{ven_id}/automations".encode() in overview.data
    assert b"Export sensor" in overview.data

    assert client.get("/flexmeasures-openadr3/dashboard").status_code == 200


def test_polling_schedule_form_rejects_invalid_cron_string(
    app: Flask,
    fresh_db: SQLAlchemy,
    logged_in_prosumer: User,
    created_ven_client: VenClient,
) -> None:
    """An invalid cron string is reported on the form, and creates no automation."""
    client = _logged_in_client(app, logged_in_prosumer)

    response = client.post(
        f"/flexmeasures-openadr3/ven-clients/{created_ven_client.id}/sensor-configs",
        data={
            "name": "bad-poll",
            "cronstr": "every day",
            "timezone": "UTC",
            "fetch_import_capacity_limits": "on",
        },
    )

    assert response.status_code == 200
    assert b"is-invalid" in response.data
    assert fresh_db.session.scalars(select(Automation).filter_by(asset_id=created_ven_client.id)).first() is None
