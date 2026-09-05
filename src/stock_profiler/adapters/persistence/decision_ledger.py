"""Application-owned append-only decision events and formal report projections."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from stock_profiler.adapters.persistence.runtime_ownership import (
    DECISION_CASE_BUSINESS_OBJECTS,
    DECISION_EVENTS,
    FORMAL_REPORTS,
    initialize_runtime_storage,
)
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.decision_cases.domain import (
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

    def ensure_business_object(self, case: FrozenDecisionCase) -> None:
        """Persist the one host-to-framework mapping before running the framework."""
        with self._engine.begin() as connection:
            existing = connection.execute(
                select(
                    DECISION_CASE_BUSINESS_OBJECTS.c.frozen_input_fingerprint,
                    DECISION_CASE_BUSINESS_OBJECTS.c.framework_run_id,
                ).where(
                    DECISION_CASE_BUSINESS_OBJECTS.c.business_object_id == case.business_object_id
                )
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
        self, *, case: FrozenDecisionCase, framework_run_id: str, result: ExternalResult
    ) -> FormalReport:
        """Atomically append the event and its only formal report projection."""
        report = FormalReport(
            report_version_id=case.report_version_id,
            event_id=case.decision_event_id,
            business_object_id=case.business_object_id,
            framework_run_id=framework_run_id,
            case_id=case.case_id,
            synthetic=True,
            qualification_scope=case.qualification_scope,
            generated_at=case.evidence_clock.validated_at,
            knowledge_cutoff=case.knowledge_cutoff,
            evidence_clock=case.evidence_clock,
            version_bundle=case.version_bundle,
            result=result,
        )
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    DECISION_EVENTS.insert().values(
                        decision_event_id=case.decision_event_id,
                        business_object_id=case.business_object_id,
                        framework_run_id=framework_run_id,
                        event_payload=json.dumps(
                            {
                                "case_id": case.case_id,
                                "result": result.model_dump(mode="json"),
                                "version_bundle": case.version_bundle.model_dump(mode="json"),
                            },
                            ensure_ascii=True,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        committed_at=datetime.now(UTC).isoformat(),
                    )
                )
                connection.execute(
                    FORMAL_REPORTS.insert().values(
                        report_version_id=report.report_version_id,
                        decision_event_id=report.event_id,
                        report_payload=report.model_dump_json(),
                        generated_at=report.generated_at,
                    )
                )
        except Exception as error:
            raise DecisionEventCommitError("decision event commit failed") from error
        return report

    def get_formal_report(self, report_version_id: str) -> FormalReport | None:
        """Read a report only when the report row references a committed event."""
        with self._engine.connect() as connection:
            payload = connection.execute(
                select(FORMAL_REPORTS.c.report_payload)
                .join(
                    DECISION_EVENTS,
                    FORMAL_REPORTS.c.decision_event_id == DECISION_EVENTS.c.decision_event_id,
                )
                .where(FORMAL_REPORTS.c.report_version_id == report_version_id)
            ).scalar_one_or_none()
        return FormalReport.model_validate_json(payload) if payload is not None else None

    def counts(self) -> dict[str, int]:
        """Expose only test-facing cardinalities for this D0 seam."""
        with self._engine.connect() as connection:
            return {
                "business_objects": int(
                    connection.execute(select(func.count()).select_from(DECISION_CASE_BUSINESS_OBJECTS)).scalar_one()
                ),
                "decision_events": int(
                    connection.execute(select(func.count()).select_from(DECISION_EVENTS)).scalar_one()
                ),
                "reports": int(
                    connection.execute(select(func.count()).select_from(FORMAL_REPORTS)).scalar_one()
                ),
            }
