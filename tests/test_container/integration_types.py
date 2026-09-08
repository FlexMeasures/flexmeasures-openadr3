"""Shared types for OpenADR integration and E2E tests."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class OAuthConfiguration:
    """OAuth client credentials and token endpoint settings."""

    client_id: str
    client_secret: str
    token_url: str
    scopes: list[str] | None
    audience: str | None


@dataclass
class IntegrationTestVTNServer:
    """Contains information on the VTN server used during tests."""

    vtn_base_url: str
    allow_insecure_http: bool


@dataclass
class IntegrationTestVTNClient:
    """Contains information on the VTN client used during tests."""

    vtn_configuration: IntegrationTestVTNServer
    oauth_configuration: OAuthConfiguration
