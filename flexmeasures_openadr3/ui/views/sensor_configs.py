from typing import cast

from flask import abort, current_app, flash, redirect, request, url_for
from flask_login import login_required
from flexmeasures.data import db
from flexmeasures.ui.utils.view_utils import render_flexmeasures_template
from werkzeug.wrappers import Response

from flexmeasures_openadr3 import flexmeasures_openadr3_ui_bp
from flexmeasures_openadr3.models.forms import FormValidationErrors, VenSensorConfigFormValues, VenSensorConfigPostValues
from flexmeasures_openadr3.models.views import VenSensorConfigOverview
from flexmeasures_openadr3.utils.ven_client_forms import build_ven_sensor_config_form_values, validate_ven_sensor_config_form
from flexmeasures_openadr3.utils.ven_clients import VenClientRepository
from flexmeasures_openadr3.utils.ven_jobs import notify_cron_resync

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
    ven_client = ven_client_repository.find_by_id(ven_id)
    if ven_client is None:
        abort(404)

    post_values = _sensor_config_post_values_from_request()
    validation = validate_ven_sensor_config_form(post_values, existing_configs=ven_client.sensor_configs)
    if validation.is_valid:
        ven_client_repository.append_sensor_config(
            ven_client,
            _require_validated_data(validation.data),
        )
        db.session.commit()
        notify_cron_resync(current_app.redis_connection)
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
    ven_client = ven_client_repository.find_by_id(ven_id)
    if ven_client is None:
        abort(404)

    sensor_config = ven_client.get_sensor_config(config_name)
    if sensor_config is None:
        abort(404)

    ven_client_repository.delete_sensor_config(ven_client, config_name)
    db.session.commit()
    notify_cron_resync(current_app.redis_connection)
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
        ven_client_repository.update_sensor_config(
            ven_client,
            config_name,
            _require_validated_data(validation.data),
        )
        db.session.commit()
        notify_cron_resync(current_app.redis_connection)
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
