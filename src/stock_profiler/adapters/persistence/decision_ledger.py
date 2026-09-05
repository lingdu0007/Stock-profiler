"""Application-owned append-only decision events and formal report projections."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import cast

from sqlalchemy import Column, Integer, MetaData, String, Table, func, select
from sqlalchemy.engine import Connection, Engine

from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.decision_cases.domain import (
    DecisionEventFact,
    ExternalResult,
    FormalReport,
    FrozenDecisionCase,
    NotificationAttempt,
    NotificationAttemptStatus,
    StageResult,
)

METADATA = MetaData()
DECISION_CASE_BUSINESS_OBJECTS = Table(
    "decision_case_business_objects",
    METADATA,
    Column("business_object_id", String(96), primary_key=True),
    Column("case_id", String(96), nullable=False),
    Column("frozen_input_fingerprint", String(64), nullable=False),
    Column("framework_run_id", String(96), nullable=False, unique=True),
    Column("case_payload", String, nullable=True),
    Column("created_at", String(40), nullable=False),
)
DECISION_EVENTS = Table(
    "decision_events",
    METADATA,
    Column("decision_event_id", String(96), primary_key=True),
    Column("business_object_id", String(96), nullable=False),
    Column("framework_run_id", String(96), nullable=False),
    Column("corrects_event_id", String(96), nullable=True),
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
DECISION_STAGE_EVENTS = Table(
    "decision_stage_events",
    METADATA,
    Column("sequence", Integer, primary_key=True, autoincrement=True),
    Column("stage_event_id", String(96), nullable=False, unique=True),
    Column("business_object_id", String(96), nullable=False),
    Column("framework_run_id", String(96), nullable=False),
    Column("decision_event_id", String(96), nullable=True),
    Column("stage_payload", String, nullable=False),
    Column("recorded_at", String(40), nullable=False),
)
DECISION_NOTIFICATION_ATTEMPTS = Table(
    "decision_notification_attempts",
    METADATA,
    Column("sequence", Integer, primary_key=True, autoincrement=True),
    Column("notification_attempt_id", String(96), nullable=False, unique=True),
    Column("report_version_id", String(96), nullable=False),
    Column("decision_event_id", String(96), nullable=False),
    Column("status", String(16), nullable=False),
    Column("reasons_payload", String, nullable=False),
    Column("recorded_at", String(40), nullable=False),
)


class DecisionEventCommitError(RuntimeError):
    """A host event was not reliably committed, so no report may be published."""


class DecisionEventCommitUncertainError(DecisionEventCommitError):
    """A database acknowledgement was lost, so the event may or may not exist."""


@dataclass(frozen=True)
class BusinessObjectMapping:
    """The durable case snapshot and original Run bound to one business object."""

    case_id: str
    frozen_input_fingerprint: str
    framework_run_id: str
    case: FrozenDecisionCase | None


class DecisionLedger:
    """Persistence boundary for the host's business identity, event, and report."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @classmethod
    def from_settings(cls, settings: Settings) -> DecisionLedger:
        return cls(initialize_runtime_storage(settings).engine)

    @contextmanager
    def serialize_case_execution(self) -> Iterator[Connection]:
        """Serialize one D0 identity from lookup through event/report publication."""
        connection = self._engine.connect()
        try:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            yield connection
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    def get_business_object_mapping(
        self, business_object_id: str, connection: Connection
    ) -> BusinessObjectMapping | None:
        """Load a saved case snapshot before deriving any current-build Run identity."""
        row = connection.execute(
            select(
                DECISION_CASE_BUSINESS_OBJECTS.c.case_id,
                DECISION_CASE_BUSINESS_OBJECTS.c.frozen_input_fingerprint,
                DECISION_CASE_BUSINESS_OBJECTS.c.framework_run_id,
                DECISION_CASE_BUSINESS_OBJECTS.c.case_payload,
            ).where(DECISION_CASE_BUSINESS_OBJECTS.c.business_object_id == business_object_id)
        ).one_or_none()
        if row is None:
            return None
        return BusinessObjectMapping(
            case_id=row.case_id,
            frozen_input_fingerprint=row.frozen_input_fingerprint,
            framework_run_id=row.framework_run_id,
            case=(
                FrozenDecisionCase.model_validate_json(row.case_payload)
                if row.case_payload is not None
                else None
            ),
        )

    def ensure_business_object(self, connection: Connection, case: FrozenDecisionCase) -> None:
        """Persist the one host-to-framework mapping before running the framework."""
        mapping = self.get_business_object_mapping(case.business_object_id, connection)
        if mapping is None:
            connection.execute(
                DECISION_CASE_BUSINESS_OBJECTS.insert().values(
                    business_object_id=case.business_object_id,
                    case_id=case.case_id,
                    frozen_input_fingerprint=case.frozen_input_fingerprint,
                    framework_run_id=case.framework_run_id,
                    case_payload=case.model_dump_json(),
                    created_at=datetime.now(UTC).isoformat(),
                )
            )
            return
        if mapping.case is not None:
            if (
                mapping.frozen_input_fingerprint != mapping.case.frozen_input_fingerprint
                or mapping.framework_run_id != mapping.case.framework_run_id
            ):
                raise DecisionEventCommitError("business identity maps to different frozen input")
            if mapping.case.matches_recovery_input(
                case
            ) or mapping.case.matches_legacy_recovery_input(case):
                return
            raise DecisionEventCommitError("business identity maps to different frozen input")
        if (
            mapping.case is None
            and mapping.case_id == case.case_id
            and case.matches_legacy_recovery_input()
        ):
            return
        raise DecisionEventCommitError("business identity maps to different frozen input")

    def persist_business_mapping_before_framework(self, case: FrozenDecisionCase) -> None:
        """Commit the original mapping before a separate M-Agent store can checkpoint work."""
        with self.serialize_case_execution() as connection:
            if self.get_original_decision_event(case.business_object_id, connection) is None:
                self.ensure_business_object(connection, case)

    def get_decision_event(
        self, decision_event_id: str, connection: Connection
    ) -> DecisionEventFact | None:
        """Read the committed host fact before attempting a report projection."""
        payload = connection.execute(
            select(DECISION_EVENTS.c.event_payload).where(
                DECISION_EVENTS.c.decision_event_id == decision_event_id
            )
        ).scalar_one_or_none()
        return DecisionEventFact.model_validate_json(payload) if payload is not None else None

    def get_original_decision_event(
        self, business_object_id: str, connection: Connection
    ) -> DecisionEventFact | None:
        """Read the original fact by stable business identity across host builds."""
        payload = connection.execute(
            select(DECISION_EVENTS.c.event_payload)
            .where(
                DECISION_EVENTS.c.business_object_id == business_object_id,
                DECISION_EVENTS.c.corrects_event_id.is_(None),
            )
            .order_by(DECISION_EVENTS.c.committed_at)
        ).scalar_one_or_none()
        return DecisionEventFact.model_validate_json(payload) if payload is not None else None

    def get_correction_event(
        self, original_event_id: str, connection: Connection
    ) -> DecisionEventFact | None:
        """Read the sole D0 correction that already references one original fact."""
        payload = connection.execute(
            select(DECISION_EVENTS.c.event_payload).where(
                DECISION_EVENTS.c.corrects_event_id == original_event_id
            )
        ).scalar_one_or_none()
        return DecisionEventFact.model_validate_json(payload) if payload is not None else None

    def record_stage_result(
        self,
        connection: Connection,
        *,
        case: FrozenDecisionCase,
        stage_result: StageResult,
        decision_event_id: str | None = None,
        stage_event_id: str | None = None,
        framework_run_id: str | None = None,
        allow_repeated_occurrence: bool = False,
    ) -> None:
        """Append a phase outcome without replacing an earlier result family."""
        durable_framework_run_id = framework_run_id or case.framework_run_id
        stage_payload = _canonical_json(stage_result.model_dump(mode="json"))
        if stage_event_id is not None:
            self._insert_or_validate_stage_result(
                connection,
                stage_event_id=stage_event_id,
                case=case,
                framework_run_id=durable_framework_run_id,
                decision_event_id=decision_event_id,
                stage_payload=stage_payload,
                stage_result=stage_result,
            )
            return
        occurrence_count = int(
            connection.execute(
                select(func.count())
                .select_from(DECISION_STAGE_EVENTS)
                .where(
                    DECISION_STAGE_EVENTS.c.business_object_id == case.business_object_id,
                    DECISION_STAGE_EVENTS.c.framework_run_id == durable_framework_run_id,
                    DECISION_STAGE_EVENTS.c.decision_event_id == decision_event_id,
                    DECISION_STAGE_EVENTS.c.stage_payload == stage_payload,
                )
            ).scalar_one()
        )
        if occurrence_count and not allow_repeated_occurrence:
            return
        self._insert_or_validate_stage_result(
            connection,
            stage_event_id=_stage_event_id(
                case,
                stage_result,
                decision_event_id,
                framework_run_id=durable_framework_run_id,
                occurrence=occurrence_count + 1 if allow_repeated_occurrence else None,
            ),
            case=case,
            framework_run_id=durable_framework_run_id,
            decision_event_id=decision_event_id,
            stage_payload=stage_payload,
            stage_result=stage_result,
        )

    def _insert_or_validate_stage_result(
        self,
        connection: Connection,
        *,
        stage_event_id: str,
        case: FrozenDecisionCase,
        framework_run_id: str,
        decision_event_id: str | None,
        stage_payload: str,
        stage_result: StageResult,
    ) -> None:
        existing = connection.execute(
            select(DECISION_STAGE_EVENTS.c.stage_payload).where(
                DECISION_STAGE_EVENTS.c.stage_event_id == stage_event_id
            )
        ).scalar_one_or_none()
        if existing is None:
            connection.execute(
                DECISION_STAGE_EVENTS.insert().values(
                    stage_event_id=stage_event_id,
                    business_object_id=case.business_object_id,
                    framework_run_id=framework_run_id,
                    decision_event_id=decision_event_id,
                    stage_payload=stage_payload,
                    recorded_at=datetime.now(UTC).isoformat(),
                )
            )
            return
        if StageResult.model_validate_json(existing) != stage_result:
            raise DecisionEventCommitError("stage event identity maps to different results")

    def get_stage_results(
        self, business_object_id: str, connection: Connection | None = None
    ) -> tuple[StageResult, ...]:
        """Read the append-only stage history in the order it was recorded."""
        statement = (
            select(DECISION_STAGE_EVENTS.c.stage_payload)
            .where(DECISION_STAGE_EVENTS.c.business_object_id == business_object_id)
            .order_by(DECISION_STAGE_EVENTS.c.sequence)
        )
        if connection is not None:
            payloads = connection.execute(statement).scalars().all()
        else:
            with self._engine.connect() as read_connection:
                payloads = read_connection.execute(statement).scalars().all()
        return tuple(StageResult.model_validate_json(payload) for payload in payloads)

    def record_notification_attempt(
        self,
        connection: Connection,
        *,
        report: FormalReport,
        status: NotificationAttemptStatus,
        reasons: tuple[str, ...],
    ) -> NotificationAttempt:
        """Append one notification attempt without mutating its source report."""
        attempt_number = (
            int(
                connection.execute(
                    select(func.count())
                    .select_from(DECISION_NOTIFICATION_ATTEMPTS)
                    .where(
                        DECISION_NOTIFICATION_ATTEMPTS.c.report_version_id
                        == report.report_version_id
                    )
                ).scalar_one()
            )
            + 1
        )
        attempt = NotificationAttempt(
            notification_attempt_id=_notification_attempt_id(report, attempt_number),
            report_version_id=report.report_version_id,
            event_id=report.event_id,
            status=status,
            reasons=reasons,
        )
        connection.execute(
            DECISION_NOTIFICATION_ATTEMPTS.insert().values(
                notification_attempt_id=attempt.notification_attempt_id,
                report_version_id=attempt.report_version_id,
                decision_event_id=attempt.event_id,
                status=attempt.status,
                reasons_payload=json.dumps(
                    attempt.reasons,
                    ensure_ascii=True,
                    separators=(",", ":"),
                ),
                recorded_at=datetime.now(UTC).isoformat(),
            )
        )
        return attempt

    def get_notification_attempts(
        self, report_version_id: str, connection: Connection | None = None
    ) -> tuple[NotificationAttempt, ...]:
        """Read notification history without deriving or changing report content."""
        statement = (
            select(
                DECISION_NOTIFICATION_ATTEMPTS.c.notification_attempt_id,
                DECISION_NOTIFICATION_ATTEMPTS.c.report_version_id,
                DECISION_NOTIFICATION_ATTEMPTS.c.decision_event_id,
                DECISION_NOTIFICATION_ATTEMPTS.c.status,
                DECISION_NOTIFICATION_ATTEMPTS.c.reasons_payload,
            )
            .where(DECISION_NOTIFICATION_ATTEMPTS.c.report_version_id == report_version_id)
            .order_by(DECISION_NOTIFICATION_ATTEMPTS.c.sequence)
        )
        if connection is not None:
            rows = connection.execute(statement).all()
        else:
            with self._engine.connect() as read_connection:
                rows = read_connection.execute(statement).all()
        return tuple(
            NotificationAttempt(
                notification_attempt_id=row.notification_attempt_id,
                report_version_id=row.report_version_id,
                event_id=row.decision_event_id,
                status=cast(NotificationAttemptStatus, row.status),
                reasons=tuple(json.loads(row.reasons_payload)),
            )
            for row in rows
        )

    def commit_event(
        self,
        connection: Connection,
        *,
        case: FrozenDecisionCase,
        framework_run_id: str,
        result: ExternalResult,
        stage_results: tuple[StageResult, ...],
        decision_event_id: str | None = None,
        corrects_event_id: str | None = None,
        committed_at: str | None = None,
        generated_at: str | None = None,
    ) -> DecisionEventFact:
        """Reliably append the host event before any report projection is made."""
        fact = self.build_event_fact(
            case=case,
            framework_run_id=framework_run_id,
            result=result,
            stage_results=stage_results,
            decision_event_id=decision_event_id,
            corrects_event_id=corrects_event_id,
            committed_at=committed_at,
            generated_at=generated_at,
        )
        existing = self.get_decision_event(fact.decision_event_id, connection)
        if existing is not None:
            if existing != fact:
                raise DecisionEventCommitError("decision event identity maps to different facts")
            return existing
        try:
            connection.execute(
                DECISION_EVENTS.insert().values(
                    decision_event_id=fact.decision_event_id,
                    business_object_id=case.business_object_id,
                    framework_run_id=framework_run_id,
                    corrects_event_id=corrects_event_id,
                    event_payload=fact.model_dump_json(),
                    committed_at=fact.committed_at,
                )
            )
        except Exception as error:
            raise DecisionEventCommitError("decision event write failed") from error
        try:
            connection.commit()
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        except Exception as error:
            connection.rollback()
            raise DecisionEventCommitUncertainError(
                "decision event commit acknowledgement is uncertain"
            ) from error
        return fact

    def build_event_fact(
        self,
        *,
        case: FrozenDecisionCase,
        framework_run_id: str,
        result: ExternalResult,
        stage_results: tuple[StageResult, ...],
        decision_event_id: str | None = None,
        corrects_event_id: str | None = None,
        committed_at: str | None = None,
        generated_at: str | None = None,
    ) -> DecisionEventFact:
        """Construct the exact append-only fact that a commit attempt must preserve."""
        event_id = decision_event_id or case.decision_event_id
        return DecisionEventFact(
            decision_event_id=event_id,
            business_object_id=case.business_object_id,
            framework_run_id=framework_run_id,
            case=case,
            result=result,
            validation_status="PASSED",
            committed_at=committed_at or case.report_generated_at,
            stage_results=stage_results,
            corrects_event_id=corrects_event_id,
            generated_at=generated_at or case.report_generated_at,
        )

    def reconcile_event_commit(
        self, connection: Connection, attempted: DecisionEventFact
    ) -> DecisionEventFact | None:
        """Verify an acknowledged-uncertain event on a fresh read connection."""
        connection.rollback()
        with self._engine.connect() as verification_connection:
            committed = self.get_decision_event(
                attempted.decision_event_id,
                verification_connection,
            )
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        if committed is not None and committed != attempted:
            raise DecisionEventCommitError("committed event does not match attempted fact")
        return committed

    def ensure_event_stage_results(self, connection: Connection, fact: DecisionEventFact) -> None:
        """Backfill stage rows from an already committed append-only event."""
        latest_business_commit_index = max(
            (
                index
                for index, stage_result in enumerate(fact.stage_results)
                if stage_result.phase == "BUSINESS_COMMIT"
            ),
            default=None,
        )
        for index, stage_result in enumerate(fact.stage_results):
            decision_event_id = (
                fact.decision_event_id
                if (stage_result.phase == "CORRECTION" or index == latest_business_commit_index)
                else None
            )
            self.record_stage_result(
                connection,
                case=fact.case,
                stage_result=stage_result,
                decision_event_id=decision_event_id,
                framework_run_id=fact.framework_run_id,
            )

    def discard_unconfirmed_publication(self, connection: Connection) -> None:
        """Roll back a report whose durable acknowledgement was not received."""
        connection.rollback()
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    def publish_report(
        self,
        connection: Connection,
        fact: DecisionEventFact,
        report_version_id: str | None = None,
    ) -> FormalReport:
        """Create a report only after its source business event is durably committed."""
        resolved_report_version_id = report_version_id or fact.case.report_version_id_for_event(
            fact.decision_event_id
        )
        existing = self.get_formal_report(resolved_report_version_id, connection)
        if existing is not None:
            if existing != fact.formal_report(resolved_report_version_id):
                raise DecisionEventCommitError("report identity maps to different projection")
            return existing
        report = fact.formal_report(resolved_report_version_id)
        try:
            connection.execute(
                FORMAL_REPORTS.insert().values(
                    report_version_id=report.report_version_id,
                    decision_event_id=report.event_id,
                    report_payload=_canonical_json(
                        fact.formal_report_payload(resolved_report_version_id)
                    ),
                    generated_at=report.generated_at,
                )
            )
        except Exception as error:
            raise DecisionEventCommitError("formal report publication failed") from error
        return report

    def get_formal_report(
        self, report_version_id: str, connection: Connection | None = None
    ) -> FormalReport | None:
        """Read a report only when the report row references a committed event."""
        if connection is not None:
            return self._formal_report_from_connection(connection, report_version_id)
        with self._engine.connect() as read_connection:
            return self._formal_report_from_connection(read_connection, report_version_id)

    def get_original_formal_report(
        self, business_object_id: str, connection: Connection
    ) -> FormalReport | None:
        """Read the original published report by its stable business identity."""
        row = connection.execute(
            select(FORMAL_REPORTS.c.report_payload)
            .join(
                DECISION_EVENTS,
                FORMAL_REPORTS.c.decision_event_id == DECISION_EVENTS.c.decision_event_id,
            )
            .where(
                DECISION_EVENTS.c.business_object_id == business_object_id,
                DECISION_EVENTS.c.corrects_event_id.is_(None),
            )
            .order_by(FORMAL_REPORTS.c.generated_at)
        ).one_or_none()
        if row is None:
            return None
        return FormalReport.model_validate_json(row.report_payload)

    def get_formal_report_for_event(
        self, decision_event_id: str, connection: Connection
    ) -> FormalReport | None:
        """Read one existing projection without deriving a new report identity."""
        row = connection.execute(
            select(FORMAL_REPORTS.c.report_payload)
            .join(
                DECISION_EVENTS,
                FORMAL_REPORTS.c.decision_event_id == DECISION_EVENTS.c.decision_event_id,
            )
            .where(FORMAL_REPORTS.c.decision_event_id == decision_event_id)
        ).one_or_none()
        if row is None:
            return None
        return FormalReport.model_validate_json(row.report_payload)

    def _formal_report_from_connection(
        self, connection: Connection, report_version_id: str
    ) -> FormalReport | None:
        row = connection.execute(
            select(FORMAL_REPORTS.c.report_payload)
            .join(
                DECISION_EVENTS,
                FORMAL_REPORTS.c.decision_event_id == DECISION_EVENTS.c.decision_event_id,
            )
            .where(FORMAL_REPORTS.c.report_version_id == report_version_id)
        ).one_or_none()
        if row is None:
            return None
        return FormalReport.model_validate_json(row.report_payload)

    def counts(self) -> dict[str, int]:
        """Expose only test-facing cardinalities for this D0 seam."""
        with self._engine.connect() as connection:
            return {
                "business_objects": int(
                    connection.execute(
                        select(func.count()).select_from(DECISION_CASE_BUSINESS_OBJECTS)
                    ).scalar_one()
                ),
                "decision_events": int(
                    connection.execute(
                        select(func.count()).select_from(DECISION_EVENTS)
                    ).scalar_one()
                ),
                "reports": int(
                    connection.execute(
                        select(func.count()).select_from(FORMAL_REPORTS)
                    ).scalar_one()
                ),
            }


def _stage_event_id(
    case: FrozenDecisionCase,
    stage_result: StageResult,
    decision_event_id: str | None,
    *,
    framework_run_id: str,
    occurrence: int | None = None,
) -> str:
    payload = {
        "business_object_id": case.business_object_id,
        "framework_run_id": framework_run_id,
        "decision_event_id": decision_event_id,
        "stage_result": stage_result.model_dump(mode="json"),
        "occurrence": occurrence,
    }
    serialized = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return f"decision-stage-{sha256(serialized.encode()).hexdigest()}"


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _notification_attempt_id(report: FormalReport, attempt_number: int) -> str:
    payload = {
        "report_version_id": report.report_version_id,
        "event_id": report.event_id,
        "attempt_number": attempt_number,
    }
    serialized = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return f"notification-attempt-{sha256(serialized.encode()).hexdigest()}"
