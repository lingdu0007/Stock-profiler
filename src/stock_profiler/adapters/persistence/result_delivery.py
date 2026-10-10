"""Authorized result projection and durable denial auditing at the storage boundary."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.engine import Connection, Engine

from stock_profiler.adapters.persistence.access_audit import ACCESS_AUDIT as ACCESS_AUDIT
from stock_profiler.adapters.persistence.access_audit import append_denial
from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage
from stock_profiler.adapters.persistence.user_fact_storage import USER_FACTS as USER_FACTS
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.foundation.clock import Clock
from stock_profiler.modules.decision_cases.domain import (
    FormalReport,
    FrozenDecisionCase,
    StageResult,
)
from stock_profiler.modules.decision_cases.monitoring_confirmation import confirmation_permitted
from stock_profiler.modules.delivery.access import (
    SINGLE_USER_ID,
    AccessAuditFact,
    AccessPrincipal,
    read_denial,
)
from stock_profiler.modules.delivery.allocation_workspace import (
    AllocationWorkspaceSource,
    project_allocations,
)
from stock_profiler.modules.delivery.candidate_notifications import prepare_candidate_reminder
from stock_profiler.modules.delivery.candidate_reminder_contracts import (
    CandidateReminderRecord,
    CandidateReminderRequest,
)
from stock_profiler.modules.delivery.candidate_workspace import (
    CandidateWorkspace,
    CandidateWorkspaceSource,
    project_candidate_workspace,
)
from stock_profiler.modules.delivery.monitoring_workspace import (
    MonitoringWorkspace,
    project_workspace,
)
from stock_profiler.modules.delivery.user_facts import UserFact, UserFactRequest
from stock_profiler.modules.qualification.beta import envelope_decision, qualification_reasons


class ResultDelivery:
    def __init__(self, engine: Engine, *, clock: Clock | None = None) -> None:
        self._engine = engine
        self._ledger = DecisionLedger(engine, clock=clock)

    @classmethod
    def from_settings(cls, settings: Settings, *, clock: Clock | None = None) -> ResultDelivery:
        return cls(initialize_runtime_storage(settings).engine, clock=clock)

    def read_report(
        self, report_id: str, principal: AccessPrincipal | None = None
    ) -> FormalReport | None:
        with self._engine.begin() as connection:
            return self._read_report(connection, report_id, principal)

    def saved_command_case(self, identity: str) -> FrozenDecisionCase | None:
        return self._ledger.get_frozen_case_by_identity(identity)

    def report_input_case(self, event_id: str) -> FrozenDecisionCase | None:
        with self._engine.begin() as connection:
            event = self._ledger.get_decision_event(event_id, connection)
            return event.case if event is not None else None

    def observed_at(self) -> str:
        return self._ledger.observed_at()

    def monitoring_workspace(self, principal: AccessPrincipal | None) -> MonitoringWorkspace | None:
        with self._engine.begin() as connection:
            reason = read_denial(principal, principal.account_ids if principal is not None else ())
            if reason is not None:
                self._deny(connection, "monitoring", principal, reason, "REPORT_READ")
                return None
            reports = tuple(
                report
                for identity in self._ledger.monitoring_report_ids(connection)
                if (report := self._read_report(connection, identity, principal)) is not None
            )
            ids = tuple(report.report_version_id for report in reports)
            facts = tuple(
                UserFact.model_validate_json(payload)
                for payload in connection.execute(
                    select(USER_FACTS.c.fact_payload)
                    .where(USER_FACTS.c.report_version_id.in_(ids))
                    .order_by(USER_FACTS.c.sequence)
                ).scalars()
            )
            return project_workspace(reports, facts)

    def candidate_workspace(self, principal: AccessPrincipal | None) -> CandidateWorkspace | None:
        with self._engine.begin() as connection:
            reason = read_denial(principal, principal.account_ids if principal is not None else ())
            if reason is not None:
                self._deny(connection, "candidates", principal, reason, "REPORT_READ")
                return None
            workspace = project_candidate_workspace(
                self._candidate_sources(connection, principal),
                datetime.fromisoformat(self._ledger.observed_at()),
            )
            reports = (
                tuple(
                    report
                    for identity in self._ledger.candidate_allocation_report_ids(connection)
                    if (
                        report := self._read_report(
                            connection, identity, principal, "CANDIDATE_ALLOCATION"
                        )
                    )
                    is not None
                )
                if principal is not None and "CANDIDATE_ALLOCATION" in principal.permissions
                else ()
            )
            sources = tuple(
                AllocationWorkspaceSource(report=report, case=event.case)
                for report in reports
                if (event := self._ledger.get_decision_event(report.event_id, connection))
                is not None
            )
            plan_stops = {}
            for source in sources:
                command = source.case.candidate_allocation
                scope = source.case.access_scope
                if command is None or command.beta is None or scope is None:
                    continue
                stops = qualification_reasons(
                    command.beta,
                    self._ledger.governance_history(connection, scope),
                    workspace.observed_at,
                    evidence_cutoff=command.cutoff_at,
                )
                envelope, observation_reasons, _, enabled = envelope_decision(
                    command.beta,
                    workspace.observed_at,
                    self._ledger.governance_history(connection, scope),
                    evidence_cutoff=command.cutoff_at,
                )
                saved = source.report.result.candidate_allocation
                if (
                    not enabled
                    or saved is None
                    or saved.beta is None
                    or saved.beta.permission_envelope != envelope
                ):
                    stops += observation_reasons or ("BETA_PERMISSION_CHANGED",)
                plan_stops[source.report.event_id] = stops
            return workspace.model_copy(
                update={
                    "allocations": project_allocations(
                        sources,
                        workspace.observed_at,
                        {
                            item.event_id: item.status_reasons if item.status != "CURRENT" else ()
                            for item in workspace.releases
                        },
                        plan_stops,
                    ),
                    "commands_permitted": principal is not None
                    and "CANDIDATE_COMMAND" in principal.permissions,
                }
            )

    def _candidate_sources(
        self, connection: Connection, principal: AccessPrincipal | None
    ) -> tuple[CandidateWorkspaceSource, ...]:
        sources = []
        for identity in self._ledger.candidate_report_ids(connection):
            report = self._read_report(connection, identity, principal)
            if report is None or report.result.candidate_release is None:
                continue
            event = self._ledger.get_decision_event(report.event_id, connection)
            if event is None or event.case.access_scope is None:
                continue
            correction = self._ledger.get_correction_event(
                report.result.candidate_release.research_event_id, connection
            )
            research = self._ledger.get_decision_event(
                report.result.candidate_release.research_event_id, connection
            )
            research_report = (
                self._ledger.get_formal_report_for_event(research.decision_event_id, connection)
                if research is not None
                else None
            )
            frozen_research = research.case.research if research is not None else None
            research_outcome = research_report.result.research if research_report else None
            reminders = tuple(
                stage.candidate_reminder
                for stage in self._ledger.get_stage_results(report.business_object_id, connection)
                if stage.candidate_reminder is not None
                and stage.candidate_reminder.report_version_id == report.report_version_id
            )
            sources.append(
                CandidateWorkspaceSource(
                    report=report,
                    case=event.case,
                    qualification_history=self._ledger.governance_history(
                        connection, event.case.access_scope
                    ),
                    research_correction_event_id=correction.decision_event_id
                    if correction
                    else None,
                    research_correction_committed_at=datetime.fromisoformat(correction.committed_at)
                    if correction
                    else None,
                    reminders=reminders,
                    frozen_pool_count=len(frozen_research.screening.selected_member_ids)
                    if frozen_research is not None
                    else None,
                    research_completed_count=len(research_outcome.members)
                    if research_outcome is not None
                    else None,
                )
            )
        return tuple(sources)

    def candidate_reminder_history(
        self,
        report_id: str,
        principal: AccessPrincipal | None,
    ) -> tuple[CandidateReminderRecord, ...]:
        with self._engine.begin() as connection:
            report = self._read_report(connection, report_id, principal)
            if report is None or report.result.candidate_release is None:
                return ()
            return tuple(
                stage.candidate_reminder
                for stage in self._ledger.get_stage_results(report.business_object_id, connection)
                if stage.candidate_reminder is not None
                and stage.candidate_reminder.report_version_id == report_id
            )

    def candidate_reminder(
        self,
        report_id: str,
        principal: AccessPrincipal | None,
        request: CandidateReminderRequest,
    ) -> CandidateReminderRecord | None:
        """Host-only D0 routing observation; no HTTP callback or external provider."""
        with self._ledger.serialize_case_execution() as connection:
            try:
                request = CandidateReminderRequest.model_validate(request.model_dump(mode="python"))
            except ValidationError:
                self._deny(
                    connection, report_id, principal, "INVALID_CANDIDATE_REMINDER", "NOTIFICATION"
                )
                return None
            report = self._read_report(connection, report_id, principal)
            if report is None or report.result.candidate_release is None:
                return None
            event = self._ledger.get_decision_event(report.event_id, connection)
            if (
                event is None
                or event.case.access_scope is None
                or not report.synthetic
                or report.qualification_scope != "D0_SYNTHETIC_CONTRACT_ONLY"
            ):
                return None
            sources = self._candidate_sources(connection, principal)
            history = self._candidate_budget_history(connection, event.case.access_scope.user_id)
            now = datetime.fromisoformat(self._ledger.observed_at())
            workspace = project_candidate_workspace(tuple(sources), now)
            view = next(
                (item for item in workspace.releases if item.report_version_id == report_id), None
            )
            if (
                view is None
                or report.report_publication is None
                or report.report_publication.status != "PUBLISHED"
            ):
                self._deny(
                    connection,
                    report_id,
                    principal,
                    "CANDIDATE_PUBLICATION_REQUIRED",
                    "NOTIFICATION",
                )
                return None
            reminder = prepare_candidate_reminder(
                view,
                request,
                now,
                event.case.access_scope.user_id,
                tuple(history),
            )
            if reminder is None:
                self._deny(
                    connection,
                    report_id,
                    principal,
                    "CANDIDATE_REMINDER_INELIGIBLE",
                    "NOTIFICATION",
                )
                return None
            self._ledger.record_stage_result(
                connection,
                case=event.case,
                framework_run_id=event.framework_run_id,
                decision_event_id=event.decision_event_id,
                stage_event_id=reminder.attempt_id,
                stage_result=StageResult(
                    phase="NOTIFICATION",
                    status="SUCCEEDED" if reminder.path_completed else "FAILED",
                    gate_results=(),
                    reasons=(reminder.status,),
                    candidate_reminder=reminder,
                ),
                recorded_at=reminder.recorded_at.isoformat(),
            )
            return reminder

    def _candidate_budget_history(
        self, connection: Connection, owner_id: str
    ) -> tuple[CandidateReminderRecord, ...]:
        """Owner-level budget inspection, never exposed as an account read capability."""
        history: dict[str, CandidateReminderRecord] = {}
        for identity in self._ledger.candidate_report_ids(connection):
            report = self._ledger.get_formal_report(identity, connection)
            assert report is not None
            scope = report.access_scope
            if scope is None or scope.user_id != owner_id or scope.visibility != "USER":
                continue
            for stage in self._ledger.get_stage_results(report.business_object_id, connection):
                reminder = stage.candidate_reminder
                if reminder is not None:
                    history[reminder.attempt_id] = reminder
        return tuple(history.values())

    def _read_report(
        self,
        connection: Connection,
        report_id: str,
        principal: AccessPrincipal | None,
        permission: str = "REPORT_READ",
    ) -> FormalReport | None:
        report = self._ledger.get_formal_report(report_id, connection)
        event = (
            self._ledger.get_decision_event(report.event_id, connection)
            if report is not None
            else None
        )
        account = event.case.input.get("account") if event is not None else None
        account_id = account.get("account_id") if isinstance(account, dict) else None
        accounts = (account_id,) if isinstance(account_id, str) else ()
        scope = event.case.access_scope if event is not None else None
        if report is not None and any(
            (
                report.result.candidate_allocation,
                report.result.candidate_confirmation,
                report.result.candidate_execution,
            )
        ):
            allocation_denial = read_denial(
                principal,
                scope.account_ids if scope is not None else accounts,
                owner_id=scope.user_id if scope is not None else SINGLE_USER_ID,
                shadow=scope is not None and scope.visibility == "SHADOW",
                permission="CANDIDATE_ALLOCATION",
            )
            if allocation_denial is not None:
                self._deny(
                    connection, report_id, principal, allocation_denial, "CANDIDATE_ALLOCATION"
                )
                return None
        reason = read_denial(
            principal,
            scope.account_ids if scope is not None else accounts,
            owner_id=scope.user_id if scope is not None else SINGLE_USER_ID,
            shadow=scope is not None and scope.visibility == "SHADOW",
            permission=permission,
        )
        if reason is None and report is None:
            reason = "UNAVAILABLE"
        if reason is not None:
            self._deny(connection, report_id, principal, reason, permission)
            return None
        return report

    def _deny(
        self,
        connection: Connection,
        target: str,
        principal: AccessPrincipal | None,
        reason: str,
        surface: str,
    ) -> None:
        append_denial(connection, target, principal, reason, surface, self._ledger.observed_at())

    def record_user_fact(
        self,
        report_id: str,
        principal: AccessPrincipal | None,
        request: UserFactRequest,
    ) -> UserFact | None:
        """Append once; never convert a user declaration into an execution fact."""
        with self._ledger.serialize_case_execution() as connection:
            try:
                request = UserFactRequest.model_validate(request.model_dump(mode="python"))
            except ValidationError:
                self._deny(connection, report_id, principal, "INVALID_USER_FACT", "USER_FACT")
                return None
            report = self._read_report(connection, report_id, principal, "USER_FACT")
            if report is None or principal is None:
                return None
            if (
                any(
                    (
                        report.result.candidate_release,
                        report.result.candidate_allocation,
                        report.result.candidate_confirmation,
                        report.result.candidate_execution,
                    )
                )
                and request.kind != "VIEWED"
            ):
                self._deny(connection, report_id, principal, "CANDIDATE_READ_ONLY", "USER_FACT")
                return None
            fact_id = (
                "user-fact-"
                + sha256(
                    f"{principal.user_id}\n{report_id}\n{request.idempotency_key}".encode()
                ).hexdigest()
            )
            digest = sha256(request.model_dump_json().encode()).hexdigest()
            existing = connection.execute(
                select(USER_FACTS.c.request_digest, USER_FACTS.c.fact_payload).where(
                    USER_FACTS.c.fact_id == fact_id
                )
            ).one_or_none()
            if existing is not None:
                if existing.request_digest != digest:
                    self._deny(
                        connection, report_id, principal, "IDEMPOTENCY_CONFLICT", "USER_FACT"
                    )
                    return None
                return UserFact.model_validate_json(existing.fact_payload)
            if request.kind == "CONFIRMED" and not confirmation_permitted(
                report, self._ledger, connection
            ):
                self._deny(
                    connection, report_id, principal, "PLAN_REVALIDATION_FAILED", "USER_FACT"
                )
                return None
            fact = UserFact(
                fact_id=fact_id,
                report_version_id=report_id,
                event_id=report.event_id,
                kind=request.kind,
                choice=request.choice,
                declaration=request.declaration,
                reconciliation_status=(
                    "PENDING" if request.kind == "EXECUTION_DECLARED" else "NOT_APPLICABLE"
                ),
                recorded_at=self._ledger.observed_at(),
            )
            connection.execute(
                USER_FACTS.insert().values(
                    fact_id=fact_id,
                    report_version_id=report_id,
                    request_digest=digest,
                    fact_payload=fact.model_dump_json(),
                )
            )
            return fact

    def user_facts(
        self, report_id: str, principal: AccessPrincipal | None
    ) -> tuple[UserFact, ...] | None:
        with self._engine.begin() as connection:
            if self._read_report(connection, report_id, principal) is None:
                return None
            return tuple(
                UserFact.model_validate_json(payload)
                for payload in connection.execute(
                    select(USER_FACTS.c.fact_payload)
                    .where(USER_FACTS.c.report_version_id == report_id)
                    .order_by(USER_FACTS.c.sequence)
                ).scalars()
            )

    def audit_history(self) -> tuple[AccessAuditFact, ...]:
        """Host-console audit inspection, not an HTTP or conversational capability."""
        with self._engine.connect() as connection:
            return tuple(
                AccessAuditFact.model_validate(dict(row))
                for row in connection.execute(
                    select(ACCESS_AUDIT).order_by(ACCESS_AUDIT.c.sequence)
                ).mappings()
            )

    def record_capability_denial(self, target: str, surface: str = "FRAMEWORK") -> None:
        """Retain an opaque probe fact without invoking or storing its requested payload."""
        with self._engine.begin() as connection:
            self._deny(connection, target, None, "UNDECLARED_CAPABILITY", surface)
