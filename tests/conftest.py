"""Fixtures for VEN client and polling-schedule integration tests."""

from __future__ import annotations

import os
from datetime import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from flask import Flask
from flask_login import login_user
from flask_sqlalchemy import SQLAlchemy
from flexmeasures.data.models.user import User
from openadr3_client.oadr310.models.event.event import ExistingEvent
from rq.cron import CronScheduler
from testcontainers.core.network import Network
from testcontainers.keycloak import KeycloakContainer

from flexmeasures_openadr3.utils.ven_clients import (
    VenClient,
    VenClientFormData,
    VenClientRepository,
    VenSensorConfigFormData,
)
from flexmeasures_openadr3.utils.ven_jobs import VenFetchJobScheduler
from tests.test_container.integration_types import (
    IntegrationTestVTNClient,
    IntegrationTestVTNServer,
    OAuthConfiguration,
)
from tests.test_container.oadr310_vtn_test_container import (
    OpenAdr310VtnTestContainer,
)
from tests.test_container.openadr_vtn_setup import (
    create_bl_http_client,
    seed_capacity_limit_event,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

_TESTS_ROOT = Path(__file__).resolve().parents[0]
_KEYCLOAK_REALM_IMPORT_FILE = _TESTS_ROOT / "keycloak_integration_realm.json"


KEYCLOAK_REALM_NAME = "integration-test-realm"

# Keycloak OAuth test clients (from `tests/keycloak_integration_realm.json`)
KEYCLOAK_BL_CLIENT_ID = "test-client-id"
KEYCLOAK_BL_CLIENT_SECRET = "my-client-secret"
KEYCLOAK_VEN1_CLIENT_ID = "test-ven-1"
KEYCLOAK_VEN2_CLIENT_ID = "test-ven-2"
KEYCLOAK_VEN_CLIENT_SECRET = "my-client-secret"

KEYCLOAK_INTERNAL_BASE_URL = f"http://keycloak:8080/realms/{KEYCLOAK_REALM_NAME}/protocol/openid-connect"
KEYCLOAK_INTERNAL_TOKEN_URL = f"{KEYCLOAK_INTERNAL_BASE_URL}/token"
KEYCLOAK_INTERNAL_JWKS_URL = f"{KEYCLOAK_INTERNAL_BASE_URL}/certs"

# Scopes expected by OpenLEADR-rs (used when testing against OpenLEADR-rs VTNs).
# These match the scope strings parsed by OpenLEADR-rs in `openleadr-vtn/src/jwt.rs`.
OPENLEADR_RS_BL_SCOPES = [
    "read_all",
    "write_programs",
    "write_events",
    "write_reports",
    "write_subscriptions",
    "write_vens",
    "write_users",
]
OPENLEADR_RS_VEN_SCOPES = [
    "read_targets",
    "read_ven_objects",
    "write_reports",
    "write_subscriptions",
    "write_vens",
]


@pytest.fixture
def clean_forecasting_queue(app: Flask) -> Iterator[None]:
    """Reset the test Redis DB without queue.empty() (fakeredis lacks EVALSHA)."""
    app.redis_connection.flushdb()
    yield
    app.redis_connection.flushdb()


@pytest.fixture
def ven_client_repository() -> VenClientRepository:
    """Repository bound to the current app and database session."""
    return VenClientRepository()


@pytest.fixture
def ven_fetch_job_scheduler(
    app: Flask,
    ven_client_repository: VenClientRepository,
) -> VenFetchJobScheduler:
    """RQ job scheduler backed by a fresh CronScheduler, as the dedicated cron-scheduler process builds one."""
    return VenFetchJobScheduler(ven_client_repository, cron_scheduler=CronScheduler(connection=app.redis_connection))


@pytest.fixture
def sample_ven_client_form_data() -> VenClientFormData:
    """Valid VEN client connection fields for create/update tests."""
    return VenClientFormData(
        name="test-ven-client",
        vtn_url="https://vtn.example/openadr3",
        oauth_client_id="test-client-id",
        oauth_client_secret="test-client-secret",
        oauth_token_url="https://vtn.example/oauth/token",
        scopes=["read", "write"],
    )


@pytest.fixture
def sample_sensor_config_form_data() -> VenSensorConfigFormData:
    """Valid polling schedule fields for create/update tests."""
    return VenSensorConfigFormData(
        name="daily-poll",
        targets=["target-a", "target-b"],
        utc_trigger_time=time.fromisoformat("02:30:00"),
        fetch_import_capacity_limits=True,
        fetch_export_capacity_limits=True,
    )


@pytest.fixture
def logged_in_prosumer(
    app: Flask,
    setup_roles_users_fresh_db: dict[str, int],
    db: SQLAlchemy,
) -> Iterator[User]:
    """Prosumer user with an active Flask-Login session for repository calls."""
    user = db.session.get(User, setup_roles_users_fresh_db["Test Prosumer User"])
    assert user is not None

    with app.test_request_context():
        login_user(user)
        yield user


@pytest.fixture
def created_ven_client(
    fresh_db: object,  # noqa: ARG001
    db: SQLAlchemy,
    logged_in_prosumer: User,  # noqa: ARG001
    ven_client_repository: VenClientRepository,
    sample_ven_client_form_data: VenClientFormData,
) -> VenClient:
    """VEN client persisted in the database."""
    ven_client = ven_client_repository.create(sample_ven_client_form_data)
    db.session.commit()
    return ven_client


@pytest.fixture
def created_ven_client_with_schedule(
    db: SQLAlchemy,
    created_ven_client: VenClient,
    ven_client_repository: VenClientRepository,
    sample_sensor_config_form_data: VenSensorConfigFormData,
) -> VenClient:
    """VEN client with one polling schedule and linked sensors."""
    sensor_config = ven_client_repository.append_sensor_config(
        created_ven_client,
        sample_sensor_config_form_data,
    )
    db.session.commit()
    assert sensor_config is not None
    return created_ven_client


@pytest.fixture(scope="session")
def integration_test_docker_network() -> Iterator[Network]:
    """Shared Docker network for Keycloak and OpenLEADR-rs VTN testcontainers."""
    network = Network()
    network.create()
    yield network
    network.remove()


@pytest.fixture(scope="session", autouse=True)
def _allow_http_oauth_for_test_keycloak() -> Iterator[None]:
    """Keycloak testcontainer serves HTTP; oauthlib requires this in non-production tests."""
    previous_insecure = os.environ.get("OAUTHLIB_INSECURE_TRANSPORT")
    previous_scope = os.environ.get("OAUTHLIB_RELAX_TOKEN_SCOPE")
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
    os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
    yield
    if previous_insecure is None:
        os.environ.pop("OAUTHLIB_INSECURE_TRANSPORT", None)
    else:
        os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = previous_insecure
    if previous_scope is None:
        os.environ.pop("OAUTHLIB_RELAX_TOKEN_SCOPE", None)
    else:
        os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = previous_scope


@pytest.fixture(scope="session")
def integration_test_auth_server(
    integration_test_docker_network: Network,
) -> Iterator[KeycloakContainer]:
    """Keycloak with the integration-test realm (OAuth for BL and VEN clients)."""
    keycloak = (
        KeycloakContainer("quay.io/keycloak/keycloak:25.0.4")
        .with_network(integration_test_docker_network)
        .with_network_aliases("keycloak")
        .with_realm_import_file(str(_KEYCLOAK_REALM_IMPORT_FILE))
    )
    with keycloak:
        yield keycloak


@pytest.fixture(scope="session")
def integration_test_oauth_client_bl_client(
    integration_test_auth_server: KeycloakContainer,
) -> OAuthConfiguration:
    """
    A testcontainers keycloak fixture which is initialized once per test run.

    Yields an OAuthConfiguration which contains an oauth client that was created
    for the scope of this test session. This OAUTH client is configured to have the BL scopes inside an OpenADR VTN.

    Yields:
        IntegrationTestOAuthClient: The integration test oauth client.

    """
    token_url = integration_test_auth_server.get_url() + f"/realms/{KEYCLOAK_REALM_NAME}/protocol/openid-connect/token"
    return OAuthConfiguration(
        client_id=KEYCLOAK_BL_CLIENT_ID,
        client_secret=KEYCLOAK_BL_CLIENT_SECRET,
        token_url=token_url,
        scopes=None,
        audience=None,
    )


@pytest.fixture(scope="session")
def integration_test_openadr310_vtn_server(
    integration_test_docker_network: Network,
) -> Iterable[IntegrationTestVTNServer]:
    """
    A testcontainers openleadr-vtn (OpenADR 3.1.0) fixture which is initialized once per test run.

    Yields an OpenAdr310VtnTestContainer which contains the base URL of the VTN being hosted.

    Args:
        integration_test_docker_network (Network): The docker network to which the keycloak container will be connected.
        integration_test_oauth_client (OAuthConfiguration): OAuth client for retrieving access tokens (Keycloak).

    Yields:
        Iterable[OpenAdr310VtnTestContainer]: The integration test vtn client.

    """
    with OpenAdr310VtnTestContainer(
        oauth_jwks_url=KEYCLOAK_INTERNAL_JWKS_URL,
        oauth_valid_audiences="https://integration.test.elaad.nl,",
        oauth_token_url=KEYCLOAK_INTERNAL_TOKEN_URL,
        network=integration_test_docker_network,
    ) as vtn_container:
        yield IntegrationTestVTNServer(
            vtn_base_url=vtn_container.get_base_url(),
            allow_insecure_http=True,
        )


@pytest.fixture(scope="session")
def bl_oauth_configuration(
    integration_test_auth_server: KeycloakContainer,
) -> OAuthConfiguration:
    """
    A testcontainers keycloak fixture which is initialized once per test run.

    Yields an OAuthConfiguration which contains an oauth client that was created
    for the scope of this test session. This OAUTH client is configured to have the BL scopes inside an OpenADR VTN.
    """
    token_url = integration_test_auth_server.get_url() + f"/realms/{KEYCLOAK_REALM_NAME}/protocol/openid-connect/token"
    return OAuthConfiguration(
        client_id=KEYCLOAK_BL_CLIENT_ID,
        client_secret=KEYCLOAK_BL_CLIENT_SECRET,
        token_url=token_url,
        scopes=None,
        audience=None,
    )


@pytest.fixture(scope="session")
def ven_oauth_configuration(
    integration_test_auth_server: KeycloakContainer,
) -> OAuthConfiguration:
    """
    A testcontainers keycloak fixture which is initialized once per test run.

    Yields an OAuthConfiguration which contains an oauth client that was created
    for the scope of this test session. This OAUTH client is configured to have the VEN scopes inside an OpenADR VTN.
    """
    token_url = integration_test_auth_server.get_url() + f"/realms/{KEYCLOAK_REALM_NAME}/protocol/openid-connect/token"
    return OAuthConfiguration(
        client_id=KEYCLOAK_VEN1_CLIENT_ID,
        client_secret=KEYCLOAK_VEN_CLIENT_SECRET,
        token_url=token_url,
        scopes=None,
        audience=None,
    )


@pytest.fixture(scope="session")
def bl_client(
    integration_test_openadr310_vtn_server: IntegrationTestVTNServer,
    bl_oauth_configuration: OAuthConfiguration,
) -> IntegrationTestVTNClient:
    """
    Return a BL-side VTN client with server info and OAuth credentials.

    Args:
        integration_test_openadr310_vtn_server (IntegrationTestVTNServer): OpenLEADR-rs VTN server.
        bl_oauth_configuration (OAuthConfiguration): OAuth configuration for the BL token.

    Yields:
        Iterable[IntegrationTestVTNClient]: The integration test vtn client.

    """
    return IntegrationTestVTNClient(
        vtn_configuration=integration_test_openadr310_vtn_server,
        oauth_configuration=bl_oauth_configuration,
    )


@pytest.fixture(scope="session")
def ven_client(
    integration_test_openadr310_vtn_server: IntegrationTestVTNServer,
    ven_oauth_configuration: OAuthConfiguration,
) -> IntegrationTestVTNClient:
    """
    Return a VEN-side VTN client with server info and OAuth credentials.

    Args:
        integration_test_openadr310_vtn_server (IntegrationTestVTNServer): OpenLEADR-rs VTN server.
        ven_oauth_configuration (OAuthConfiguration): OAuth configuration for the VEN token.

    Yields:
        Iterable[IntegrationTestVTNClient]: The integration test vtn client.

    """
    return IntegrationTestVTNClient(
        vtn_configuration=integration_test_openadr310_vtn_server,
        oauth_configuration=ven_oauth_configuration,
    )


@pytest.fixture(scope="session")
def vtn_capacity_limit_event_seed(
    bl_client: IntegrationTestVTNClient,
) -> ExistingEvent:
    """Program + 24h capacity-limit event on the VTN, starting one hour from seed time."""
    bl_http = create_bl_http_client(bl_client)
    return seed_capacity_limit_event(
        bl_http,
    )
