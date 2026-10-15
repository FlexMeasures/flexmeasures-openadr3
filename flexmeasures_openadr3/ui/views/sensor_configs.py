from typing import cast

from flask import abort, flash, redirect, request, url_for
from flask_login import login_required
from flexmeasures.data import db
from flexmeasures.data.services.automations import describe_cronstr
from flexmeasures.ui.utils.view_utils import render_flexmeasures_template
from werkzeug.wrappers import Response

from flexmeasures_openadr3 import flexmeasures_openadr3_ui_bp
from flexmeasures_openadr3.models.forms import FormValidationErrors, VenSensorConfigFormValues, VenSensorConfigPostValues
from flexmeasures_openadr3.models.views import VenSensorConfigOverview
from flexmeasures_openadr3.utils.ven_client_forms import build_ven_sensor_config_form_values, validate_ven_sensor_config_form
from flexmeasures_openadr3.utils.ven_clients import VenClientRepository


def _require_validated_data[T](data: T | None) -> T:
    """Return validated form data or raise if it is unexpectedly missing."""
    if data is None:
        msg = "Validated form data missing despite successful validation."
        raise RuntimeError(msg)
    return data


def _build_repository() -> VenClientRepository:
    """Create a repository only when a request/app context is active."""
    return VenClientRepository()


def _sensor_config_post_values_from_request() -> VenSensorConfigPostValues:
    return VenSensorConfigPostValues(
        name=request.form.get("name", "").strip(),
        targets=request.form.get("targets", "").strip(),
        cronstr=request.form.get("cronstr", "").strip(),
        timezone=request.form.get("timezone", "").strip(),
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
            automation_id=cfg.automation.id,
            cronstr=cfg.cronstr,
            schedule_description=describe_cronstr(cfg.cronstr),
            timezone=cfg.timezone,
            active=cfg.active,
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
