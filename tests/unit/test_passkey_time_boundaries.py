from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr

from stock_profiler.adapters.authentication.passkeys import (
    AuthenticationError,
    HostGrantPurpose,
    PasskeyAuthenticator,
)
from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage
from stock_profiler.bootstrap.settings import Settings


@dataclass
class MutableClock:
    value: datetime

    def now(self) -> datetime:
        return self.value

    def advance(self, duration: timedelta) -> None:
        self.value += duration


def test_host_grants_and_recent_reauthentication_use_the_injected_clock(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = migrated_settings.model_copy(
        update={
            "auth_bootstrap_token": SecretStr("bootstrap-token-for-synthetic-test"),
            "auth_recovery_token": SecretStr("recovery-token-for-synthetic-test"),
        }
    )
    clock = MutableClock(datetime(2042, 5, 17, 15, 0, tzinfo=UTC))
    authenticator = PasskeyAuthenticator(
        initialize_runtime_storage(settings).engine,
        settings,
        clock=clock,
    )
    grant_id = authenticator.create_host_console_grant(HostGrantPurpose.BOOTSTRAP)

    authenticator.registration_options(grant_id)
    clock.advance(timedelta(minutes=10))
    with pytest.raises(AuthenticationError, match="grant expired"):
        authenticator.registration_options(grant_id)

    monkeypatch.setattr(authenticator, "verify_assertion", lambda *_: "synthetic-credential")
    session_token, _ = authenticator.authentication_verify("unused", {})
    assert authenticator.require_session(session_token).credential_id == "synthetic-credential"

    clock.advance(timedelta(minutes=10))
    with pytest.raises(AuthenticationError, match="session expired"):
        authenticator.require_session(session_token, require_recent_reauthentication=True)

    clock.advance(timedelta(days=7))
    with pytest.raises(AuthenticationError, match="session expired"):
        authenticator.require_session(session_token)
