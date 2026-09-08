"""FastAPI application exposing the stable diagnostic contract."""

from __future__ import annotations

from typing import Annotated, Literal, cast

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import JSONResponse

from stock_profiler.adapters.authentication.passkeys import (
    CSRF_COOKIE_NAME,
    AuthenticationError,
    ChallengePurpose,
    PasskeyAuthenticator,
)
from stock_profiler.adapters.persistence.result_delivery import ResultDelivery
from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage
from stock_profiler.bootstrap.decision_cases import get_formal_report
from stock_profiler.bootstrap.settings import Settings, load_settings
from stock_profiler.foundation.clock import Clock
from stock_profiler.foundation.logging import log_operational_event
from stock_profiler.foundation.versioning import build_version_bundle
from stock_profiler.modules.decision_cases.domain import FormalReport
from stock_profiler.modules.delivery.access import SINGLE_USER_ID, AccessPrincipal
from stock_profiler.modules.delivery.monitoring_workspace import MonitoringWorkspace
from stock_profiler.modules.delivery.user_facts import UserFact, UserFactRequest


class VersionDiagnosticDto(BaseModel):
    """Pydantic-only transport contract for the public diagnostic interface."""

    model_config = ConfigDict(frozen=True)

    application_version: str
    source_sha: str
    m_agent_version: str
    m_agent_wheel_url: str
    m_agent_wheel_sha256: str
    m_agent_release_commit: str


class OpaqueRequestErrorDto(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    detail: Literal["request is not permitted"] = "request is not permitted"


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


class ChallengeDto(BaseModel):
    model_config = ConfigDict(frozen=True)

    challenge_id: str
    options: dict[str, object]


class CredentialRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    challenge_id: str
    credential: dict[str, object]


class AuthenticationVerificationDto(BaseModel):
    """Pydantic transport contract for a completed passkey authentication."""

    model_config = ConfigDict(frozen=True)

    csrf_token: str


class ReauthenticationVerificationDto(BaseModel):
    """Pydantic transport contract for a completed recent reauthentication."""

    model_config = ConfigDict(frozen=True)

    credential_id: str


class SessionRefreshDto(BaseModel):
    """Pydantic transport contract for a CSRF-protected idle-session refresh."""

    model_config = ConfigDict(frozen=True)

    status: str


def _challenge_dto(payload: dict[str, object]) -> ChallengeDto:
    """Translate the adapter's value boundary into the generated HTTP contract."""
    return ChallengeDto(
        challenge_id=cast(str, payload["challenge_id"]),
        options=cast(dict[str, object], payload["options"]),
    )


def create_app(settings: Settings | None = None, *, clock: Clock | None = None) -> FastAPI:
    """Build the HTTP transport without exposing persistence entities."""
    app_settings = settings or load_settings()
    if app_settings.process_role != "api":
        raise ValueError("HTTP entrypoint requires api process role")
    app = FastAPI(
        title="Stock Profiler",
        version="0.1.0.dev0",
        openapi_version="3.1.0",
        docs_url=None,
        redoc_url=None,
        responses={422: {"model": OpaqueRequestErrorDto}},
    )

    @app.middleware("http")
    async def prohibit_private_api_caching(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
            if (response.status_code == 404 and request.scope.get("route") is None) or (
                response.status_code == 405
            ):
                ResultDelivery.from_settings(app_settings, clock=clock).record_capability_denial(
                    request.method + " " + request.url.path, "HTTP"
                )
        return response

    @app.exception_handler(RequestValidationError)
    async def opaque_validation_denial(
        request: Request, _error: RequestValidationError
    ) -> JSONResponse:
        ResultDelivery.from_settings(app_settings, clock=clock).record_capability_denial(
            request.method + " " + request.url.path, "HTTP"
        )
        return JSONResponse(status_code=422, content=OpaqueRequestErrorDto().model_dump())

    def report_principal() -> AccessPrincipal:
        return AccessPrincipal(
            user_id=SINGLE_USER_ID,
            account_ids=app_settings.report_account_ids,
            permissions=app_settings.report_permissions,
        )

    def authenticator() -> PasskeyAuthenticator:
        return PasskeyAuthenticator(
            initialize_runtime_storage(app_settings).engine, app_settings, clock=clock
        )

    def require_expected_origin(origin: Annotated[str | None, Header()] = None) -> None:
        if origin != app_settings.auth_origin:
            raise HTTPException(status_code=403, detail="origin is not authorized")

    @app.post(
        "/api/v1/auth/passkeys/registration/options",
        response_model=ChallengeDto,
        responses={403: {"description": "Host console grant required"}},
    )
    def registration_options(
        grant_id: str = Header(alias="X-Host-Console-Grant"),
        _: None = Depends(require_expected_origin),
    ) -> ChallengeDto:
        try:
            return _challenge_dto(authenticator().registration_options(grant_id))
        except AuthenticationError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error

    @app.post(
        "/api/v1/auth/passkeys/registration/verify",
        status_code=204,
        responses={401: {"description": "Registration verification failed"}},
    )
    def registration_verify(
        request: CredentialRequest, _: None = Depends(require_expected_origin)
    ) -> Response:
        try:
            authenticator().registration_verify(request.challenge_id, request.credential)
        except AuthenticationError as error:
            raise HTTPException(status_code=401, detail=str(error)) from error
        return Response(status_code=204)

    @app.post(
        "/api/v1/auth/passkeys/authentication/options",
        response_model=ChallengeDto,
        responses={401: {"description": "No enrolled passkey is available"}},
    )
    def authentication_options(_: None = Depends(require_expected_origin)) -> ChallengeDto:
        try:
            return _challenge_dto(authenticator().authentication_options())
        except AuthenticationError as error:
            raise HTTPException(status_code=401, detail=str(error)) from error

    @app.post(
        "/api/v1/auth/passkeys/authentication/verify",
        response_model=AuthenticationVerificationDto,
        responses={401: {"description": "Authentication verification failed"}},
    )
    def authentication_verify(
        request: CredentialRequest,
        response: Response,
        _: None = Depends(require_expected_origin),
    ) -> AuthenticationVerificationDto:
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
            max_age=app_settings.auth_session_absolute_ttl_seconds,
        )
        response.set_cookie(
            key=CSRF_COOKIE_NAME,
            value=csrf_token,
            secure=True,
            samesite="lax",
            path="/",
            max_age=app_settings.auth_session_absolute_ttl_seconds,
        )
        return AuthenticationVerificationDto(csrf_token=csrf_token)

    @app.post(
        "/api/v1/auth/passkeys/reauthentication/options",
        response_model=ChallengeDto,
        responses={403: {"description": "Recent authenticated session required"}},
    )
    def reauthentication_options(
        session_token: Annotated[str | None, Cookie(alias="__Host-stock_profiler_session")] = None,
        csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
        _: None = Depends(require_expected_origin),
    ) -> ChallengeDto:
        try:
            authenticator().require_mutable_session(session_token, csrf_token)
            return _challenge_dto(
                authenticator().authentication_options(ChallengePurpose.REAUTHENTICATION)
            )
        except AuthenticationError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error

    @app.post(
        "/api/v1/auth/passkeys/reauthentication/verify",
        response_model=ReauthenticationVerificationDto,
        responses={403: {"description": "Reauthentication verification failed"}},
    )
    def reauthentication_verify(
        request: CredentialRequest,
        session_token: Annotated[str | None, Cookie(alias="__Host-stock_profiler_session")] = None,
        csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
        _: None = Depends(require_expected_origin),
    ) -> ReauthenticationVerificationDto:
        try:
            credential_id = authenticator().verify_assertion(
                request.challenge_id, request.credential, ChallengePurpose.REAUTHENTICATION
            )
            access = authenticator().reauthenticate(session_token, csrf_token, credential_id)
        except AuthenticationError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        return ReauthenticationVerificationDto(credential_id=access.credential_id)

    @app.post(
        "/api/v1/auth/session/refresh",
        response_model=SessionRefreshDto,
        responses={403: {"description": "Origin, CSRF token, or session was invalid"}},
    )
    def refresh_session(
        response: Response,
        session_token: Annotated[str | None, Cookie(alias="__Host-stock_profiler_session")] = None,
        csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
        _: None = Depends(require_expected_origin),
    ) -> SessionRefreshDto:
        try:
            access = authenticator().refresh_session(session_token, csrf_token)
        except AuthenticationError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        response.set_cookie(
            key=app_settings.auth_session_cookie_name,
            value=session_token or "",
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
            max_age=access.cookie_max_age_seconds,
        )
        return SessionRefreshDto(status="authenticated")

    @app.delete(
        "/api/v1/auth/session",
        status_code=204,
        responses={403: {"description": "Origin, CSRF token, or session was invalid"}},
    )
    def logout(
        session_token: Annotated[str | None, Cookie(alias="__Host-stock_profiler_session")] = None,
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
        response.delete_cookie(
            key=CSRF_COOKIE_NAME,
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

    @app.get("/api/v1/monitoring", response_model=MonitoringWorkspace)
    def monitoring_workspace(
        session_token: Annotated[str | None, Cookie(alias="__Host-stock_profiler_session")] = None,
    ) -> MonitoringWorkspace:
        delivery = ResultDelivery.from_settings(app_settings, clock=clock)
        try:
            authenticator().require_session(session_token)
        except AuthenticationError as error:
            delivery.monitoring_workspace(None)
            raise HTTPException(status_code=401, detail="authentication required") from error
        workspace = delivery.monitoring_workspace(report_principal())
        if workspace is None:
            raise HTTPException(status_code=404, detail="monitoring unavailable")
        return workspace

    @app.get(
        "/api/v1/reports/{report_version_id}",
        response_model=FormalReport,
        responses={
            401: {"description": "Authentication required"},
            404: {"description": "Formal report not found"},
        },
    )
    def formal_report(
        report_version_id: str,
        session_token: Annotated[str | None, Cookie(alias="__Host-stock_profiler_session")] = None,
    ) -> FormalReport:
        try:
            authenticator().require_session(session_token)
        except AuthenticationError as error:
            get_formal_report(report_version_id, app_settings, clock=clock)
            raise HTTPException(status_code=401, detail=str(error)) from error
        report = get_formal_report(
            report_version_id,
            app_settings,
            principal=report_principal(),
            clock=clock,
        )
        if report is None:
            raise HTTPException(status_code=404, detail="formal report not found")
        return report

    @app.get("/api/v1/reports/{report_version_id}/facts", response_model=list[UserFact])
    def report_user_facts(
        report_version_id: str,
        session_token: Annotated[str | None, Cookie(alias="__Host-stock_profiler_session")] = None,
    ) -> list[UserFact]:
        delivery = ResultDelivery.from_settings(app_settings, clock=clock)
        try:
            authenticator().require_session(session_token)
        except AuthenticationError as error:
            delivery.read_report(report_version_id)
            raise HTTPException(status_code=401, detail="authentication required") from error
        facts = delivery.user_facts(report_version_id, report_principal())
        if facts is None:
            raise HTTPException(status_code=404, detail="formal report not found")
        return list(facts)

    @app.post("/api/v1/reports/{report_version_id}/facts", response_model=UserFact)
    def record_report_user_fact(
        report_version_id: str,
        request: UserFactRequest,
        session_token: Annotated[str | None, Cookie(alias="__Host-stock_profiler_session")] = None,
        csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
        origin: str | None = Header(default=None),
    ) -> UserFact:
        delivery = ResultDelivery.from_settings(app_settings, clock=clock)
        try:
            if origin != app_settings.auth_origin:
                raise AuthenticationError("origin is not authorized")
            authenticator().require_mutable_session(session_token, csrf_token)
        except AuthenticationError as error:
            delivery.read_report(report_version_id)
            raise HTTPException(status_code=403, detail="request is not permitted") from error
        fact = delivery.record_user_fact(report_version_id, report_principal(), request)
        if fact is None:
            raise HTTPException(status_code=404, detail="formal report not found")
        return fact

    return app


app = create_app()
