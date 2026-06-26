import contextlib
from typing import cast

from flask import abort, flash, redirect, request, url_for
from flask_login import login_required
from flexmeasures.data import db
from flexmeasures.ui.utils.view_utils import render_flexmeasures_template
from werkzeug.wrappers import Response

from flexmeasures_openadr3 import flexmeasures_openadr3_ui_bp
from flexmeasures_openadr3.models.forms import FormValidationErrors, VenSensorConfigFormValues, VenSensorConfigPostValues
from flexmeasures_openadr3.models.views import VenSensorConfigOverview
from flexmeasures_openadr3.utils.encryption import SecretsDecryptionError, SecretsEncryptor
from flexmeasures_openadr3.utils.events import ACTIVE_OPENADR_EVENTS, SUPPORTED_SIGNAL_NAMES
from flexmeasures_openadr3.utils.ven_clients import (
    VEN_CLIENT_FORM_FIELDS,
    VenClient,
    VenClientRepository,
    build_ven_client_form_values,
    build_ven_sensor_config_form_values,
    validate_ven_client_form,
    validate_ven_sensor_config_form,
)
from flexmeasures_openadr3.utils.ven_jobs import VenFetchJobScheduler

_HOUR_MINUTE_COMPONENT_COUNT = 2
_MAX_HOUR = 23
_MAX_MINUTE = 59


def _require_validated_data[T](data: T | None) -> T:
    """Return validated form data or raise if it is unexpectedly missing."""
    if data is None:
        msg = "Validated form data missing despite successful validation."
        raise RuntimeError(msg)
    return data


def _build_repository() -> VenClientRepository:
    """Create a repository only when a request/app context is active."""
    return VenClientRepository()


def _build_job_scheduler(repository: VenClientRepository) -> VenFetchJobScheduler:
    """Create a scheduler bound to the current request repository."""
    return VenFetchJobScheduler(repository)


def _utc_trigger_time_from_request(raw: str) -> str:
    """
    Normalize HTML time input values to HH:MM:SS for validation.

    Browsers may submit HH:MM when seconds are zero; the form validator
    expects three colon-separated parts.
    """
    stripped = raw.strip()
    if not stripped:
        return ""
    parts = stripped.split(":")
    if len(parts) == _HOUR_MINUTE_COMPONENT_COUNT and all(p.isdigit() for p in parts):
        hour, minute = int(parts[0]), int(parts[1])
        if 0 <= hour <= _MAX_HOUR and 0 <= minute <= _MAX_MINUTE:
            return f"{hour:02d}:{minute:02d}:00"
    return stripped


def _sensor_config_post_values_from_request() -> VenSensorConfigPostValues:
    return VenSensorConfigPostValues(
        name=request.form.get("name", "").strip(),
        targets=request.form.get("targets", "").strip(),
        utc_trigger_time=_utc_trigger_time_from_request(request.form.get("utc_trigger_time", "")),
        fetch_import_capacity_limits=request.form.get("fetch_import_capacity_limits") == "on",
        fetch_export_capacity_limits=request.form.get("fetch_export_capacity_limits") == "on",
    )


def _decrypt_ven_client_credentials_for_form(ven_client: VenClient) -> VenClient:
    """
    Decrypt VEN credentials for UI rendering.

    If credentials are already plaintext (legacy records), keep them unchanged.
    """
    secrets_encryptor = SecretsEncryptor.from_current_app()
    with contextlib.suppress(SecretsDecryptionError):
        ven_client.oauth_client_id = secrets_encryptor.decrypt(ven_client.oauth_client_id)
    with contextlib.suppress(SecretsDecryptionError):
        ven_client.oauth_client_secret = secrets_encryptor.decrypt(ven_client.oauth_client_secret)
    return ven_client


@flexmeasures_openadr3_ui_bp.route("/")
@flexmeasures_openadr3_ui_bp.route("/dashboard")
@login_required
def dashboard() -> str:
    """Render the OpenADR 3 dashboard."""
    ven_client_repository = _build_repository()
    active_events = tuple(event for event in ACTIVE_OPENADR_EVENTS if all(pd.payload_type in SUPPORTED_SIGNAL_NAMES for pd in event.payload_descriptors or ()))

    return cast(
        "str",
        render_flexmeasures_template(
            "flexmeasures_oadr3_dashboard.html",
            ven_clients=ven_client_repository.list_ven_clients(),
            active_events=active_events,
        ),
    )


@flexmeasures_openadr3_ui_bp.route("/ven-clients/<int:ven_id>/sensor-configs")
@login_required
def sensor_configs_overview(ven_id: int) -> str:
    """Render an overview of sensor configurations for one VEN client."""
    ven_client_repository = _build_repository()
    ven_client = ven_client_repository.find_by_id(ven_id)
    if ven_client is None:
        abort(404)

    sensor_configs = [
        VenSensorConfigOverview(
            ven_id=ven_client.id,
            ven_name=ven_client.name,
            config_name=cfg.name,
            targets=tuple(cfg.targets),
            utc_trigger_time=cfg.utc_trigger_time.isoformat() if cfg.utc_trigger_time else "",
            fetch_import_capacity_limits=cfg.fetch_import_capacity_limits,
            fetch_export_capacity_limits=cfg.fetch_export_capacity_limits,
            import_sensor_id=cfg.import_sensor.id if cfg.import_sensor else None,
            export_sensor_id=cfg.export_sensor.id if cfg.export_sensor else None,
        )
        for cfg in ven_client.sensor_configs
    ]

    return cast(
        "str",
        render_flexmeasures_template(
            "flexmeasures_oadr3_sensor_configs_overview.html",
            ven_client=ven_client,
            sensor_configs=sensor_configs,
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
    ven_client = _decrypt_ven_client_credentials_for_form(ven_client)

    return cast(
        "str",
        render_flexmeasures_template(
            "flexmeasures_oadr3_ven_client_detail.html",
            ven_client=ven_client,
            form_values=build_ven_client_form_values(ven_client),
            form_errors=FormValidationErrors(),
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
    job_scheduler = _build_job_scheduler(ven_client_repository)
    ven_client = ven_client_repository.find_by_id(ven_id)
    if ven_client is None:
        abort(404)

    job_scheduler.delete_all(ven_client)
    ven_client_repository.delete(ven_client)
    db.session.commit()
    flash(f"VEN client '{ven_client.name}' deleted.")
    return redirect(url_for(".dashboard"))


@flexmeasures_openadr3_ui_bp.route("/ven-clients/<int:ven_id>/sensor-configs/new", methods=["GET"])
@login_required
def ven_client_sensor_config_new(ven_id: int) -> str:
    """Render the 'new polling schedule' form for a VEN client."""
    ven_client_repository = _build_repository()
    ven_client = ven_client_repository.find_by_id(ven_id)
    if ven_client is None:
        abort(404)

    return cast(
        "str",
        render_flexmeasures_template(
            "flexmeasures_oadr3_ven_sensor_config_form.html",
            ven_client=ven_client,
            form_values=build_ven_sensor_config_form_values(ven_client),
            form_errors=FormValidationErrors(),
            form_action=url_for(".ven_client_sensor_config_create", ven_id=ven_client.id),
            title="Add polling schedule",
        ),
    )


@flexmeasures_openadr3_ui_bp.route("/ven-clients/<int:ven_id>/sensor-configs", methods=["POST"])
@login_required
def ven_client_sensor_config_create(ven_id: int) -> str | Response:
    """Handle form submission for creating a polling schedule."""
    ven_client_repository = _build_repository()
    job_scheduler = _build_job_scheduler(ven_client_repository)
    ven_client = ven_client_repository.find_by_id(ven_id)
    if ven_client is None:
        abort(404)

    post_values = _sensor_config_post_values_from_request()
    validation = validate_ven_sensor_config_form(post_values, existing_configs=ven_client.sensor_configs)
    if validation.is_valid:
        sensor_config = ven_client_repository.append_sensor_config(
            ven_client,
            _require_validated_data(validation.data),
        )
        job_scheduler.schedule(ven_client, sensor_config)
        db.session.commit()
        flash(f"Polling schedule added for VEN client '{ven_client.name}'.")
        return redirect(url_for(".dashboard"))

    return cast(
        "str",
        render_flexmeasures_template(
            "flexmeasures_oadr3_ven_sensor_config_form.html",
            ven_client=ven_client,
            form_values=VenSensorConfigFormValues.from_post_values(post_values),
            form_errors=validation.errors,
            form_action=url_for(".ven_client_sensor_config_create", ven_id=ven_client.id),
            title="Add polling schedule",
        ),
    )


@flexmeasures_openadr3_ui_bp.route(
    "/ven-clients/<int:ven_id>/sensor-configs/<config_name>/delete",
    methods=["POST"],
)
@login_required
def ven_client_sensor_config_delete(ven_id: int, config_name: str) -> Response:
    """Delete a polling schedule from a VEN client."""
    ven_client_repository = _build_repository()
    job_scheduler = _build_job_scheduler(ven_client_repository)
    ven_client = ven_client_repository.find_by_id(ven_id)
    if ven_client is None:
        abort(404)

    sensor_config = ven_client.get_sensor_config(config_name)
    if sensor_config is None:
        abort(404)

    job_scheduler.delete(sensor_config)
    ven_client_repository.delete_sensor_config(ven_client, config_name)
    db.session.commit()
    flash(f"Polling schedule '{config_name}' deleted.")
    return redirect(url_for(".sensor_configs_overview", ven_id=ven_id))


@flexmeasures_openadr3_ui_bp.route(
    "/ven-clients/<int:ven_id>/sensor-configs/<config_name>",
    methods=["GET"],
)
@login_required
def ven_client_sensor_config_edit(ven_id: int, config_name: str) -> str:
    """Render the 'edit polling schedule' form for a VEN client."""
    ven_client_repository = _build_repository()
    ven_client = ven_client_repository.find_by_id(ven_id)
    if ven_client is None:
        abort(404)

    if ven_client.get_sensor_config(config_name) is None:
        abort(404)

    return cast(
        "str",
        render_flexmeasures_template(
            "flexmeasures_oadr3_ven_sensor_config_form.html",
            ven_client=ven_client,
            form_values=build_ven_sensor_config_form_values(ven_client, config_name),
            form_errors=FormValidationErrors(),
            form_action=url_for(
                ".ven_client_sensor_config_update",
                ven_id=ven_client.id,
                config_name=config_name,
            ),
            delete_action=url_for(
                ".ven_client_sensor_config_delete",
                ven_id=ven_client.id,
                config_name=config_name,
            ),
            title="Edit polling schedule",
        ),
    )


@flexmeasures_openadr3_ui_bp.route(
    "/ven-clients/<int:ven_id>/sensor-configs/<config_name>/update",
    methods=["POST"],
)
@login_required
def ven_client_sensor_config_update(ven_id: int, config_name: str) -> str | Response:
    """Handle form submission for updating a polling schedule."""
    ven_client_repository = _build_repository()
    job_scheduler = _build_job_scheduler(ven_client_repository)
    ven_client = ven_client_repository.find_by_id(ven_id)
    if ven_client is None:
        abort(404)

    if ven_client.get_sensor_config(config_name) is None:
        abort(404)

    post_values = _sensor_config_post_values_from_request()
    validation = validate_ven_sensor_config_form(
        post_values,
        existing_configs=ven_client.sensor_configs,
        current_name=config_name,
    )
    if validation.is_valid:
        existing_config = ven_client.get_sensor_config(config_name)
        if existing_config is None:
            abort(404)
        job_scheduler.delete(existing_config)
        updated_config = ven_client_repository.update_sensor_config(
            ven_client,
            config_name,
            _require_validated_data(validation.data),
        )
        job_scheduler.schedule(ven_client, updated_config)
        db.session.commit()
        flash(f"Polling schedule updated for VEN client '{ven_client.name}'.")
        return redirect(url_for(".dashboard"))

    return cast(
        "str",
        render_flexmeasures_template(
            "flexmeasures_oadr3_ven_sensor_config_form.html",
            ven_client=ven_client,
            form_values=VenSensorConfigFormValues.from_post_values(post_values),
            form_errors=validation.errors,
            form_action=url_for(
                ".ven_client_sensor_config_update",
                ven_id=ven_client.id,
                config_name=config_name,
            ),
            delete_action=url_for(
                ".ven_client_sensor_config_delete",
                ven_id=ven_client.id,
                config_name=config_name,
            ),
            title="Edit polling schedule",
        ),
    )
