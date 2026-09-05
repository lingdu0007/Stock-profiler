"""Separate application and M-Agent SQLite ownership at the persistence seam."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from m_agent.adapters import PlaintextPayloadCodec, SQLiteRunStore
from sqlalchemy import Column, MetaData, String, Table, create_engine, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from stock_profiler.bootstrap.settings import Settings

METADATA = MetaData()
APPLICATION_SCHEMA_REVISION = "0002_decision_case_ledger"
HEARTBEAT_MAX_AGE = timedelta(seconds=90)
HeartbeatStatus = Literal["missing", "ready", "stale"]
HEARTBEATS = Table(
    "runtime_heartbeats",
    METADATA,
    Column("process_name", String(32), primary_key=True),
    Column("observed_at", String(40), nullable=False),
)
DECISION_CASE_BUSINESS_OBJECTS = Table(
    "decision_case_business_objects",
    METADATA,
    Column("business_object_id", String(96), primary_key=True),
    Column("case_id", String(96), nullable=False),
    Column("frozen_input_fingerprint", String(64), nullable=False),
    Column("framework_run_id", String(96), nullable=False, unique=True),
    Column("created_at", String(40), nullable=False),
)
DECISION_EVENTS = Table(
    "decision_events",
    METADATA,
    Column("decision_event_id", String(96), primary_key=True),
    Column("business_object_id", String(96), nullable=False, unique=True),
    Column("framework_run_id", String(96), nullable=False, unique=True),
    Column("event_payload", String, nullable=False),
    Column("committed_at", String(40), nullable=False),
)
FORMAL_REPORTS = Table(
    "formal_reports",
    METADATA,
    Column("report_version_id", String(96), primary_key=True),
    Column("decision_event_id", String(96), nullable=False, unique=True),
    Column("report_payload", String, nullable=False),
    Column("generated_at", String(40), nullable=False),
)
AUTH_CREDENTIALS = Table(
    "auth_credentials",
    METADATA,
    Column("credential_id", String(1024), primary_key=True),
    Column("credential_public_key", String, nullable=False),
    Column("sign_count", String(32), nullable=False),
    Column("created_at", String(40), nullable=False),
)
AUTH_CHALLENGES = Table(
    "auth_challenges",
    METADATA,
    Column("challenge_id", String(96), primary_key=True),
    Column("purpose", String(32), nullable=False),
    Column("challenge", String(256), nullable=False),
    Column("grant_id", String(96), nullable=True),
    Column("expires_at", String(40), nullable=False),
)
AUTH_HOST_GRANTS = Table(
    "auth_host_grants",
    METADATA,
    Column("grant_id", String(96), primary_key=True),
    Column("purpose", String(32), nullable=False),
    Column("expires_at", String(40), nullable=False),
)
AUTH_SESSIONS = Table(
    "auth_sessions",
    METADATA,
    Column("session_hash", String(64), primary_key=True),
    Column("csrf_hash", String(64), nullable=False),
    Column("credential_id", String(1024), nullable=False),
    Column("created_at", String(40), nullable=False),
    Column("last_seen_at", String(40), nullable=False),
    Column("recent_reauth_at", String(40), nullable=False),
    Column("absolute_expires_at", String(40), nullable=False),
)


@dataclass(frozen=True)
class RuntimeStorage:
    """The two stores intentionally have no shared tables or transaction."""

    application_database_path: Path
    m_agent_run_store_path: Path
    engine: Engine
    run_store: SQLiteRunStore

    def write_heartbeat(self, process_name: str) -> dict[str, str]:
        """Record a process heartbeat in application-owned storage only."""
        observed_at = datetime.now(UTC).isoformat()
        with self.engine.begin() as connection:
            connection.execute(HEARTBEATS.delete().where(HEARTBEATS.c.process_name == process_name))
            connection.execute(
                HEARTBEATS.insert().values(process_name=process_name, observed_at=observed_at)
            )
        return {"process": process_name, "status": "ready"}

    def heartbeat_status(self, process_name: str) -> HeartbeatStatus:
        """Classify a process heartbeat without creating or altering business state."""
        with self.engine.connect() as connection:
            observed_at = connection.execute(
                select(HEARTBEATS.c.observed_at).where(HEARTBEATS.c.process_name == process_name)
            ).scalar_one_or_none()
        if observed_at is None:
            return "missing"
        try:
            observed = datetime.fromisoformat(observed_at)
        except ValueError:
            return "stale"
        if observed.tzinfo is None:
            return "stale"
        age = datetime.now(UTC) - observed
        if timedelta(0) <= age <= HEARTBEAT_MAX_AGE:
            return "ready"
        return "stale"


def assert_application_schema_current(engine: Engine) -> None:
    """Fail closed unless Alembic owns the application schema at this revision."""
    try:
        with engine.connect() as connection:
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
    except SQLAlchemyError as error:
        raise RuntimeError("application database has not been migrated") from error
    if revision != APPLICATION_SCHEMA_REVISION:
        raise RuntimeError("application database revision is incompatible")


def initialize_runtime_storage(settings: Settings) -> RuntimeStorage:
    """Open independent SQLite identities only after application migration succeeds."""
    application_path = settings.application_database_path
    run_store_path = settings.resolved_m_agent_run_store_path
    application_path.parent.mkdir(parents=True, exist_ok=True)
    run_store_path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(settings.app_database_url)
    assert_application_schema_current(engine)
    run_store = SQLiteRunStore(run_store_path, PlaintextPayloadCodec())
    return RuntimeStorage(
        application_database_path=application_path,
        m_agent_run_store_path=run_store_path,
        engine=engine,
        run_store=run_store,
    )
