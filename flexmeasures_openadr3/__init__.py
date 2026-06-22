from __future__ import annotations

import warnings
from importlib_metadata import version as pkg_version
from packaging.version import Version

from dataclasses import dataclass
from typing import TypedDict

from flask import Blueprint
from flask.blueprints import BlueprintSetupState

from .utils.blueprints import ensure_bp_routes_are_loaded_fresh

__version__ = "0.1"


"""
The __init__ for the flexmeasures-oadr3 FlexMeasures plugin.

FlexMeasures registers the BluePrint objects it finds in here.
"""

try:
    _fm_version = pkg_version("flexmeasures")
    if Version(_fm_version) < Version("v0.32.0"):
        warnings.warn(
            f"flexmeasures-openadr3 requires FlexMeasures >= v0.32.0, "
            f"but version {_fm_version} is installed."
        )
except Exception:
    pass


class PluginSettingSpec(TypedDict):
    description: str
    level: str
    required: bool
    default: str


__settings__: dict[str, PluginSettingSpec] = {
    "OPENADR_SECRETS_ENCRYPTION_KEY": {
        "description": "Encryption key for OpenADR secrets in the attributes.",
        "level": "debug",
        "required": False,
        "default": "",
    },
    "ALLOW_INSECURE_HTTP_VTN": {
        "description": "Allow insecure HTTP connections to VTNs.",
        "level": "debug",
        "required": False,
        "default": "false",
    },
}

flexmeasures_openadr3_ui_bp: Blueprint = Blueprint(
    "flexmeasures-openadr3 UI",
    __name__,
    template_folder="ui/templates",
    static_folder="ui/static",
    url_prefix="/flexmeasures-openadr3",
)
ensure_bp_routes_are_loaded_fresh("ui.views.dashboards")
from flexmeasures_openadr3.ui.views import dashboard  # noqa: E402,F401


@dataclass(frozen=True, slots=True)
class MenuRegistration:
    view_key: str
    title: str
    icon: str


OPENADR_MENU_REGISTRATION = MenuRegistration(
    view_key="flexmeasures-openadr3/dashboard",
    title="OpenADR 3 Configuration",
    icon="bolt",
)


@flexmeasures_openadr3_ui_bp.record_once
def register_menu_item(setup_state: BlueprintSetupState) -> None:
    app = setup_state.app
    registration = OPENADR_MENU_REGISTRATION
    app.config["FLEXMEASURES_MENU_LISTED_VIEWS"] = app.config.get(
        "FLEXMEASURES_MENU_LISTED_VIEWS", []
    ) + [registration.view_key]
    app.config["FLEXMEASURES_MENU_LISTED_VIEW_TITLES"] = {
        **app.config.get("FLEXMEASURES_MENU_LISTED_VIEW_TITLES", {}),
        registration.view_key: registration.title,
    }
    app.config["FLEXMEASURES_MENU_LISTED_VIEW_ICONS"] = {
        **app.config.get("FLEXMEASURES_MENU_LISTED_VIEW_ICONS", {}),
        registration.view_key: registration.icon,
    }
