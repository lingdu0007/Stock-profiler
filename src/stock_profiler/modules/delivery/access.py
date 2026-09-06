"""Fail-closed authorization for saved results, independent of authentication."""

from __future__ import annotations

from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SINGLE_USER_ID = "stock-profiler-single-user"


class AccessPrincipal(BaseModel):
    """Trusted host grants, never populated from browser-supplied identity fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: str = Field(min_length=1)
    account_ids: tuple[str, ...]
    permissions: tuple[str, ...]


class AccessAuditFact(BaseModel):
    """Non-content audit record; request identities are digests, not payloads."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sequence: int
    request_digest: str
    surface: str
    outcome: Literal["DENIED"]
    reason: str
    recorded_at: str


def read_denial(
    principal: AccessPrincipal | None,
    account_ids: tuple[str, ...],
    *,
    owner_id: str = SINGLE_USER_ID,
    shadow: bool = False,
    permission: str = "REPORT_READ",
) -> str | None:
    if principal is None:
        return "AUTHENTICATION_REQUIRED"
    if principal.user_id != SINGLE_USER_ID or principal.user_id != owner_id:
        return "USER_SCOPE"
    if shadow:
        return "SHADOW_ISOLATED"
    if not account_ids or not set(account_ids).issubset(principal.account_ids):
        return "ACCOUNT_SCOPE"
    if "REPORT_READ" not in principal.permissions or permission not in principal.permissions:
        return "PERMISSION"
    return None


def request_digest(report_id: str, principal: AccessPrincipal | None) -> str:
    identity = principal.model_dump_json() if principal is not None else "unauthenticated"
    return sha256(f"{report_id}\n{identity}".encode()).hexdigest()
