"""Shared append-only denial storage, independent of result orchestration."""

from sqlalchemy import Column, Integer, MetaData, String, Table
from sqlalchemy.engine import Connection

from stock_profiler.modules.delivery.access import AccessPrincipal, request_digest

ACCESS_AUDIT = Table(
    "result_access_audit",
    MetaData(),
    Column("sequence", Integer, primary_key=True, autoincrement=True),
    Column("request_digest", String(64), nullable=False),
    Column("surface", String(32), nullable=False),
    Column("outcome", String(16), nullable=False),
    Column("reason", String(64), nullable=False),
    Column("recorded_at", String(40), nullable=False),
)


def append_denial(
    connection: Connection,
    target: str,
    principal: AccessPrincipal | None,
    reason: str,
    surface: str,
    recorded_at: str,
) -> None:
    connection.execute(
        ACCESS_AUDIT.insert().values(
            request_digest=request_digest(target, principal),
            surface=surface,
            outcome="DENIED",
            reason=reason,
            recorded_at=recorded_at,
        )
    )
