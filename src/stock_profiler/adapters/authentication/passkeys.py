"""Single-user WebAuthn authentication with opaque SQLite sessions."""

from __future__ import annotations

import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from sqlalchemy import select, update
from sqlalchemy.engine import Engine
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (
    PublicKeyCredentialCreationOptions,
    PublicKeyCredentialDescriptor,
    PublicKeyCredentialRequestOptions,
)

from stock_profiler.adapters.persistence.runtime_ownership import (
    AUTH_CHALLENGES,
    AUTH_CREDENTIALS,
    AUTH_HOST_GRANTS,
    AUTH_SESSIONS,
)
from stock_profiler.bootstrap.settings import Settings


class AuthenticationError(RuntimeError):
    """Authentication data is missing, expired, invalid, or not authorized."""


@dataclass(frozen=True)
class SessionAccess:
    """Validated session identity plus the remaining browser-cookie lifetime."""

    credential_id: str
    cookie_max_age_seconds: int


class PasskeyAuthenticator:
    """Own the WebAuthn ceremony and server-side session state."""

    def __init__(self, engine: Engine, settings: Settings) -> None:
        self._engine = engine
        self._settings = settings

    def create_host_grant(self, purpose: str, token: str) -> str:
        expected_secret = (
            self._settings.auth_bootstrap_token
            if purpose == "bootstrap"
            else self._settings.auth_recovery_token
        )
        expected = expected_secret.get_secret_value() if expected_secret else ""
        if (
            purpose not in {"bootstrap", "recovery"}
            or not expected
            or not secrets.compare_digest(token, expected)
        ):
            raise AuthenticationError("host console authorization failed")
        grant_id = secrets.token_urlsafe(32)
        with self._engine.begin() as connection:
            connection.execute(
                AUTH_HOST_GRANTS.insert().values(
                    grant_id=grant_id,
                    purpose=purpose,
                    expires_at=self._expires(self._settings.auth_bootstrap_token_ttl_seconds),
                )
            )
        return grant_id

    def registration_options(self, grant_id: str) -> dict[str, object]:
        self._require_active_grant(grant_id)
        options = generate_registration_options(
            rp_id=self._settings.auth_rp_id,
            rp_name="Stock Profiler",
            user_id=b"stock-profiler-single-user",
            user_name="single-user",
            user_display_name="Stock Profiler User",
            timeout=self._settings.auth_challenge_ttl_seconds * 1000,
        )
        return self._save_challenge("registration", options.challenge, grant_id, options)

    def registration_verify(self, challenge_id: str, credential: dict[str, object]) -> None:
        challenge, grant_id = self._consume_challenge(challenge_id, "registration")
        if grant_id is None:
            raise AuthenticationError("registration is missing a host console grant")
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=self._settings.auth_rp_id,
            expected_origin=self._settings.auth_origin,
        )
        with self._engine.begin() as connection:
            self._consume_active_grant(connection, grant_id)
            connection.execute(
                AUTH_CREDENTIALS.insert().values(
                    credential_id=bytes_to_base64url(verified.credential_id),
                    credential_public_key=bytes_to_base64url(verified.credential_public_key),
                    sign_count=str(verified.sign_count),
                    created_at=self._now(),
                )
            )

    def authentication_options(self, purpose: str = "authentication") -> dict[str, object]:
        with self._engine.connect() as connection:
            credentials = (
                connection.execute(select(AUTH_CREDENTIALS.c.credential_id)).scalars().all()
            )
        if not credentials:
            raise AuthenticationError("no passkey is enrolled")
        options = generate_authentication_options(
            rp_id=self._settings.auth_rp_id,
            timeout=self._settings.auth_challenge_ttl_seconds * 1000,
            allow_credentials=[
                PublicKeyCredentialDescriptor(id=base64url_to_bytes(credential_id))
                for credential_id in credentials
            ],
        )
        return self._save_challenge(purpose, options.challenge, None, options)

    def authentication_verify(
        self, challenge_id: str, credential: dict[str, object], purpose: str = "authentication"
    ) -> tuple[str, str]:
        credential_id = self.verify_assertion(challenge_id, credential, purpose)
        return self._create_session(credential_id)

    def verify_assertion(
        self, challenge_id: str, credential: dict[str, object], purpose: str
    ) -> str:
        """Verify one assertion and advance its stored signature counter."""
        challenge, _ = self._consume_challenge(challenge_id, purpose)
        credential_id = str(credential.get("id", ""))
        with self._engine.connect() as connection:
            stored = (
                connection.execute(
                    select(AUTH_CREDENTIALS).where(
                        AUTH_CREDENTIALS.c.credential_id == credential_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        if stored is None:
            raise AuthenticationError("unknown passkey")
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=self._settings.auth_rp_id,
            expected_origin=self._settings.auth_origin,
            credential_public_key=base64url_to_bytes(stored["credential_public_key"]),
            credential_current_sign_count=int(stored["sign_count"]),
        )
        with self._engine.begin() as connection:
            connection.execute(
                update(AUTH_CREDENTIALS)
                .where(AUTH_CREDENTIALS.c.credential_id == credential_id)
                .values(sign_count=str(verified.new_sign_count))
            )
        return credential_id

    def require_session(
        self, session_token: str | None, require_recent_reauthentication: bool = False
    ) -> SessionAccess:
        if not session_token:
            raise AuthenticationError("authentication required")
        with self._engine.connect() as connection:
            stored = (
                connection.execute(
                    select(AUTH_SESSIONS).where(
                        AUTH_SESSIONS.c.session_hash == _digest(session_token)
                    )
                )
                .mappings()
                .one_or_none()
            )
        if (
            stored is None
            or self._expired(stored["absolute_expires_at"])
            or self._idle_expired(stored["last_seen_at"])
            or (
                require_recent_reauthentication
                and self._recent_reauthentication_expired(stored["recent_reauth_at"])
            )
        ):
            raise AuthenticationError("session expired")
        with self._engine.begin() as connection:
            connection.execute(
                update(AUTH_SESSIONS)
                .where(AUTH_SESSIONS.c.session_hash == _digest(session_token))
                .values(last_seen_at=self._now())
            )
        return SessionAccess(
            credential_id=str(stored["credential_id"]),
            cookie_max_age_seconds=self._cookie_max_age_seconds(stored["absolute_expires_at"]),
        )

    def reauthenticate(
        self, session_token: str | None, csrf_token: str | None, credential_id: str
    ) -> SessionAccess:
        """Mark an active session recent only after CSRF and same-credential verification."""
        access = self._authorize_session_mutation(session_token, csrf_token)
        if access.credential_id != credential_id:
            raise AuthenticationError("reauthentication credential does not match the session")
        with self._engine.begin() as connection:
            connection.execute(
                update(AUTH_SESSIONS)
                .where(AUTH_SESSIONS.c.session_hash == _digest(session_token or ""))
                .values(recent_reauth_at=self._now())
            )
        return access

    def authorize_session_mutation(
        self, session_token: str | None, csrf_token: str | None
    ) -> SessionAccess:
        """Require a live opaque session and its double-submit CSRF token."""
        return self._authorize_session_mutation(session_token, csrf_token)

    def logout(self, session_token: str | None, csrf_token: str | None) -> None:
        """Destroy one authenticated browser session after its double-submit CSRF check."""
        if not session_token or not csrf_token:
            raise AuthenticationError("session and CSRF token are required")
        session_hash = _digest(session_token)
        with self._engine.begin() as connection:
            stored = (
                connection.execute(
                    select(AUTH_SESSIONS).where(AUTH_SESSIONS.c.session_hash == session_hash)
                )
                .mappings()
                .one_or_none()
            )
            self._validate_session_mutation(stored, csrf_token)
            connection.execute(
                AUTH_SESSIONS.delete().where(AUTH_SESSIONS.c.session_hash == session_hash)
            )

    def _create_session(self, credential_id: str) -> tuple[str, str]:
        session_token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        now = self._now()
        with self._engine.begin() as connection:
            connection.execute(
                AUTH_SESSIONS.insert().values(
                    session_hash=_digest(session_token),
                    csrf_hash=_digest(csrf_token),
                    credential_id=credential_id,
                    created_at=now,
                    last_seen_at=now,
                    recent_reauth_at=now,
                    absolute_expires_at=self._expires(
                        self._settings.auth_session_absolute_ttl_seconds
                    ),
                )
            )
        return session_token, csrf_token

    def _save_challenge(
        self,
        purpose: str,
        challenge: bytes,
        grant_id: str | None,
        options: PublicKeyCredentialCreationOptions | PublicKeyCredentialRequestOptions,
    ) -> dict[str, object]:
        challenge_id = secrets.token_urlsafe(32)
        with self._engine.begin() as connection:
            connection.execute(
                AUTH_CHALLENGES.insert().values(
                    challenge_id=challenge_id,
                    purpose=purpose,
                    challenge=bytes_to_base64url(challenge),
                    grant_id=grant_id,
                    expires_at=self._expires(self._settings.auth_challenge_ttl_seconds),
                )
            )
        return {"challenge_id": challenge_id, "options": json.loads(options_to_json(options))}

    def _consume_challenge(self, challenge_id: str, purpose: str) -> tuple[bytes, str | None]:
        with self._engine.begin() as connection:
            stored = (
                connection.execute(
                    select(AUTH_CHALLENGES).where(AUTH_CHALLENGES.c.challenge_id == challenge_id)
                )
                .mappings()
                .one_or_none()
            )
            if (
                stored is None
                or stored["purpose"] != purpose
                or self._expired(stored["expires_at"])
            ):
                raise AuthenticationError("challenge expired or invalid")
            connection.execute(
                AUTH_CHALLENGES.delete().where(AUTH_CHALLENGES.c.challenge_id == challenge_id)
            )
        return base64url_to_bytes(str(stored["challenge"])), (
            str(stored["grant_id"]) if stored["grant_id"] is not None else None
        )

    def _require_active_grant(self, grant_id: str) -> None:
        with self._engine.connect() as connection:
            grant = (
                connection.execute(
                    select(AUTH_HOST_GRANTS).where(AUTH_HOST_GRANTS.c.grant_id == grant_id)
                )
                .mappings()
                .one_or_none()
            )
        if grant is None or self._expired(grant["expires_at"]):
            raise AuthenticationError("host console grant expired or invalid")

    def _consume_active_grant(self, connection: object, grant_id: str) -> None:
        """Consume the bootstrap or recovery grant only after registration verifies."""
        from sqlalchemy.engine import Connection

        assert isinstance(connection, Connection)
        grant = (
            connection.execute(
                select(AUTH_HOST_GRANTS).where(AUTH_HOST_GRANTS.c.grant_id == grant_id)
            )
            .mappings()
            .one_or_none()
        )
        if grant is None or self._expired(grant["expires_at"]):
            raise AuthenticationError("host console grant expired or invalid")
        connection.execute(AUTH_HOST_GRANTS.delete().where(AUTH_HOST_GRANTS.c.grant_id == grant_id))

    def _authorize_session_mutation(
        self, session_token: str | None, csrf_token: str | None
    ) -> SessionAccess:
        if not session_token or not csrf_token:
            raise AuthenticationError("session and CSRF token are required")
        with self._engine.connect() as connection:
            stored = (
                connection.execute(
                    select(AUTH_SESSIONS).where(
                        AUTH_SESSIONS.c.session_hash == _digest(session_token)
                    )
                )
                .mappings()
                .one_or_none()
            )
        self._validate_session_mutation(stored, csrf_token)
        assert stored is not None
        return SessionAccess(
            credential_id=str(stored["credential_id"]),
            cookie_max_age_seconds=self._cookie_max_age_seconds(stored["absolute_expires_at"]),
        )

    def _validate_session_mutation(
        self, stored: object, csrf_token: str | None
    ) -> None:
        if not isinstance(stored, Mapping) or not csrf_token:
            raise AuthenticationError("session logout was not authorized")
        if (
            self._expired(str(stored["absolute_expires_at"]))
            or self._idle_expired(str(stored["last_seen_at"]))
            or not secrets.compare_digest(str(stored["csrf_hash"]), _digest(csrf_token))
        ):
            raise AuthenticationError("session logout was not authorized")

    def _now(self) -> str:
        return datetime.now(UTC).isoformat()

    def _expires(self, seconds: int) -> str:
        return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()

    def _expired(self, timestamp: str) -> bool:
        return datetime.fromisoformat(timestamp) <= datetime.now(UTC)

    def _idle_expired(self, timestamp: str) -> bool:
        return datetime.fromisoformat(timestamp) + timedelta(
            seconds=self._settings.auth_session_idle_ttl_seconds
        ) <= datetime.now(UTC)

    def _recent_reauthentication_expired(self, timestamp: str) -> bool:
        return datetime.fromisoformat(timestamp) + timedelta(
            seconds=self._settings.auth_recent_reauthentication_ttl_seconds
        ) <= datetime.now(UTC)

    def _cookie_max_age_seconds(self, absolute_expires_at: str) -> int:
        absolute_remaining = int(
            (datetime.fromisoformat(absolute_expires_at) - datetime.now(UTC)).total_seconds()
        )
        return max(0, min(self._settings.auth_session_idle_ttl_seconds, absolute_remaining))


def _digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()
