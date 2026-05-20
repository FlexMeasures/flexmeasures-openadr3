"""Shared types for OpenADR integration and E2E tests."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class OAuthConfiguration:
    client_id: str
    client_secret: str
    token_url: str
    scopes: list[str] | None
    audience: str | None


@dataclass
class IntegrationTestVTNServer:
    vtn_base_url: str
    allow_insecure_http: bool


@dataclass
class IntegrationTestVTNClient:
    vtn_configuration: IntegrationTestVTNServer
    oauth_configuration: OAuthConfiguration
