"""FastAPI application exposing the stable diagnostic contract."""

from __future__ import annotations

from typing import Annotated, Literal, cast

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict
from starlette.middleware.base import RequestResponseEndpoint

from stock_profiler.adapters.authentication.passkeys import (
    AuthenticationError,
    PasskeyAuthenticator,
)
from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage
from stock_profiler.bootstrap.settings import Settings, load_settings
from stock_profiler.foundation.logging import log_operational_event
from stock_profiler.foundation.versioning import build_version_bundle
from stock_profiler.modules.decision_cases.domain import FormalReport
from stock_profiler.modules.decision_cases.service import get_formal_report


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


class HostGrantRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    purpose: Literal["bootstrap", "recovery"]


class ChallengeDto(BaseModel):
    model_config = ConfigDict(frozen=True)

    challenge_id: str
    options: dict[str, object]


class CredentialRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    challenge_id: str
    credential: dict[str, object]


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the HTTP transport without exposing persistence entities."""
    app_settings = settings or load_settings()
    if app_settings.process_role != "api":
        raise ValueError("HTTP entrypoint requires api process role")
    app = FastAPI(
        title="Stock Profiler Diagnostics",
        version="0.1.0.dev0",
        openapi_version="3.1.0",
        docs_url=None,
        redoc_url=None,
    )

    @app.middleware("http")
    async def prohibit_private_api_caching(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        if request.url.path.startswith("/api/v1/auth/") or request.url.path.startswith(
            "/api/v1/reports/"
        ):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
        return response

    def authenticator() -> PasskeyAuthenticator:
        return PasskeyAuthenticator(initialize_runtime_storage(app_settings).engine, app_settings)

    def require_expected_origin(origin: Annotated[str | None, Header()] = None) -> None:
        if origin != app_settings.auth_origin:
            raise HTTPException(status_code=403, detail="origin is not authorized")

    @app.post("/api/v1/auth/host-console/grants")
    def host_console_grant(
        request: HostGrantRequest,
        host_token: str | None = Header(default=None, alias="X-Host-Token"),
        _: None = Depends(require_expected_origin),
    ) -> dict[str, str]:
        try:
            return {
                "grant_id": authenticator().create_host_grant(request.purpose, host_token or "")
            }
        except AuthenticationError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error

    @app.post("/api/v1/auth/passkeys/registration/options", response_model=ChallengeDto)
    def registration_options(
        grant_id: str = Header(alias="X-Host-Console-Grant"),
        _: None = Depends(require_expected_origin),
    ) -> ChallengeDto:
        try:
            payload = authenticator().registration_options(grant_id)
            return ChallengeDto(
                challenge_id=cast(str, payload["challenge_id"]),
                options=cast(dict[str, object], payload["options"]),
            )
        except AuthenticationError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error

    @app.post("/api/v1/auth/passkeys/registration/verify", status_code=204)
    def registration_verify(
        request: CredentialRequest, _: None = Depends(require_expected_origin)
    ) -> Response:
        try:
            authenticator().registration_verify(request.challenge_id, request.credential)
        except AuthenticationError as error:
            raise HTTPException(status_code=401, detail=str(error)) from error
        return Response(status_code=204)

    @app.post("/api/v1/auth/passkeys/authentication/options", response_model=ChallengeDto)
    def authentication_options(_: None = Depends(require_expected_origin)) -> ChallengeDto:
        try:
            payload = authenticator().authentication_options()
            return ChallengeDto(
                challenge_id=cast(str, payload["challenge_id"]),
                options=cast(dict[str, object], payload["options"]),
            )
        except AuthenticationError as error:
            raise HTTPException(status_code=401, detail=str(error)) from error

    @app.post("/api/v1/auth/passkeys/authentication/verify")
    def authentication_verify(
        request: CredentialRequest,
        response: Response,
        _: None = Depends(require_expected_origin),
    ) -> dict[str, str]:
        try:
            session_token, csrf_token = authenticator().authentication_verify(
                request.challenge_id, request.credential
            )
        except AuthenticationError as error:
            raise HTTPException(status_code=401, detail=str(error)) from error
        response.set_cookie(
            key=app_settings.auth_session_cookie_name,
            value=session_token,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
            max_age=app_settings.auth_session_idle_ttl_seconds,
        )
        return {"csrf_token": csrf_token}

    @app.post("/api/v1/auth/passkeys/reauthentication/options", response_model=ChallengeDto)
    def reauthentication_options(
        session_token: Annotated[
            str | None, Cookie(alias="__Host-stock_profiler_session")
        ] = None,
        csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
        _: None = Depends(require_expected_origin),
    ) -> ChallengeDto:
        try:
            authenticator().authorize_session_mutation(session_token, csrf_token)
            payload = authenticator().authentication_options("reauthentication")
            return ChallengeDto(
                challenge_id=cast(str, payload["challenge_id"]),
                options=cast(dict[str, object], payload["options"]),
            )
        except AuthenticationError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error

    @app.post("/api/v1/auth/passkeys/reauthentication/verify")
    def reauthentication_verify(
        request: CredentialRequest,
        session_token: Annotated[
            str | None, Cookie(alias="__Host-stock_profiler_session")
        ] = None,
        csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
        _: None = Depends(require_expected_origin),
    ) -> dict[str, str]:
        try:
            credential_id = authenticator().verify_assertion(
                request.challenge_id, request.credential, "reauthentication"
            )
            access = authenticator().reauthenticate(
                session_token, csrf_token, credential_id
            )
        except AuthenticationError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        return {"credential_id": access.credential_id}

    @app.delete("/api/v1/auth/session", status_code=204)
    def logout(
        session_token: Annotated[
            str | None, Cookie(alias="__Host-stock_profiler_session")
        ] = None,
        csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
        _: None = Depends(require_expected_origin),
    ) -> Response:
        try:
            authenticator().logout(session_token, csrf_token)
        except AuthenticationError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        response = Response(status_code=204)
        response.delete_cookie(
            key=app_settings.auth_session_cookie_name,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
        )
        return response

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

    @app.get(
        "/api/v1/reports/{report_version_id}",
        response_model=FormalReport,
        responses={401: {"description": "Authentication required"}},
    )
    def formal_report(
        report_version_id: str,
        response: Response,
        session_token: Annotated[
            str | None, Cookie(alias="__Host-stock_profiler_session")
        ] = None,
    ) -> FormalReport:
        try:
            access = authenticator().require_session(session_token)
        except AuthenticationError as error:
            raise HTTPException(status_code=401, detail=str(error)) from error
        report = get_formal_report(report_version_id, app_settings)
        if report is None:
            raise HTTPException(status_code=404, detail="formal report not found")
        response.set_cookie(
            key=app_settings.auth_session_cookie_name,
            value=session_token or "",
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
            max_age=access.cookie_max_age_seconds,
        )
        return report

    return app


app = create_app()
