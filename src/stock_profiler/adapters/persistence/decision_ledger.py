"""Application-owned append-only decision events and formal report projections."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from typing import cast

from pydantic import ValidationError
from sqlalchemy import JSON, Column, Integer, MetaData, String, Table, func, select, type_coerce
from sqlalchemy.engine import Connection, Engine, Row

from stock_profiler.adapters.persistence.access_audit import append_denial
from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage
from stock_profiler.adapters.persistence.user_fact_storage import USER_FACTS
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.foundation.clock import Clock, UtcClock
from stock_profiler.modules.decision_cases.domain import (
    DecisionEventFact,
    ExternalResult,
    FormalReport,
    FrozenDecisionCase,
    NotificationAttempt,
    NotificationAttemptStatus,
    ResultAccessScope,
    StageResult,
    stored_report_payload,
)
from stock_profiler.modules.decision_cases.ports import (
    BusinessObjectMapping as BusinessObjectMapping,
)
from stock_profiler.modules.decision_cases.ports import (
    DecisionEventCommitError as DecisionEventCommitError,
)
from stock_profiler.modules.decision_cases.ports import (
    DecisionEventCommitUncertainError as DecisionEventCommitUncertainError,
)
from stock_profiler.modules.decision_cases.ports import (
    FormalReportCommitUncertainError as FormalReportCommitUncertainError,
)
from stock_profiler.modules.delivery.user_facts import UserFact
from stock_profiler.modules.portfolio.contracts import PortfolioAuthorizationOutcome
from stock_profiler.modules.portfolio.drawdown_contracts import DrawdownOutcome
from stock_profiler.modules.portfolio.liquidity import LiquidityOutcome
from stock_profiler.modules.portfolio.stress import PortfolioStressOutcome
from stock_profiler.modules.position_management.concentration_contracts import ConcentrationHistory
from stock_profiler.modules.position_management.contracts import (
    AccountCashState,
    AuthoritativeLedgerEntry,
    PositionReconciliationOutcome,
)
from stock_profiler.modules.position_management.history import (
    authoritative_cash_history,
    authoritative_ledger_history,
)
from stock_profiler.modules.qualification.contracts import GovernanceOutcome

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
    Column("event_sequence", Integer, nullable=True, unique=True),
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


class DecisionLedger:
    """Persistence boundary for the host's business identity, event, and report."""

    def __init__(self, engine: Engine, *, clock: Clock | None = None) -> None:
        self._engine = engine
        self._clock = clock or UtcClock()

    @classmethod
    def from_settings(cls, settings: Settings, *, clock: Clock | None = None) -> DecisionLedger:
        return cls(initialize_runtime_storage(settings).engine, clock=clock)

    def observed_at(self) -> str:
        """Record the controlled UTC instant at which this host observes a write boundary."""
        return _utc_timestamp(self._clock.now())

    def execution_plan_history(
        self, connection: Connection, access_scope: ResultAccessScope, portfolio_id: str
    ) -> tuple[DecisionEventFact, ...]:
        return tuple(
            fact
            for fact in self._original_event_facts(connection, "execution history is unavailable")
            if fact.case.access_scope is not None
            and fact.case.access_scope.user_id == access_scope.user_id
            and fact.case.access_scope.visibility == access_scope.visibility
            and fact.case.execution_plan is not None
            and fact.case.execution_plan.portfolio_id == portfolio_id
            and fact.result.execution_plan is not None
            and fact.result.execution_plan.targets
            and not set(fact.result.execution_plan.reasons).intersection(
                {
                    "EXECUTION_HISTORY_SCOPE_INCOMPLETE",
                    "EXECUTION_SNAPSHOT_NOT_FORWARD",
                    "RISK_HANDOFF_IDENTITY_MISMATCH",
                    "TARGET_SCOPE_INVALID",
                }
            )
        )

    def monitoring_history(
        self, connection: Connection, access_scope: ResultAccessScope, portfolio_id: str
    ) -> tuple[DecisionEventFact, ...]:
        facts = tuple(
            fact
            for fact in self._event_facts(connection, "monitoring history is unavailable")
            if fact.case.access_scope is not None
            and fact.case.access_scope.user_id == access_scope.user_id
            and fact.case.access_scope.visibility == access_scope.visibility
            and fact.case.monitoring is not None
            and fact.case.monitoring.portfolio_id == portfolio_id
            and fact.result.monitoring is not None
            and not set(fact.result.monitoring.reasons).intersection(
                {
                    "MONITORING_HISTORY_SCOPE_INCOMPLETE",
                    "MONITORING_SNAPSHOT_NOT_FORWARD",
                }
            )
        )
        selected: list[DecisionEventFact] = []
        latest_assessment: str | None = None
        for fact in facts:
            assert fact.case.monitoring is not None
            if fact.case.monitoring.kind in {"DAILY_CLOSE", "EVENT_REASSESS"}:
                if (
                    fact.corrects_event_id is not None
                    and fact.corrects_event_id != latest_assessment
                ):
                    continue
                latest_assessment = fact.decision_event_id
            selected.append(fact)
        return tuple(selected)

    def monitoring_user_facts(
        self,
        connection: Connection,
        access_scope: ResultAccessScope,
        portfolio_id: str,
        recorded_from: datetime,
        recorded_until: datetime,
    ) -> tuple[UserFact, ...]:
        covered = tuple(
            identity
            for identity in self.monitoring_report_ids(connection)
            if (report := self.get_formal_report(identity, connection)) is not None
            and report.access_scope is not None
            and access_scope.same_scope_as(report.access_scope)
            and report.result.monitoring is not None
            and report.result.monitoring.portfolio_id == portfolio_id
        )
        return tuple(
            fact
            for payload in connection.execute(
                select(USER_FACTS.c.fact_payload)
                .where(USER_FACTS.c.report_version_id.in_(covered))
                .order_by(USER_FACTS.c.sequence)
            ).scalars()
            if recorded_from
            <= datetime.fromisoformat((fact := UserFact.model_validate_json(payload)).recorded_at)
            <= recorded_until
        )

    def monitoring_inputs_unchanged(
        self, connection: Connection, access_scope: ResultAccessScope, plan_event_id: str
    ) -> bool:
        """Any newer owner fact requires a fresh plan; publication does not refresh facts."""
        found = False
        for fact in self._event_facts(connection, "monitoring inputs are unavailable"):
            if fact.decision_event_id == plan_event_id:
                found = True
                continue
            scope = fact.case.access_scope
            if (
                found
                and scope is not None
                and scope.user_id == access_scope.user_id
                and scope.visibility == access_scope.visibility
                and fact.case.monitoring is None
            ):
                return False
        return found and self.get_correction_event(plan_event_id, connection) is None

    def monitoring_report_ids(self, connection: Connection) -> tuple[str, ...]:
        """Internal projection inventory; ResultDelivery applies the principal boundary."""
        return tuple(
            report.report_version_id
            for report_id in connection.execute(
                select(FORMAL_REPORTS.c.report_version_id)
                .join(
                    DECISION_EVENTS,
                    FORMAL_REPORTS.c.decision_event_id == DECISION_EVENTS.c.decision_event_id,
                )
                .order_by(DECISION_EVENTS.c.event_sequence)
            ).scalars()
            if (report := self.get_formal_report(report_id, connection)) is not None
            and report.result.monitoring is not None
        )

    def concentration_history(
        self,
        connection: Connection,
        access_scope: ResultAccessScope,
        portfolio_id: str,
    ) -> ConcentrationHistory:
        """Read covered original obligations, including before an account expansion."""
        lineage = [
            (scope, concentration)
            for fact in self._original_event_facts(
                connection, "concentration history is unavailable"
            )
            if (scope := fact.case.access_scope) is not None
            and scope.user_id == access_scope.user_id
            and scope.visibility == access_scope.visibility
            and (concentration := fact.result.concentration) is not None
            and concentration.portfolio_id == portfolio_id
            and "CONCENTRATION_SNAPSHOT_OUT_OF_ORDER" not in concentration.reasons
            and "CONCENTRATION_HISTORY_SCOPE_UNRESOLVED" not in concentration.reasons
        ]
        lineage.sort(key=lambda item: item[1].cutoff_at)
        latest = {
            issuer.issuer_id: (scope, issuer)
            for scope, outcome in lineage
            for issuer in outcome.issuers
            if issuer.obligation_id is not None
        }
        return ConcentrationHistory(
            outcomes=tuple(
                outcome
                for scope, outcome in lineage
                if set(scope.account_ids).issubset(access_scope.account_ids)
            ),
            uncovered_obligation=any(
                issuer.direction == "REDUCE"
                and not set(scope.account_ids).issubset(access_scope.account_ids)
                for scope, issuer in latest.values()
            ),
        )

    def governance_history(
        self, connection: Connection, access_scope: ResultAccessScope
    ) -> tuple[GovernanceOutcome, ...]:
        """Select original authority by its saved owner, accounts and visibility before use."""
        return tuple(
            fact.result.governance
            for fact in self._original_event_facts(connection, "governance history is unavailable")
            if (
                fact.case.access_scope is not None
                and fact.case.access_scope.same_scope_as(access_scope)
                and fact.result.governance is not None
            )
        )

    def portfolio_authorization_history(
        self,
        connection: Connection,
        access_scope: ResultAccessScope,
        portfolio_id: str,
        knowledge_cutoff: str,
    ) -> tuple[PortfolioAuthorizationOutcome, ...]:
        """Read only one visible lineage whose retained evidence predates the cutoff."""
        cutoff = datetime.fromisoformat(knowledge_cutoff)
        return tuple(
            portfolio
            for portfolio in self.portfolio_authorization_lineage(
                connection,
                access_scope,
                portfolio_id,
            )
            if portfolio.authorization is not None
            and portfolio.authorization.evidence_available_by(cutoff)
        )

    def portfolio_authorization_lineage(
        self,
        connection: Connection,
        access_scope: ResultAccessScope,
        portfolio_id: str,
    ) -> tuple[PortfolioAuthorizationOutcome, ...]:
        """Read a trusted host-only lineage for stale revision/use rejection, never projection."""
        return tuple(
            portfolio
            for portfolio in self.portfolio_authorization_owner_lineage(
                connection,
                access_scope,
            )
            if portfolio.authorization is not None
            and portfolio.authorization.proposal.portfolio_id == portfolio_id
        )

    def portfolio_authorization_owner_lineage(
        self,
        connection: Connection,
        access_scope: ResultAccessScope,
    ) -> tuple[PortfolioAuthorizationOutcome, ...]:
        """Read same-owner/visibility history only for negative cross-portfolio guards."""
        return tuple(
            portfolio
            for fact in self._original_event_facts(
                connection, "portfolio authorization history is unavailable"
            )
            if (
                (scope := fact.case.access_scope) is not None
                and scope.user_id == access_scope.user_id
                and scope.visibility == access_scope.visibility
                and (portfolio := fact.result.portfolio) is not None
                and portfolio.authorization is not None
            )
        )

    def _original_event_facts(
        self, connection: Connection, unavailable_message: str
    ) -> tuple[DecisionEventFact, ...]:
        return self._event_facts(connection, unavailable_message, originals_only=True)

    def _event_facts(
        self,
        connection: Connection,
        unavailable_message: str,
        *,
        originals_only: bool = False,
    ) -> tuple[DecisionEventFact, ...]:
        query = select(DECISION_EVENTS.c.decision_event_id)
        if originals_only:
            query = query.where(DECISION_EVENTS.c.corrects_event_id.is_(None))
        event_ids = (
            connection.execute(
                query.order_by(
                    DECISION_EVENTS.c.event_sequence,
                    DECISION_EVENTS.c.committed_at,
                    DECISION_EVENTS.c.decision_event_id,
                )
            )
            .scalars()
            .all()
        )
        facts = []
        for event_id in event_ids:
            fact = self.get_decision_event(event_id, connection)
            if fact is None:
                raise DecisionEventCommitError(unavailable_message)
            facts.append(fact)
        return tuple(facts)

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
        try:
            stored_case = (
                FrozenDecisionCase.model_validate_json(row.case_payload)
                if row.case_payload is not None
                else None
            )
        except ValidationError as error:
            raise DecisionEventCommitError("stored frozen case snapshot is invalid") from error
        case = stored_case
        if stored_case is not None and stored_case.framework_run_id != row.framework_run_id:
            case = stored_case.model_copy(
                update={"recovery_framework_run_id": row.framework_run_id}
            )
        return BusinessObjectMapping(
            case_id=row.case_id,
            frozen_input_fingerprint=row.frozen_input_fingerprint,
            framework_run_id=row.framework_run_id,
            case=case,
        )

    def get_frozen_case_by_identity(self, business_identity: str) -> FrozenDecisionCase | None:
        """Resolve an unambiguous saved host-console identity, never an HTTP authorization."""
        with self._engine.connect() as connection:
            object_ids = (
                connection.execute(
                    select(DECISION_CASE_BUSINESS_OBJECTS.c.business_object_id).where(
                        type_coerce(DECISION_CASE_BUSINESS_OBJECTS.c.case_payload, JSON)[
                            "business_identity"
                        ].as_string()
                        == business_identity
                    )
                )
                .scalars()
                .all()
            )
            if len(object_ids) > 1:
                raise ValueError("ambiguous frozen decision-case business identity")
            if not object_ids:
                return None
            mapping = self.get_business_object_mapping(object_ids[0], connection)
            if (
                mapping is None
                or mapping.case is None
                or mapping.case.business_identity != business_identity
                or mapping.case.business_object_id != object_ids[0]
                or mapping.case.frozen_input_fingerprint != mapping.frozen_input_fingerprint
                or mapping.case.case_id != mapping.case_id
            ):
                raise DecisionEventCommitError("stored frozen case identity is inconsistent")
            return mapping.case

    def mapped_framework_run_ids(self, connection: Connection) -> frozenset[str]:
        return frozenset(
            connection.execute(select(DECISION_CASE_BUSINESS_OBJECTS.c.framework_run_id)).scalars()
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
                    created_at=self.observed_at(),
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

    def resolve_business_object_id(self, case: FrozenDecisionCase, connection: Connection) -> str:
        """Choose one retained business lineage without inventing a version fork."""
        matching_ids = tuple(
            business_object_id
            for business_object_id in case.recovery_business_object_ids
            if self.get_business_object_mapping(business_object_id, connection) is not None
        )
        if len(matching_ids) > 1:
            raise DecisionEventCommitError(
                "multiple durable business mappings match the frozen business identity"
            )
        return matching_ids[0] if matching_ids else case.business_object_id

    def persist_business_mapping_before_framework(self, case: FrozenDecisionCase) -> str:
        """Commit the original mapping before a separate M-Agent store can checkpoint work."""
        with self.serialize_case_execution() as connection:
            business_object_id = self.resolve_business_object_id(case, connection)
            if (
                business_object_id == case.business_object_id
                and self.get_original_decision_event(business_object_id, connection) is None
            ):
                self.ensure_business_object(connection, case)
            return business_object_id

    def drawdown_history(
        self, connection: Connection, access_scope: ResultAccessScope
    ) -> tuple[DrawdownOutcome, ...]:
        """Owner history supplies a negative guard against a replacement capital epoch."""
        return tuple(
            outcome
            for fact in self._original_event_facts(connection, "drawdown history is unavailable")
            if (scope := fact.case.access_scope) is not None
            and scope.user_id == access_scope.user_id
            and scope.visibility == access_scope.visibility
            and (outcome := fact.result.drawdown) is not None
        )

    def position_evidence_for_drawdown(
        self,
        connection: Connection,
        access_scope: ResultAccessScope,
        event_id: str,
        cutoff_at: datetime,
    ) -> PositionReconciliationOutcome | None:
        fact = self.get_decision_event(event_id, connection)
        if (
            fact is None
            or fact.corrects_event_id is not None
            or fact.case.access_scope is None
            or not fact.case.access_scope.same_scope_as(access_scope)
            or fact.result.position is None
            or fact.result.position.snapshot.cutoff_at > cutoff_at
        ):
            return None
        return fact.result.position

    def position_ledger_history(
        self,
        connection: Connection,
        access_scope: ResultAccessScope,
        cutoff_at: datetime,
    ) -> tuple[AuthoritativeLedgerEntry, ...]:
        """Return first-observed, visible ledger facts available by the frozen cutoff."""
        return authoritative_ledger_history(
            self._position_history(connection, access_scope),
            account_ids=frozenset(access_scope.account_ids),
            cutoff_at=cutoff_at,
        )

    def portfolio_stress_history(
        self,
        connection: Connection,
        access_scope: ResultAccessScope,
        portfolio_id: str,
    ) -> tuple[PortfolioStressOutcome, ...]:
        """Host-only lineage; future and uncovered records may only block new decisions."""
        return tuple(
            stress
            for fact in self._original_event_facts(connection, "stress history is unavailable")
            if (scope := fact.case.access_scope) is not None
            and scope.user_id == access_scope.user_id
            and scope.visibility == access_scope.visibility
            and (stress := fact.result.stress) is not None
            and stress.portfolio_id == portfolio_id
        )

    def position_cash_history(
        self,
        connection: Connection,
        access_scope: ResultAccessScope,
        cutoff_at: datetime,
    ) -> tuple[AccountCashState, ...]:
        """Return first-observed visible opening-cash baselines by account."""
        return authoritative_cash_history(
            self._position_history(connection, access_scope),
            account_ids=frozenset(access_scope.account_ids),
            cutoff_at=cutoff_at,
        )

    def liquidity_history(
        self,
        connection: Connection,
        access_scope: ResultAccessScope,
        portfolio_id: str,
        cutoff_at: datetime,
    ) -> tuple[LiquidityOutcome, ...]:
        """Read same-owner portfolio history; adjudication guards missing account scope."""
        return tuple(
            fact.result.liquidity
            for fact in self._original_event_facts(connection, "liquidity history is unavailable")
            if fact.case.access_scope is not None
            and fact.case.access_scope.user_id == access_scope.user_id
            and fact.case.access_scope.visibility == access_scope.visibility
            and fact.case.liquidity is not None
            and fact.case.liquidity.portfolio_id == portfolio_id
            and fact.case.liquidity.position_snapshot.cutoff_at <= cutoff_at
            and fact.result.liquidity is not None
        )

    def _position_history(
        self,
        connection: Connection,
        access_scope: ResultAccessScope,
    ) -> tuple[PositionReconciliationOutcome, ...]:
        """Read same-owner, same-visibility position facts in durable event order."""
        return tuple(
            position
            for fact in self._original_event_facts(
                connection,
                "position ledger history is unavailable",
            )
            if (
                (scope := fact.case.access_scope) is not None
                and scope.user_id == access_scope.user_id
                and scope.visibility == access_scope.visibility
                and (position := fact.result.position) is not None
            )
        )

    def get_decision_event(
        self, decision_event_id: str, connection: Connection
    ) -> DecisionEventFact | None:
        """Read the committed host fact before attempting a report projection."""
        row = connection.execute(
            select(
                DECISION_EVENTS.c.decision_event_id,
                DECISION_EVENTS.c.business_object_id,
                DECISION_EVENTS.c.framework_run_id,
                DECISION_EVENTS.c.corrects_event_id,
                DECISION_EVENTS.c.event_payload,
            ).where(DECISION_EVENTS.c.decision_event_id == decision_event_id)
        ).one_or_none()
        return self._stored_decision_event(connection, row) if row is not None else None

    def get_original_decision_event(
        self, business_object_id: str, connection: Connection
    ) -> DecisionEventFact | None:
        """Read the original fact by stable business identity across host builds."""
        row = connection.execute(
            select(
                DECISION_EVENTS.c.decision_event_id,
                DECISION_EVENTS.c.business_object_id,
                DECISION_EVENTS.c.framework_run_id,
                DECISION_EVENTS.c.corrects_event_id,
                DECISION_EVENTS.c.event_payload,
            )
            .where(
                DECISION_EVENTS.c.business_object_id == business_object_id,
                DECISION_EVENTS.c.corrects_event_id.is_(None),
            )
            .order_by(DECISION_EVENTS.c.committed_at)
        ).one_or_none()
        return self._stored_decision_event(connection, row) if row is not None else None

    def get_correction_event(
        self, original_event_id: str, connection: Connection
    ) -> DecisionEventFact | None:
        """Read the sole D0 correction that already references one original fact."""
        row = connection.execute(
            select(
                DECISION_EVENTS.c.decision_event_id,
                DECISION_EVENTS.c.business_object_id,
                DECISION_EVENTS.c.framework_run_id,
                DECISION_EVENTS.c.corrects_event_id,
                DECISION_EVENTS.c.event_payload,
            ).where(DECISION_EVENTS.c.corrects_event_id == original_event_id)
        ).one_or_none()
        return self._stored_decision_event(connection, row) if row is not None else None

    def _stored_decision_event(
        self,
        connection: Connection,
        row: Row[tuple[str, str, str, str | None, str]],
    ) -> DecisionEventFact:
        """Bind persisted event JSON to its row and stable business mapping before use."""
        decision_event_id = row.decision_event_id
        business_object_id = row.business_object_id
        framework_run_id = row.framework_run_id
        corrects_event_id = row.corrects_event_id
        payload = row.event_payload
        try:
            fact = DecisionEventFact.model_validate_json(payload)
        except ValidationError as error:
            raise DecisionEventCommitError("stored decision event is invalid") from error
        mapping = self.get_business_object_mapping(business_object_id, connection)
        has_snapshotless_legacy_mapping_for_fact = (
            mapping is not None
            and mapping.case is None
            and mapping.case_id == fact.case.case_id
            and fact.case.matches_legacy_recovery_input()
        )
        has_recovered_mapping_for_fact = (
            mapping is not None
            and mapping.framework_run_id == framework_run_id
            and (
                (mapping.case is not None and mapping.case.matches_recovery_input(fact.case))
                or has_snapshotless_legacy_mapping_for_fact
            )
        )
        expected_event_ids = (
            (fact.case.correction_event_id(fact.corrects_event_id),)
            if fact.corrects_event_id is not None
            else (
                fact.case.decision_event_id_for_framework_run(framework_run_id),
                fact.case.legacy_decision_event_id_for_framework_run(framework_run_id),
            )
        )
        is_correction = fact.corrects_event_id is not None
        if (
            fact.decision_event_id != decision_event_id
            or fact.business_object_id != business_object_id
            or fact.framework_run_id != framework_run_id
            or fact.corrects_event_id != corrects_event_id
            or fact.case.business_object_id != business_object_id
            or (
                not is_correction
                and fact.case.framework_run_id != framework_run_id
                and not has_recovered_mapping_for_fact
            )
            or fact.decision_event_id not in expected_event_ids
        ):
            raise DecisionEventCommitError(
                "stored decision event does not match its durable lineage"
            )
        if corrects_event_id is not None:
            if corrects_event_id == decision_event_id:
                raise DecisionEventCommitError(
                    "stored decision event does not match its durable lineage"
                )
            original_event = self.get_decision_event(corrects_event_id, connection)
            if (
                original_event is None
                or original_event.corrects_event_id is not None
                or original_event.business_object_id != business_object_id
                or original_event.framework_run_id != framework_run_id
            ):
                raise DecisionEventCommitError(
                    "stored decision event does not match its durable lineage"
                )
        if (
            mapping is None
            or mapping.case_id != fact.case.case_id
            or mapping.framework_run_id != framework_run_id
            or (
                fact.corrects_event_id is None
                and mapping.frozen_input_fingerprint != fact.case.frozen_input_fingerprint
                and not has_snapshotless_legacy_mapping_for_fact
            )
            or (
                mapping.case is not None
                and not (
                    mapping.case.matches_recovery_input(fact.case)
                    if fact.corrects_event_id is None
                    else mapping.case.matches_legacy_recovery_input(fact.case)
                )
            )
        ):
            raise DecisionEventCommitError(
                "stored decision event does not match its durable business mapping"
            )
        return fact

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
        recorded_at: str | None = None,
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
                recorded_at=recorded_at or self.observed_at(),
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
        occurrence = occurrence_count + 1 if allow_repeated_occurrence else 1
        self._insert_or_validate_stage_result(
            connection,
            stage_event_id=_stage_event_id(
                case,
                stage_result,
                decision_event_id,
                framework_run_id=durable_framework_run_id,
                occurrence=occurrence if allow_repeated_occurrence else None,
            ),
            case=case,
            framework_run_id=durable_framework_run_id,
            decision_event_id=decision_event_id,
            stage_payload=stage_payload,
            stage_result=stage_result,
            recorded_at=recorded_at or self.observed_at(),
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
        recorded_at: str,
    ) -> None:
        existing = connection.execute(
            select(
                DECISION_STAGE_EVENTS.c.business_object_id,
                DECISION_STAGE_EVENTS.c.framework_run_id,
                DECISION_STAGE_EVENTS.c.decision_event_id,
                DECISION_STAGE_EVENTS.c.stage_payload,
            ).where(DECISION_STAGE_EVENTS.c.stage_event_id == stage_event_id)
        ).one_or_none()
        if existing is None:
            connection.execute(
                DECISION_STAGE_EVENTS.insert().values(
                    stage_event_id=stage_event_id,
                    business_object_id=case.business_object_id,
                    framework_run_id=framework_run_id,
                    decision_event_id=decision_event_id,
                    stage_payload=stage_payload,
                    recorded_at=recorded_at,
                )
            )
            return
        if (
            existing.business_object_id != case.business_object_id
            or existing.framework_run_id != framework_run_id
            or existing.decision_event_id != decision_event_id
            or StageResult.model_validate_json(existing.stage_payload) != stage_result
        ):
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
            recorded_at=self.observed_at(),
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
                recorded_at=attempt.recorded_at,
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
                DECISION_NOTIFICATION_ATTEMPTS.c.recorded_at,
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
                recorded_at=row.recorded_at,
            )
            for row in rows
        )

    def commit_event_fact(
        self, connection: Connection, fact: DecisionEventFact
    ) -> DecisionEventFact:
        """Submit the same immutable fact that the host will reconcile on uncertainty."""
        return self.commit_event(
            connection,
            case=fact.case,
            framework_run_id=fact.framework_run_id,
            result=fact.result,
            stage_results=fact.stage_results,
            decision_event_id=fact.decision_event_id,
            corrects_event_id=fact.corrects_event_id,
            committed_at=fact.committed_at,
            generated_at=fact.generated_at,
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
            next_event_sequence = (
                int(
                    connection.execute(
                        select(func.coalesce(func.max(DECISION_EVENTS.c.event_sequence), 0))
                    ).scalar_one()
                )
                + 1
            )
            connection.execute(
                DECISION_EVENTS.insert().values(
                    decision_event_id=fact.decision_event_id,
                    event_sequence=next_event_sequence,
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
        observed_at = committed_at or self.observed_at()
        return DecisionEventFact(
            decision_event_id=event_id,
            business_object_id=case.business_object_id,
            framework_run_id=framework_run_id,
            case=case,
            result=result,
            validation_status="PASSED",
            committed_at=observed_at,
            stage_results=stage_results,
            corrects_event_id=corrects_event_id,
            generated_at=generated_at or observed_at,
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

    def reconcile_report_commit(
        self, connection: Connection, attempted: FormalReport
    ) -> FormalReport | None:
        """Verify an acknowledgement-uncertain report by its original report identity."""
        connection.rollback()
        with self._engine.connect() as verification_connection:
            committed = self._stored_formal_report(
                attempted.report_version_id,
                verification_connection,
            )
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        if committed is not None and committed != attempted:
            raise DecisionEventCommitError("committed report does not match attempted projection")
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
        if fact.case.access_scope is not None and fact.case.access_scope.visibility == "SHADOW":
            # Abandon uncommitted work before auditing, so the caller's rollback
            # cannot erase the denial and its SQLite write lock cannot deadlock it.
            connection.rollback()
            with self._engine.begin() as audit_connection:
                append_denial(
                    audit_connection,
                    fact.decision_event_id,
                    None,
                    "SHADOW_ISOLATED",
                    "PUBLICATION",
                    self.observed_at(),
                )
            raise DecisionEventCommitError("shadow events cannot become user reports")
        if not self._has_confirmed_business_commit(connection, fact.decision_event_id):
            raise DecisionEventCommitError("report source has no confirmed business commit")
        resolved_report_version_id = report_version_id or fact.case.report_version_id_for_event(
            fact.decision_event_id
        )
        existing = self._stored_formal_report(resolved_report_version_id, connection)
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
        try:
            connection.commit()
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        except Exception as error:
            connection.rollback()
            raise FormalReportCommitUncertainError(
                "formal report commit acknowledgement is uncertain"
            ) from error
        return report

    def get_formal_report(
        self, report_version_id: str, connection: Connection | None = None
    ) -> FormalReport | None:
        """Read a report only when the report row references a committed event."""
        if connection is not None:
            return self._confirmed_formal_report_from_connection(connection, report_version_id)
        with self._engine.connect() as read_connection:
            return self._confirmed_formal_report_from_connection(
                read_connection,
                report_version_id,
            )

    def get_original_formal_report(
        self, business_object_id: str, connection: Connection
    ) -> FormalReport | None:
        """Read the original published report by its stable business identity."""
        row = connection.execute(
            select(
                FORMAL_REPORTS.c.report_version_id,
                FORMAL_REPORTS.c.decision_event_id,
                FORMAL_REPORTS.c.report_payload,
                FORMAL_REPORTS.c.generated_at,
            )
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
        if not self._has_confirmed_business_commit(
            connection, row.decision_event_id
        ) or not self._has_confirmed_publication(connection, row.decision_event_id):
            return None
        return self._with_publication_history(
            connection,
            self._stored_formal_report_from_row(connection, row),
        )

    def get_formal_report_for_event(
        self, decision_event_id: str, connection: Connection
    ) -> FormalReport | None:
        """Read one existing projection without deriving a new report identity."""
        row = connection.execute(
            select(
                FORMAL_REPORTS.c.report_version_id,
                FORMAL_REPORTS.c.decision_event_id,
                FORMAL_REPORTS.c.report_payload,
                FORMAL_REPORTS.c.generated_at,
            )
            .join(
                DECISION_EVENTS,
                FORMAL_REPORTS.c.decision_event_id == DECISION_EVENTS.c.decision_event_id,
            )
            .where(FORMAL_REPORTS.c.decision_event_id == decision_event_id)
        ).one_or_none()
        if row is None:
            return None
        if not self._has_confirmed_business_commit(
            connection, row.decision_event_id
        ) or not self._has_confirmed_publication(connection, row.decision_event_id):
            return None
        return self._with_publication_history(
            connection,
            self._stored_formal_report_from_row(connection, row),
        )

    def _stored_formal_report(
        self, report_version_id: str, connection: Connection
    ) -> FormalReport | None:
        """Read a report row for idempotent recovery before public confirmation."""
        row = connection.execute(
            select(
                FORMAL_REPORTS.c.report_version_id,
                FORMAL_REPORTS.c.decision_event_id,
                FORMAL_REPORTS.c.report_payload,
                FORMAL_REPORTS.c.generated_at,
            ).where(FORMAL_REPORTS.c.report_version_id == report_version_id)
        ).one_or_none()
        return self._stored_formal_report_from_row(connection, row) if row is not None else None

    def _confirmed_formal_report_from_connection(
        self, connection: Connection, report_version_id: str
    ) -> FormalReport | None:
        row = connection.execute(
            select(
                FORMAL_REPORTS.c.report_version_id,
                FORMAL_REPORTS.c.decision_event_id,
                FORMAL_REPORTS.c.report_payload,
                FORMAL_REPORTS.c.generated_at,
            )
            .join(
                DECISION_EVENTS,
                FORMAL_REPORTS.c.decision_event_id == DECISION_EVENTS.c.decision_event_id,
            )
            .where(FORMAL_REPORTS.c.report_version_id == report_version_id)
        ).one_or_none()
        if row is None:
            return None
        if not self._has_confirmed_business_commit(
            connection, row.decision_event_id
        ) or not self._has_confirmed_publication(connection, row.decision_event_id):
            return None
        return self._with_publication_history(
            connection,
            self._stored_formal_report_from_row(connection, row),
        )

    def _stored_formal_report_from_row(
        self,
        connection: Connection,
        row: Row[tuple[str, str, str, str]],
    ) -> FormalReport:
        """Bind persisted report JSON to its row and exact source event projection."""
        report_version_id = row.report_version_id
        decision_event_id = row.decision_event_id
        payload = row.report_payload
        generated_at = row.generated_at
        event = self.get_decision_event(decision_event_id, connection)
        if event is None:
            raise DecisionEventCommitError(
                "stored formal report references an unknown decision event"
            )
        try:
            serialized_payload = _canonical_json(json.loads(payload))
            report = FormalReport.model_validate_json(payload)
        except (json.JSONDecodeError, ValidationError) as error:
            raise DecisionEventCommitError("stored formal report is invalid") from error
        if (
            report.report_version_id != report_version_id
            or report.event_id != decision_event_id
            or report.generated_at != generated_at
            or report_version_id != _report_version_id_for_event(event)
            or serialized_payload
            != _canonical_json(stored_report_payload(event, report_version_id))
        ):
            raise DecisionEventCommitError("stored formal report does not match its durable event")
        return report

    def _has_confirmed_business_commit(
        self, connection: Connection, decision_event_id: str
    ) -> bool:
        """Require a saved successful host commit before any report is exposed."""
        stage_payloads = connection.execute(
            select(DECISION_STAGE_EVENTS.c.stage_payload).where(
                DECISION_STAGE_EVENTS.c.decision_event_id == decision_event_id
            )
        ).scalars()
        return any(
            stage_result.phase == "BUSINESS_COMMIT" and stage_result.status == "SUCCEEDED"
            for stage_result in (
                StageResult.model_validate_json(payload) for payload in stage_payloads
            )
        )

    def _with_publication_history(
        self,
        connection: Connection,
        report: FormalReport,
    ) -> FormalReport:
        """Acquire saved stage facts; the report contract owns their visible selection."""
        event_stages = tuple(
            stage_result
            for stage_result in (
                StageResult.model_validate_json(payload)
                for payload in connection.execute(
                    select(DECISION_STAGE_EVENTS.c.stage_payload)
                    .where(DECISION_STAGE_EVENTS.c.decision_event_id == report.event_id)
                    .order_by(DECISION_STAGE_EVENTS.c.sequence)
                ).scalars()
            )
        )
        projected = report.with_publication_history(event_stages)
        if report.result.monitoring is None:
            return projected
        from stock_profiler.modules.delivery.monitoring_contracts import MonitoringPublication

        clocks: dict[str, str] = {}
        for payload, recorded_at in connection.execute(
            select(DECISION_STAGE_EVENTS.c.stage_payload, DECISION_STAGE_EVENTS.c.recorded_at)
            .where(DECISION_STAGE_EVENTS.c.decision_event_id == report.event_id)
            .order_by(DECISION_STAGE_EVENTS.c.sequence)
        ):
            stage = StageResult.model_validate_json(payload)
            if stage.status == "SUCCEEDED":
                clocks.setdefault(stage.phase, recorded_at)
        if "BUSINESS_COMMIT" not in clocks or "PUBLICATION" not in clocks:
            raise DecisionEventCommitError("monitoring publication clocks are unavailable")
        return projected.model_copy(
            update={
                "monitoring_publication": MonitoringPublication(
                    committed_at=clocks["BUSINESS_COMMIT"], published_at=clocks["PUBLICATION"]
                )
            }
        )

    def _has_confirmed_publication(self, connection: Connection, decision_event_id: str) -> bool:
        """Expose a report only after an append-only publication success was saved."""
        stage_payloads = connection.execute(
            select(DECISION_STAGE_EVENTS.c.stage_payload).where(
                DECISION_STAGE_EVENTS.c.decision_event_id == decision_event_id
            )
        ).scalars()
        return any(
            stage_result.phase == "PUBLICATION" and stage_result.status == "SUCCEEDED"
            for stage_result in (
                StageResult.model_validate_json(payload) for payload in stage_payloads
            )
        )

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


def _report_version_id_for_event(event: DecisionEventFact) -> str:
    if event.corrects_event_id is not None:
        return event.case.correction_report_version_id(event.corrects_event_id)
    return event.case.report_version_id_for_event(event.decision_event_id)


def _notification_attempt_id(report: FormalReport, attempt_number: int) -> str:
    payload = {
        "report_version_id": report.report_version_id,
        "event_id": report.event_id,
        "attempt_number": attempt_number,
    }
    serialized = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return f"notification-attempt-{sha256(serialized.encode()).hexdigest()}"


def _utc_timestamp(value: datetime) -> str:
    """Serialize a controlled timestamp as the ledger's canonical UTC representation."""
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
