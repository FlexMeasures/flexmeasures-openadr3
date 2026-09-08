"""PostgreSQL testcontainer used by flexmeasures during integration tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

from testcontainers.postgres import PostgresContainer

if TYPE_CHECKING:
    from types import TracebackType

FLEXMEASURES_TEST_POSTGRES_IMAGE = "postgres"
FLEXMEASURES_TEST_DB_NAME = "flexmeasures_test"
FLEXMEASURES_TEST_DB_USER = "flexmeasures_test"
FLEXMEASURES_TEST_DB_PASSWORD = "flexmeasures_test"


@dataclass(frozen=True)
class FlexMeasuresTestPostgres:
    """Connection details for the FlexMeasures test database."""

    database_uri: str
    host: str
    port: int
    dbname: str
    username: str
    password: str


class FlexMeasuresTestPostgresContainer:
    """Session-scoped PostgreSQL container for FlexMeasures plugin tests."""

    def __init__(self) -> None:
        container = PostgresContainer(
            image=FLEXMEASURES_TEST_POSTGRES_IMAGE,
            username=FLEXMEASURES_TEST_DB_USER,
            password=FLEXMEASURES_TEST_DB_PASSWORD,
            dbname=FLEXMEASURES_TEST_DB_NAME,
        )
        self._container = container

    def start(self) -> Self:
        """Start the PostgreSQL container."""
        self._container.start()
        return self

    def stop(self) -> None:
        """Stop the PostgreSQL container."""
        self._container.stop()

    def _get_database_uri(self) -> str:
        """SQLAlchemy URI for FlexMeasures ``TestingConfig`` (localhost + mapped port)."""
        url = str(self._container.get_connection_url())
        # Align with flexmeasures.utils.config_defaults.TestingConfig (postgresql://).
        return url.replace("postgresql+psycopg2://", "postgresql://", 1)

    def get_connection_info(self) -> FlexMeasuresTestPostgres:
        """Return connection details for the running test database."""
        host = self._container.get_container_host_ip()
        port = int(self._container.get_exposed_port(self._container.port))
        return FlexMeasuresTestPostgres(
            database_uri=self._get_database_uri(),
            host=host,
            port=port,
            dbname=FLEXMEASURES_TEST_DB_NAME,
            username=FLEXMEASURES_TEST_DB_USER,
            password=FLEXMEASURES_TEST_DB_PASSWORD,
        )

    def __enter__(self) -> Self:
        """Enter the container context manager."""
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit the container context manager."""
        self.stop()
