import os
from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest
from openadr3_client.oadr310.models.event.event import ExistingEvent
from testcontainers.core.network import Network
from testcontainers.keycloak import KeycloakContainer

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

_TESTS_ROOT = Path(__file__).resolve().parents[1]
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
