"""Connection settings for the capacity-limits walkthrough stack."""

KEYCLOAK_REALM = "integration-test-realm"

# OAuth clients from keycloak/realm.json (same as tests/keycloak_integration_realm.json)
BL_OAUTH_CLIENT_ID = "test-client-id"
VEN_OAUTH_CLIENT_ID = "test-ven-1"
OAUTH_CLIENT_SECRET = "my-client-secret"

# URLs when running Python scripts on your host machine
HOST_KEYCLOAK_TOKEN_URL = f"http://localhost:8080/realms/{KEYCLOAK_REALM}/protocol/openid-connect/token"
HOST_VTN_BASE_URL = "http://localhost:3000"

# URLs to enter in the FlexMeasures UI (resolved inside the Docker network)
DOCKER_VTN_BASE_URL = "http://openleadr-vtn:3000"
DOCKER_KEYCLOAK_TOKEN_URL = f"http://keycloak:8080/realms/{KEYCLOAK_REALM}/protocol/openid-connect/token"

# Suggested names for the UI walkthrough
VEN_CLIENT_NAME = "demo-ven"
POLLING_SCHEDULE_NAME = "demo-poll"

# FlexMeasures web UI
FLEXMEASURES_URL = "http://localhost:5002"
FLEXMEASURES_LOGIN_EMAIL = "toy-user@flexmeasures.io"
FLEXMEASURES_LOGIN_PASSWORD = "toy-password"
