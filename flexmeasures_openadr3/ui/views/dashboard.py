from typing import cast

from flask import abort, current_app, flash, redirect, request, url_for
from flask_login import login_required
from flexmeasures.data import db
from flexmeasures.ui.utils.view_utils import render_flexmeasures_template
from werkzeug.wrappers import Response

from flexmeasures_openadr3 import flexmeasures_openadr3_ui_bp
from flexmeasures_openadr3.models.forms import FormValidationErrors
from flexmeasures_openadr3.utils.ven_client_forms import (
    VEN_CLIENT_FORM_FIELDS,
    build_ven_client_form_values,
    validate_ven_client_form,
)
from flexmeasures_openadr3.utils.ven_clients import VenClientRepository
from flexmeasures_openadr3.utils.ven_jobs import notify_cron_resync


def _require_validated_data[T](data: T | None) -> T:
    """Return validated form data or raise if it is unexpectedly missing."""
    if data is None:
        msg = "Validated form data missing despite successful validation."
        raise RuntimeError(msg)
    return data


def _build_repository() -> VenClientRepository:
    """Create a repository only when a request/app context is active."""
    return VenClientRepository()


@flexmeasures_openadr3_ui_bp.route("/")
@flexmeasures_openadr3_ui_bp.route("/dashboard")
@login_required
def dashboard() -> str:
    """Render the OpenADR 3 dashboard."""
    ven_client_repository = _build_repository()

    return cast(
        "str",
        render_flexmeasures_template(
            "flexmeasures_oadr3_dashboard.html",
            ven_clients=ven_client_repository.list_ven_clients(),
        ),
    )


@flexmeasures_openadr3_ui_bp.route("/ven-clients/new", methods=["GET", "POST"])
@login_required
def ven_client_new() -> str | Response:
    """Create a new VEN client (shared fields only)."""
    ven_client_repository = _build_repository()
    field_values = build_ven_client_form_values()
    errors = FormValidationErrors()

    if request.method == "POST":
        field_values = field_values.with_request_fields(VEN_CLIENT_FORM_FIELDS, request.form)
        validation = validate_ven_client_form(
            field_values=field_values,
            ven_client_repository=ven_client_repository,
        )
        errors = validation.errors

        if validation.is_valid:
            ven_client = ven_client_repository.create(_require_validated_data(validation.data))
            db.session.commit()
            flash(f"VEN client '{ven_client.name}' created.")
            return redirect(url_for(".dashboard"))

    return cast(
        "str",
        render_flexmeasures_template(
            "flexmeasures_oadr3_ven_client_new.html",
            form_values=field_values,
            form_errors=errors,
        ),
    )


@flexmeasures_openadr3_ui_bp.route("/ven-clients/<int:ven_id>")
@login_required
def ven_client_detail(ven_id: int) -> str:
    """Render the detail page for a VEN client."""
    ven_client_repository = _build_repository()
    ven_client = ven_client_repository.find_by_id(ven_id)
    if ven_client is None:
        abort(404)

    return cast(
        "str",
        render_flexmeasures_template(
            "flexmeasures_oadr3_ven_client_detail.html",
            ven_client=ven_client,
            form_values=build_ven_client_form_values(ven_client),
            form_errors=FormValidationErrors(),
            oauth_client_id_is_set=ven_client.oauth_client_id_is_set,
            oauth_client_secret_is_set=ven_client.oauth_client_secret_is_set,
        ),
    )


@flexmeasures_openadr3_ui_bp.route("/ven-clients/<int:ven_id>/update", methods=["POST"])
@login_required
def ven_client_update(ven_id: int) -> str | Response:
    """Update an existing VEN client (shared fields only)."""
    ven_client_repository = _build_repository()
    ven_client = ven_client_repository.find_by_id(ven_id)
    if ven_client is None:
        abort(404)

    field_values = build_ven_client_form_values(ven_client).with_request_fields(VEN_CLIENT_FORM_FIELDS, request.form)
    validation = validate_ven_client_form(
        field_values,
        ven_client_repository=ven_client_repository,
        current_name=ven_client.name,
    )
    if not validation.is_valid:
        return cast(
            "str",
            render_flexmeasures_template(
                "flexmeasures_oadr3_ven_client_detail.html",
                ven_client=ven_client,
                form_values=field_values,
                form_errors=validation.errors,
                oauth_client_id_is_set=ven_client.oauth_client_id_is_set,
                oauth_client_secret_is_set=ven_client.oauth_client_secret_is_set,
            ),
        )

    ven_client = ven_client_repository.update(ven_client, _require_validated_data(validation.data))
    db.session.commit()
    flash(f"VEN client '{ven_client.name}' updated.")
    return redirect(url_for(".ven_client_detail", ven_id=ven_client.id))


@flexmeasures_openadr3_ui_bp.route("/ven-clients/<int:ven_id>/delete", methods=["POST"])
@login_required
def ven_client_delete(ven_id: int) -> Response:
    """Delete a VEN client and all its scheduled fetch jobs."""
    ven_client_repository = _build_repository()
    ven_client = ven_client_repository.find_by_id(ven_id)
    if ven_client is None:
        abort(404)

    ven_client_repository.delete(ven_client)
    db.session.commit()
    notify_cron_resync(current_app.redis_connection)
    flash(f"VEN client '{ven_client.name}' deleted.")
    return redirect(url_for(".dashboard"))
