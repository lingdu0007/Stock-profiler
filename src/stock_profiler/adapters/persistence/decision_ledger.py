"""Application-owned append-only decision events and formal report projections."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.engine import Connection, Engine

from stock_profiler.adapters.persistence.runtime_ownership import (
    DECISION_CASE_BUSINESS_OBJECTS,
    DECISION_EVENTS,
    FORMAL_REPORTS,
    initialize_runtime_storage,
)
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.decision_cases.domain import (
    DecisionEventFact,
    ExternalResult,
    FormalReport,
    FrozenDecisionCase,
)


class DecisionEventCommitError(RuntimeError):
    """A host event was not reliably committed, so no report may be published."""


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

    def ensure_business_object(self, connection: Connection, case: FrozenDecisionCase) -> None:
        """Persist the one host-to-framework mapping before running the framework."""
        existing = connection.execute(
            select(
                DECISION_CASE_BUSINESS_OBJECTS.c.frozen_input_fingerprint,
                DECISION_CASE_BUSINESS_OBJECTS.c.framework_run_id,
            ).where(DECISION_CASE_BUSINESS_OBJECTS.c.business_object_id == case.business_object_id)
        ).one_or_none()
        if existing is None:
            connection.execute(
                DECISION_CASE_BUSINESS_OBJECTS.insert().values(
                    business_object_id=case.business_object_id,
                    case_id=case.case_id,
                    frozen_input_fingerprint=case.frozen_input_fingerprint,
                    framework_run_id=case.framework_run_id,
                    created_at=datetime.now(UTC).isoformat(),
                )
            )
            return
        if (
            existing.frozen_input_fingerprint != case.frozen_input_fingerprint
            or existing.framework_run_id != case.framework_run_id
        ):
            raise DecisionEventCommitError("business identity maps to different frozen input")

    def commit_event_and_report(
        self,
        connection: Connection,
        *,
        case: FrozenDecisionCase,
        framework_run_id: str,
        result: ExternalResult,
    ) -> FormalReport:
        """Atomically append the event and its only formal report projection."""
        fact = DecisionEventFact(
            decision_event_id=case.decision_event_id,
            business_object_id=case.business_object_id,
            framework_run_id=framework_run_id,
            case=case,
            result=result,
            validation_status="PASSED",
            committed_at=case.report_generated_at,
        )
        report = _report_from_fact(case.report_version_id, fact)
        try:
            connection.execute(
                DECISION_EVENTS.insert().values(
                    decision_event_id=case.decision_event_id,
                    business_object_id=case.business_object_id,
                    framework_run_id=framework_run_id,
                    event_payload=fact.model_dump_json(),
                    committed_at=fact.committed_at,
                )
            )
            connection.execute(
                FORMAL_REPORTS.insert().values(
                    report_version_id=report.report_version_id,
                    decision_event_id=report.event_id,
                    generated_at=report.generated_at,
                )
            )
        except Exception as error:
            raise DecisionEventCommitError("decision event commit failed") from error
        return report

    def get_formal_report(
        self, report_version_id: str, connection: Connection | None = None
    ) -> FormalReport | None:
        """Read a report only when the report row references a committed event."""
        if connection is not None:
            return self._formal_report_from_connection(connection, report_version_id)
        with self._engine.connect() as read_connection:
            return self._formal_report_from_connection(read_connection, report_version_id)

    def _formal_report_from_connection(
        self, connection: Connection, report_version_id: str
    ) -> FormalReport | None:
        row = connection.execute(
            select(FORMAL_REPORTS.c.report_version_id, DECISION_EVENTS.c.event_payload)
            .join(
                DECISION_EVENTS,
                FORMAL_REPORTS.c.decision_event_id == DECISION_EVENTS.c.decision_event_id,
            )
            .where(FORMAL_REPORTS.c.report_version_id == report_version_id)
        ).one_or_none()
        if row is None:
            return None
        fact = DecisionEventFact.model_validate_json(row.event_payload)
        return _report_from_fact(str(row.report_version_id), fact)

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


def _report_from_fact(report_version_id: str, fact: DecisionEventFact) -> FormalReport:
    """Build the only report projection from the event's complete host-owned fact."""
    case = fact.case
    return FormalReport(
        report_version_id=report_version_id,
        event_id=fact.decision_event_id,
        business_object_id=fact.business_object_id,
        framework_run_id=fact.framework_run_id,
        case_id=case.case_id,
        synthetic=True,
        qualification_scope=case.qualification_scope,
        generated_at=case.report_generated_at,
        knowledge_cutoff=case.knowledge_cutoff,
        evidence_clock=case.evidence_clock,
        version_bundle=case.version_bundle,
        result=fact.result,
    )
