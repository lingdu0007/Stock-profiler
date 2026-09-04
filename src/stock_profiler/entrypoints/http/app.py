"""FastAPI application exposing the stable diagnostic contract."""

from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict

from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage
from stock_profiler.bootstrap.settings import Settings, load_settings
from stock_profiler.foundation.logging import log_operational_event
from stock_profiler.foundation.versioning import build_version_bundle


class VersionDiagnosticDto(BaseModel):
    """Pydantic-only transport contract for the public diagnostic interface."""

    model_config = ConfigDict(frozen=True)

    application_version: str
    source_sha: str
    m_agent_version: str
    m_agent_wheel_url: str
    m_agent_wheel_sha256: str
    m_agent_release_commit: str


class HealthDto(BaseModel):
    """Pydantic-only transport contract for service health."""

    model_config = ConfigDict(frozen=True)

    status: str


class SafetyCapabilitiesDiagnosticDto(BaseModel):
    """Machine-readable official-project safety invariants."""

    model_config = ConfigDict(frozen=True)

    single_user: bool
    public_recommendation_service: bool
    order_writing: bool


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the HTTP transport without exposing persistence entities."""
    app_settings = settings or load_settings()
    app = FastAPI(
        title="Stock Profiler Diagnostics",
        version="0.1.0.dev0",
        openapi_version="3.1.0",
        docs_url=None,
        redoc_url=None,
    )

    @app.get("/livez", response_model=HealthDto, include_in_schema=False)
    def livez() -> HealthDto:
        log_operational_event(
            event="http.health",
            component="api",
            operation="liveness",
            status="live",
            version="0.1.0.dev0",
            git_sha=app_settings.source_sha,
        )
        return HealthDto(status="live")

    @app.get("/readyz", response_model=HealthDto, include_in_schema=False)
    def readyz() -> HealthDto:
        initialize_runtime_storage(app_settings).write_heartbeat("api")
        log_operational_event(
            event="http.health",
            component="api",
            operation="readiness",
            status="ready",
            version="0.1.0.dev0",
            git_sha=app_settings.source_sha,
        )
        return HealthDto(status="ready")

    @app.get("/api/v1/diagnostics/version", response_model=VersionDiagnosticDto)
    def version_diagnostic() -> VersionDiagnosticDto:
        bundle = build_version_bundle(app_settings)
        log_operational_event(
            event="http.diagnostic",
            component="api",
            operation="version",
            status="ready",
            version=bundle.application_version,
            git_sha=bundle.source_sha,
        )
        return VersionDiagnosticDto(**bundle.to_dto())

    @app.get(
        "/api/v1/diagnostics/safety-capabilities",
        response_model=SafetyCapabilitiesDiagnosticDto,
    )
    def safety_capabilities_diagnostic() -> SafetyCapabilitiesDiagnosticDto:
        """Report static safety boundaries without exposing a product action."""
        log_operational_event(
            event="http.diagnostic",
            component="api",
            operation="safety-capabilities",
            status="ready",
            version="0.1.0.dev0",
            git_sha=app_settings.source_sha,
        )
        return SafetyCapabilitiesDiagnosticDto(
            single_user=True,
            public_recommendation_service=False,
            order_writing=False,
        )

    return app


app = create_app()
