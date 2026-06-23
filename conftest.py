"""Top-level pytest configuration (pytest_plugins must live here)."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from flask import Flask
from flexmeasures.app import create as create_flexmeasures_app

from tests.test_container.flexmeasures_test_postgres_container import (
    FlexMeasuresTestPostgres,
    FlexMeasuresTestPostgresContainer,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

pytest_plugins = [
    "flexmeasures.conftest",
]


@pytest.fixture(scope="session")
def integration_test_flexmeasures_postgres() -> Iterator[FlexMeasuresTestPostgres]:
    """Session-scoped PostgreSQL testcontainer for FlexMeasures during integration tests."""
    with FlexMeasuresTestPostgresContainer() as postgres_container:
        yield postgres_container.get_connection_info()


@pytest.fixture(scope="session")
def app(
    integration_test_flexmeasures_postgres: FlexMeasuresTestPostgres,
) -> Iterator[Flask]:
    """FlexMeasures app with this plugin loaded (overrides flexmeasures.conftest.app)."""
    # FlexMeasures reads this env var during create_app() (see read_config in testing mode).
    # Setting SQLALCHEMY_DATABASE_URI on the app config afterwards is too late: db.init_app()
    # already created a cached engine bound to TestingConfig's localhost default.
    os.environ["SQLALCHEMY_TEST_DATABASE_URI"] = integration_test_flexmeasures_postgres.database_uri

    test_app = create_flexmeasures_app(
        env="testing",
        plugins=["flexmeasures_openadr3"],
    )
    test_app.config["OPENADR_SECRETS_ENCRYPTION_KEY"] = "test-openadr-secrets-key"
    test_app.config["ALLOW_INSECURE_HTTP_VTN"] = "true"
    test_app.config["SERVER_NAME"] = "localhost"
    test_app.config["PREFERRED_URL_SCHEME"] = "http"

    ctx = test_app.app_context()
    ctx.push()

    yield test_app

    ctx.pop()

    os.environ.pop("SQLALCHEMY_TEST_DATABASE_URI", None)
