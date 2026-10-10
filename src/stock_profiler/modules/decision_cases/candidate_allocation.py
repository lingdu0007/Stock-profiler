"""Host-owned allocation from saved candidates and authoritative risk handoffs."""

from collections import defaultdict
from dataclasses import replace
from datetime import datetime
from decimal import Context, Decimal, DecimalException, localcontext

from stock_profiler.modules.candidate_selection.current_eligibility import (
    candidate_qualification_eligibility,
)
from stock_profiler.modules.decision_cases.allocation_inputs import saved_allocation_inputs_changed
from stock_profiler.modules.decision_cases.beta_permission import saved_beta_permission_reasons
from stock_profiler.modules.decision_cases.domain import FormalReport, FrozenDecisionCase
from stock_profiler.modules.decision_cases.execution_plans import adjudicate_execution_plan
from stock_profiler.modules.decision_cases.ports import DecisionLedger, Transaction
from stock_profiler.modules.portfolio.allocation_contracts import (
    AcquisitionLeg,
    AllocationCapacityCheck,
    AllocationRouteFailure,
    CandidateAllocationOutcome,
    CandidateAllocationRow,
)
from stock_profiler.modules.portfolio.allocation_solver import (
    CONTINUOUS_PRINCIPAL_QUANTUM,
    AllocationProblem,
    AllocationSolveFailed,
    Capacity,
    DiscreteAllocation,
    capacity_is_feasible,
    continuous_allocation,
    discrete_allocation,
    discrete_principal_ceiling,
)
from stock_profiler.modules.portfolio.allocation_window import entry_window_is_open
from stock_profiler.modules.portfolio.beta_exposure import retained_acquired_quantity
from stock_profiler.modules.portfolio.market_calendar import synthetic_market_calendar
from stock_profiler.modules.position_management.contracts import ReconciledPositionSnapshot
from stock_profiler.modules.qualification.beta import (
    envelope_decision,
    observation_history_reasons,
    qualification_reasons,
)
from stock_profiler.modules.qualification.beta_contracts import BetaEnvelopeOutcome

_REASON_ORDER = (
    "COMPLETE_PORTFOLIO_REQUIRED",
    "ACTIVE_RISK_BUDGET_REQUIRED",
    "NEW_EXPOSURE_BLOCKED",
    "BETA_CAPACITY_EXHAUSTED",
    "CASH_CAPACITY_EXHAUSTED",
    "ACCOUNT_CASH_CAPACITY_EXHAUSTED",
    "STRESS_CAPACITY_EXHAUSTED",
    "ENTRY_TARGET_REACHED",
    "ISSUER_CAPACITY_EXHAUSTED",
    "CORRELATION_CAPACITY_EXHAUSTED",
    "CORRELATION_EVIDENCE_FAILED",
    "LIQUIDITY_CAPACITY_EXHAUSTED",
    "MARKET_CAPACITY_EVIDENCE_FAILED",
    "ISSUER_IDENTITY_MISMATCH",
    "ACCOUNT_SCOPE_MISMATCH",
    "ACCOUNT_PERMISSION_DENIED",
    "ACCOUNT_EXECUTION_RESTRICTED",
    "PRICE_CAP_REQUIRED",
    "CURRENT_PRICE_UNAVAILABLE",
    "CURRENT_PRICE_OUT_OF_RANGE",
    "PRICE_CAP_OUT_OF_RANGE",
    "PRICE_TICK_INVALID",
    "BUY_ROUTE_EVIDENCE_FAILED",
    "BUY_ROUTE_UNAVAILABLE",
    "BELOW_MINIMUM_BUY_UNIT",
    "UNALLOCATED_CAPACITY_PRIORITY",
    "ROUNDING_REMAINDER_UNUSABLE",
    "ENTRY_WINDOW_EXPIRED",
)


def _ordered_reasons(reasons: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        sorted(
            dict.fromkeys(reasons),
            key=lambda reason: (
                _REASON_ORDER.index(reason) if reason in _REASON_ORDER else len(_REASON_ORDER)
            ),
        )
    )


def finalize_candidate_allocation(
    case: FrozenDecisionCase,
    outcome: CandidateAllocationOutcome,
    ledger: DecisionLedger[Transaction],
    connection: Transaction,
    *,
    committed_at: str,
) -> CandidateAllocationOutcome:
    command = case.candidate_allocation
    assert command is not None
    if outcome.disposition == "PLANNED" and command.beta is not None:
        assert case.access_scope is not None
        now = datetime.fromisoformat(committed_at)
        reasons = saved_beta_permission_reasons(case, outcome.beta, ledger, connection, now)
        if reasons:
            source = ledger.get_formal_report_for_event(command.candidate_event_id, connection)
            assert source is not None
            blocked = _blocked(case, source, reasons)
            return blocked.model_copy(
                update={
                    "beta": outcome.beta.model_copy(update={"capacity": Decimal(0)})
                    if outcome.beta is not None
                    else None
                }
            )
    if command.replaces_plan_event_id is None or outcome.disposition != "PLANNED":
        return outcome
    if saved_allocation_inputs_changed(case, ledger, connection):
        return outcome.model_copy(
            update={"disposition": "BLOCKED", "reasons": ("ALLOCATION_INPUT_VERSION_CONFLICT",)}
        )
    source = ledger.get_formal_report_for_event(command.replaces_plan_event_id, connection)
    if (
        source is None
        or source.result.candidate_allocation is None
        or not entry_window_is_open(
            source.result.candidate_allocation, datetime.fromisoformat(committed_at)
        )
    ):
        return outcome.model_copy(
            update={"disposition": "BLOCKED", "reasons": ("REPLANNING_WINDOW_CLOSED",)}
        )
    candidate = ledger.get_formal_report_for_event(command.candidate_event_id, connection)
    original = ledger.get_decision_event(command.candidate_event_id, connection)
    assert case.access_scope is not None
    if candidate is None or candidate.result.candidate_release is None or original is None:
        return outcome.model_copy(
            update={"disposition": "BLOCKED", "reasons": ("CANDIDATE_HANDOFF_UNAVAILABLE",)}
        )
    reasons, evidence_ids, _ = candidate_qualification_eligibility(
        candidate.result.candidate_release,
        original.case,
        ledger.governance_history(connection, case.access_scope),
        datetime.fromisoformat(committed_at),
    )
    if reasons:
        return outcome.model_copy(
            update={
                "disposition": "BLOCKED",
                "reasons": reasons,
                "eligibility_evidence_ids": evidence_ids,
            }
        )
    return outcome


def _capacity_binding(problem: AllocationProblem, check: AllocationCapacityCheck) -> bool:
    """Explain a binding gate despite downward truncation of its monetary proposal.

    This allowance labels reasons only. Every published quantity and capacity
    still passes the original exact Decimal checks without any allowance.
    """
    allowance = Decimal(0)
    for route in problem.routes:
        if route.security_id not in check.security_ids or (
            check.account_id is not None and route.account_id != check.account_id
        ):
            continue
        coefficient = (
            problem.shock + route.disposal_friction_ratio
            if check.gate_id == "global:stress"
            else 1 + route.commission_ratio + route.other_cost_ratio
            if check.gate_id == "global:cash" or check.account_id is not None
            else Decimal(1)
        )
        allowance += CONTINUOUS_PRINCIPAL_QUANTUM * coefficient
    return check.remaining_after_continuous <= allowance


def _correlated(left: tuple[Decimal, ...], right: tuple[Decimal, ...], ceiling: Decimal) -> bool:
    count = Decimal(len(left))
    left_mean, right_mean = sum(left) / count, sum(right) / count
    covariance = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right, strict=True))
    left_variance = sum((value - left_mean) ** 2 for value in left)
    right_variance = sum((value - right_mean) ** 2 for value in right)
    return covariance > 0 and covariance**2 > ceiling**2 * left_variance * right_variance


def _held_beta_exposure(
    case: FrozenDecisionCase,
    snapshot: ReconciledPositionSnapshot,
    ledger: DecisionLedger[Transaction],
    connection: Transaction,
) -> Decimal:
    """Carry acquired exposure across batches and confirmation successors.

    Broker fills establish acquisition; fresh reconciled quantities and prices
    establish remaining exposure. Relabelling an activation cannot erase either.
    """
    assert case.access_scope is not None and case.candidate_allocation is not None
    portfolio = case.candidate_allocation.risk_handoff.portfolio_id
    plans = {
        fact.result.candidate_allocation.plan_id
        for fact in ledger.candidate_allocation_history(connection, case.access_scope)
        if fact.result.candidate_allocation is not None
        and fact.result.candidate_allocation.beta is not None
        and fact.result.candidate_allocation.risk_handoff is not None
        and fact.result.candidate_allocation.risk_handoff.portfolio_id == portfolio
    }
    latest = {}
    for fact in ledger.candidate_execution_history(connection, case.access_scope, portfolio):
        execution = fact.result.candidate_execution
        assert execution is not None
        if execution.plan_id in plans:
            latest[execution.plan_id] = execution
    acquisitions: dict[tuple[str, str], dict[str, Decimal]] = defaultdict(dict)
    for execution in latest.values():
        for fill in execution.fills:
            if fill.reservation_id is not None and fill.classification != "UNKNOWN":
                key = (fill.account_id, fill.security_id)
                acquisitions[key][fill.entry_id] = (
                    acquisitions[key].get(fill.entry_id, Decimal(0)) + fill.quantity
                )
    total = Decimal(0)
    for unit in snapshot.action_units:
        key = (unit.account_id, unit.security_id)
        if not acquisitions[key]:
            continue
        entries = tuple(
            entry
            for entry in snapshot.authoritative_ledger
            if (entry.account_id, entry.security_id) == key
        )
        remaining = retained_acquired_quantity(
            entries, acquisitions[key], unit.total_quantity or Decimal(0)
        )
        total += remaining * (unit.market_price or Decimal(0))
    return total


def adjudicate_candidate_allocation(
    case: FrozenDecisionCase,
    ledger: DecisionLedger[Transaction],
    connection: Transaction,
    *,
    business_prerequisite_met: bool,
    exclude_confirmation_plan_id: str | None = None,
) -> CandidateAllocationOutcome:
    assert case.candidate_allocation is not None and case.access_scope is not None
    command = case.candidate_allocation
    if command.replaces_plan_event_id is not None:
        original = ledger.get_formal_report_for_event(command.replaces_plan_event_id, connection)
        if (
            original is None
            or original.access_scope is None
            or not case.access_scope.same_scope_as(original.access_scope)
            or original.result.candidate_allocation is None
            or original.result.candidate_allocation.candidate_event_id != command.candidate_event_id
        ):
            return CandidateAllocationOutcome(
                contract_version=command.contract_version,
                disposition="BLOCKED", reasons=("REPLANNING_SOURCE_UNAVAILABLE",)
            )
        if saved_allocation_inputs_changed(case, ledger, connection):
            return CandidateAllocationOutcome(
                contract_version=command.contract_version,
                disposition="BLOCKED",
                reasons=("ALLOCATION_INPUT_VERSION_CONFLICT",),
                candidate_event_id=command.candidate_event_id,
                replaces_plan_event_id=command.replaces_plan_event_id,
                replanning_reason=command.replanning_reason,
            )
        if not entry_window_is_open(
            original.result.candidate_allocation, datetime.fromisoformat(ledger.observed_at())
        ):
            return CandidateAllocationOutcome(
                contract_version=command.contract_version,
                disposition="BLOCKED",
                reasons=("REPLANNING_WINDOW_CLOSED",),
                replaces_plan_event_id=command.replaces_plan_event_id,
                replanning_reason=command.replanning_reason,
            )
    latest = {}
    for fact in ledger.candidate_confirmation_history(
        connection, case.access_scope, case.candidate_allocation.risk_handoff.portfolio_id
    ):
        confirmation = fact.result.candidate_confirmation
        assert confirmation is not None
        latest[confirmation.plan_id] = confirmation
    executions = {}
    for fact in ledger.candidate_execution_history(
        connection, case.access_scope, case.candidate_allocation.risk_handoff.portfolio_id
    ):
        execution = fact.result.candidate_execution
        assert execution is not None
        if (
            execution.plan_id in latest
            and execution.confirmation_id == latest[execution.plan_id].confirmation_id
        ):
            executions[execution.plan_id] = execution
    retained = {
        row.commitment_id: row
        for confirmation in latest.values()
        if confirmation.plan_id != exclude_confirmation_plan_id
        for row in (
            executions[confirmation.plan_id].reservations
            if confirmation.plan_id in executions
            else confirmation.reservations
        )
    }
    for execution in executions.values():
        retained.update((row.commitment_id, row) for row in execution.unassociated_commitments)
    execution_reservation_ids = frozenset(
        row.commitment_id
        for execution in executions.values()
        for row in (*execution.reservations, *execution.unassociated_commitments)
    )
    if any(row.account_id not in case.access_scope.account_ids for row in retained.values()):
        return CandidateAllocationOutcome(
            contract_version=command.contract_version,
            disposition="BLOCKED", reasons=("CONFIRMATION_HISTORY_SCOPE_INCOMPLETE",)
        )
    supplied = {row.commitment_id: row for row in case.candidate_allocation.commitments}
    if any(
        identity in supplied and supplied[identity] != row for identity, row in retained.items()
    ):
        source = ledger.get_formal_report_for_event(
            case.candidate_allocation.candidate_event_id, connection
        )
        if (
            source is None
            or source.access_scope is None
            or not case.access_scope.same_scope_as(source.access_scope)
            or source.result.candidate_release is None
        ):
            return CandidateAllocationOutcome(
                contract_version=command.contract_version,
                disposition="BLOCKED", reasons=("CANDIDATE_HANDOFF_UNAVAILABLE",)
            )
        return _blocked(case, source, "RESERVATION_IDENTITY_CONFLICT")
    command = case.candidate_allocation.model_copy(
        update={"commitments": tuple({**supplied, **retained}.values())}
    )
    case = case.model_copy(update={"candidate_allocation": command})
    assert case.access_scope is not None
    source = ledger.get_formal_report_for_event(command.candidate_event_id, connection)
    if source is not None and source.result.candidate_release is not None:
        if ledger.pending_candidate_executions(
            connection, case.access_scope, command.risk_handoff.portfolio_id
        ):
            return _blocked(case, source, "EXECUTION_PENDING_RECONCILIATION")
        if any(
            execution.disposition == "BLOCKED"
            or bool(execution.reasons)
            or any(order.classification == "UNKNOWN" for order in execution.order_attributions)
            or any(fill.classification == "UNKNOWN" for fill in execution.fills)
            for execution in executions.values()
        ):
            return _blocked(case, source, "EXECUTION_FACTS_UNRESOLVED")
    if source is not None and source.result.candidate_release is not None:
        liquidity_source = ledger.get_formal_report_for_event(
            command.risk_handoff.liquidity_event_id, connection
        )
        for execution in executions.values():
            if execution.disposition == "PENDING_RECONCILIATION":
                return _blocked(case, source, "EXECUTION_PENDING_RECONCILIATION")
            if execution.position_event_id is None:
                continue
            proof = ledger.get_decision_event(execution.position_event_id, connection)
            if (
                proof is None
                or proof.result.position is None
                or liquidity_source is None
                or liquidity_source.result.liquidity is None
                or liquidity_source.result.liquidity.position_snapshot.snapshot
                != proof.result.position.snapshot
                or ledger.get_correction_event(proof.decision_event_id, connection) is not None
            ):
                return _blocked(case, source, "EXECUTION_POSITION_NOT_RECONCILED")
    with localcontext(Context(prec=38)):
        try:
            result = _adjudicate_candidate_allocation(
                case,
                ledger,
                connection,
                business_prerequisite_met=business_prerequisite_met,
                reservation_ids=frozenset(retained),
                execution_reservation_ids=execution_reservation_ids,
            )
            return result.model_copy(
                update={
                    "replaces_plan_event_id": command.replaces_plan_event_id,
                    "replanning_reason": command.replanning_reason,
                }
            )
        except DecimalException:
            assert case.candidate_allocation is not None and case.access_scope is not None
            source = ledger.get_formal_report_for_event(
                case.candidate_allocation.candidate_event_id, connection
            )
            if (
                source is None
                or source.access_scope is None
                or not case.access_scope.same_scope_as(source.access_scope)
                or source.result.candidate_release is None
            ):
                return CandidateAllocationOutcome(
                    contract_version=command.contract_version,
                    disposition="BLOCKED", reasons=("CANDIDATE_HANDOFF_UNAVAILABLE",)
                )
            return _blocked(case, source, "ALLOCATION_OPTIMUM_UNAVAILABLE")


def _blocked(
    case: FrozenDecisionCase,
    source: FormalReport,
    reason: str | tuple[str, ...],
    evidence_ids: tuple[str, ...] = (),
) -> CandidateAllocationOutcome:
    command = case.candidate_allocation
    release = source.result.candidate_release
    assert command is not None and release is not None
    securities = {row.security_id: row for row in command.securities}
    reasons = _ordered_reasons((reason,) if isinstance(reason, str) else reason)
    return CandidateAllocationOutcome(
        contract_version=command.contract_version,
        disposition="BLOCKED",
        reasons=reasons,
        eligibility_evidence_ids=evidence_ids,
        candidate_event_id=command.candidate_event_id,
        candidate_batch_id=release.batch_id,
        candidate_conclusion_version=source.report_version_id,
        formed_at=command.cutoff_at,
        policy=command.policy,
        risk_handoff=command.risk_handoff,
        correlations=command.correlations,
        commitments=command.commitments,
        securities=command.securities,
        routes=command.routes,
        rows=tuple(
            CandidateAllocationRow(
                candidate=member,
                issuer_id=securities[member.security_id].issuer_id
                if member.security_id in securities
                else "UNKNOWN",
                committed_exposure=Decimal(0),
                target_gap=Decimal(0),
                continuous_principal=Decimal(0),
                principal=Decimal(0),
                outcome="UNALLOCATED",
                reasons=reasons,
                primary_reason=reasons[0],
            )
            for member in release.members
            if member.candidate
        ),
    )


def _adjudicate_candidate_allocation(
    case: FrozenDecisionCase,
    ledger: DecisionLedger[Transaction],
    connection: Transaction,
    *,
    business_prerequisite_met: bool,
    reservation_ids: frozenset[str],
    execution_reservation_ids: frozenset[str],
) -> CandidateAllocationOutcome:
    command, scope = case.candidate_allocation, case.access_scope
    assert command is not None and scope is not None
    source = ledger.get_formal_report_for_event(command.candidate_event_id, connection)
    if (
        source is None
        or source.access_scope is None
        or not scope.same_scope_as(source.access_scope)
        or source.result.candidate_release is None
    ):
        return CandidateAllocationOutcome(
            contract_version=command.contract_version,
            disposition="BLOCKED", reasons=("CANDIDATE_HANDOFF_UNAVAILABLE",)
        )
    release = source.result.candidate_release
    candidates = tuple(member for member in release.members if member.candidate)
    securities = {row.security_id: row for row in command.securities}

    if ledger.pending_candidate_confirmations(
        connection, scope, command.risk_handoff.portfolio_id
    ) - {case.business_object_id}:
        return _blocked(case, source, "CONFIRMATION_PENDING_RECONCILIATION")
    if not business_prerequisite_met:
        return _blocked(case, source, "BUSINESS_PREREQUISITE_NOT_MET")
    if command.beta is not None:
        if (
            scope.visibility != "USER"
            or command.beta.scope.market_state != release.market_state
            or command.beta.scope.portfolio_scope != command.risk_handoff.portfolio_id
            or command.beta.scope.user_id != scope.user_id
            or set(command.beta.scope.account_ids) != set(scope.account_ids)
        ):
            return _blocked(case, source, "BETA_SCOPE_MISMATCH")
        beta_reasons = qualification_reasons(
            command.beta,
            ledger.governance_history(connection, scope),
            datetime.fromisoformat(ledger.observed_at()),
            evidence_cutoff=command.cutoff_at,
        )
        if beta_reasons:
            return _blocked(case, source, beta_reasons)
        history_reasons = observation_history_reasons(
            command.beta,
            tuple(
                fact.case.candidate_allocation.beta
                for fact in ledger.candidate_allocation_history(connection, scope)
                if fact.case.candidate_allocation is not None
                and fact.case.candidate_allocation.beta is not None
            ),
            command.cutoff_at,
        )
        if history_reasons:
            return _blocked(case, source, history_reasons)
    if command.policy is None:
        return _blocked(case, source, "ALLOCATION_POLICY_REQUIRED")
    policy = command.policy
    if (
        release.disposition != "CANDIDATES"
        or release.knowledge_cutoff > command.cutoff_at
        or release.published_at > command.cutoff_at
        or any(member.calibrated_probability is None for member in candidates)
    ):
        return _blocked(case, source, "CANDIDATE_HANDOFF_INVALID")
    if len({member.security_id for member in candidates}) != len(candidates):
        return _blocked(case, source, "CANDIDATE_IDENTITIES_AMBIGUOUS")
    original = ledger.get_decision_event(source.event_id, connection)
    if original is None:
        return _blocked(case, source, "CANDIDATE_HANDOFF_EVIDENCE_UNAVAILABLE")
    for event_id, reason in (
        (source.event_id, "CANDIDATE_CONCLUSION_SUPERSEDED"),
        (release.research_event_id, "RESEARCH_EVIDENCE_CORRECTED"),
    ):
        correction = ledger.get_correction_event(event_id, connection)
        if correction is not None:
            return _blocked(case, source, reason, (correction.decision_event_id,))
    eligibility_reasons, eligibility_ids, _ = candidate_qualification_eligibility(
        release,
        original.case,
        ledger.governance_history(connection, scope),
        datetime.fromisoformat(ledger.observed_at())
        if command.replaces_plan_event_id is not None
        else command.cutoff_at,
    )
    if eligibility_reasons:
        return _blocked(case, source, eligibility_reasons, eligibility_ids)
    protection = adjudicate_execution_plan(
        case.model_copy(update={"execution_plan": command.risk_handoff}), ledger, connection
    )
    if (
        protection.new_exposure_blocked
        or protection.disposition == "BLOCKED"
        or any((target.required_sale_quantity or Decimal(0)) > 0 for target in protection.targets)
    ):
        return _blocked(case, source, ("NEW_EXPOSURE_BLOCKED", *protection.reasons))
    reports = tuple(
        ledger.get_formal_report_for_event(event, connection)
        for event in (
            command.risk_handoff.liquidity_event_id,
            command.risk_handoff.stress_event_id,
            command.risk_handoff.drawdown_event_id,
        )
    )
    liquidity_report, stress_report, drawdown_report = reports
    assert liquidity_report and stress_report and drawdown_report
    liquidity, stress, drawdown = (
        liquidity_report.result.liquidity,
        stress_report.result.stress,
        drawdown_report.result.drawdown,
    )
    assert liquidity and stress and drawdown and drawdown.state
    concentration_report = ledger.get_formal_report_for_event(
        command.risk_handoff.concentration_event_id, connection
    )
    assert concentration_report and concentration_report.result.concentration
    concentration_history = ledger.concentration_history(
        connection, scope, command.risk_handoff.portfolio_id
    )
    stress_history = ledger.portfolio_stress_history(
        connection, scope, command.risk_handoff.portfolio_id
    )
    liquidity_history = ledger.liquidity_history(
        connection, scope, command.risk_handoff.portfolio_id, command.cutoff_at
    )
    capital_history = tuple(
        item
        for item in ledger.drawdown_history(connection, scope)
        if item.state is not None and item.state.portfolio_id == command.risk_handoff.portfolio_id
    )
    if (
        concentration_history.uncovered_obligation
        or not concentration_history.outcomes
        or concentration_history.outcomes[-1] != concentration_report.result.concentration
        or not stress_history
        or stress_history[-1] != stress
        or not liquidity_history
        or liquidity_history[-1] != liquidity
        or not capital_history
        or capital_history[-1] != drawdown
    ):
        return _blocked(case, source, "RISK_HANDOFF_SUPERSEDED")
    authorization = liquidity.purchase_authorization
    if not authorization or not authorization.usage or not authorization.usage.allowed:
        return _blocked(case, source, "ACTIVE_RISK_BUDGET_REQUIRED")
    usage = authorization.usage
    budget = usage.authorization_snapshot.proposal.risk_budget
    history = ledger.portfolio_authorization_history(
        connection, scope, command.risk_handoff.portfolio_id, command.cutoff_at.isoformat()
    )
    approved = [outcome.authorization for outcome in history if outcome.authorization is not None]
    if (
        not approved
        or approved[-1].authorization_id != command.risk_handoff.authorization_id
        or not budget.effective_at <= command.cutoff_at < budget.expires_at
        or stress.risk_budget_version_id != budget.version_id
        or drawdown.state.risk_state != "NORMAL"
        or stress.obligation is not None
        and stress.obligation.status == "OUTSTANDING"
        or stress.gross_stress_loss is None
        or stress.calculation_policy is None
    ):
        return _blocked(case, source, "NEW_EXPOSURE_BLOCKED")
    snapshot = liquidity.position_snapshot.snapshot
    if command.beta is not None and {row.account_type for row in snapshot.cash_states} != {
        command.beta.scope.account_type
    }:
        return _blocked(case, source, "BETA_ACCOUNT_TYPE_MISMATCH")
    equity, cash = liquidity.net_liquidation_equity, liquidity.deployable_purchase_cash
    assert equity is not None and cash is not None
    liquidity_fact = ledger.get_original_decision_event(
        liquidity_report.business_object_id, connection
    )
    if liquidity_fact is None or liquidity_fact.case.liquidity is None:
        return _blocked(case, source, "LIQUIDITY_COST_BINDING_UNAVAILABLE")
    # Replace the prior aggregate estimate with this plan's actual full route costs.
    cash += liquidity_fact.case.liquidity.expected_purchase_fees
    if equity <= 0 or set(scope.account_ids) != {row.account_id for row in snapshot.cash_states}:
        return _blocked(case, source, "COMPLETE_PORTFOLIO_REQUIRED")
    if any(row.current_market_exposure is None for row in snapshot.issuer_exposures):
        return _blocked(case, source, "COMMITTED_EXPOSURE_UNKNOWN")
    existing: dict[str, Decimal] = defaultdict(Decimal)
    held_issuers = {unit.security_id: unit.issuer_id for unit in snapshot.action_units}
    for row in snapshot.issuer_exposures:
        assert row.current_market_exposure is not None
        existing[row.issuer_id] += row.current_market_exposure
    account_cash = {row.account_id: row.trading_cash or Decimal(0) for row in snapshot.cash_states}
    for obligation in liquidity.obligation_funding:
        account_cash[obligation.target_account_id] -= obligation.required_cash
    commitment_stress = Decimal(0)
    broker_orders = {
        (order.account_id, order.order_id): order
        for order in snapshot.unfinished_orders
        if order.side == "BUY"
    }
    supplied_orders = {key for row in command.commitments for key in row.order_keys}
    if set(broker_orders) != supplied_orders:
        return _blocked(case, source, "BUY_COMMITMENTS_INCOMPLETE")
    security_committed: dict[str, Decimal] = defaultdict(Decimal)
    known_issuers = {security.security_id: security.issuer_id for security in command.securities}
    for commitment in command.commitments:
        reconciled = commitment.commitment_id in execution_reservation_ids
        if (
            commitment.security_id in held_issuers
            and held_issuers[commitment.security_id] != commitment.issuer_id
        ) or (
            commitment.security_id in known_issuers
            and known_issuers[commitment.security_id] != commitment.issuer_id
        ):
            return _blocked(case, source, "BUY_COMMITMENT_IDENTITY_MISMATCH")
        known_issuers[commitment.security_id] = commitment.issuer_id
        cash_only = (
            reconciled
            and commitment.principal == 0
            and commitment.quantity is None
            and commitment.reconciliation_cash_hold > 0
        )
        if not cash_only and (
            commitment.quantity is None
            or commitment.price_cap is None
            or commitment.principal != commitment.quantity * commitment.price_cap
        ):
            return _blocked(case, source, "BUY_COMMITMENT_PRICE_BASIS_UNKNOWN")
        if commitment.account_id not in account_cash or commitment.evidence.problem_codes(
            command.cutoff_at,
            require_current_completeness=commitment.commitment_id not in reservation_ids,
        ):
            return _blocked(case, source, "BUY_COMMITMENT_EVIDENCE_FAILED")
        keys = commitment.order_keys
        frozen_cash = Decimal(0)
        own_account_frozen_cash = Decimal(0)
        broker_quantity = Decimal(0)
        if commitment.broker_order_bindings and not reconciled:
            return _blocked(case, source, "UNPROVEN_ORDER_MERGE")
        for key in keys:
            order = broker_orders[key]
            if (
                order.security_id != commitment.security_id
                or order.remaining_quantity is None
                or order.evidence.problem_codes(
                    command.cutoff_at, require_current_completeness=True
                )
            ):
                return _blocked(case, source, "BUY_COMMITMENT_IDENTITY_MISMATCH")
            if (
                order.reserved_cash is None
                or order.reserved_cash_semantics != "BROKER_FINAL_RESERVED_CASH"
            ):
                return _blocked(case, source, "BUY_COMMITMENT_RESERVATION_INCOMPLETE")
            broker_quantity += order.remaining_quantity
            frozen_cash += order.reserved_cash
            if order.account_id == commitment.account_id:
                own_account_frozen_cash += order.reserved_cash
        if (
            keys
            and not reconciled
            and (
                broker_quantity != commitment.quantity
                or frozen_cash < commitment.principal + commitment.purchase_cost
            )
        ):
            return _blocked(case, source, "BUY_COMMITMENT_RESERVATION_INCOMPLETE")
        if keys and reconciled and broker_quantity > (commitment.quantity or Decimal(0)):
            return _blocked(case, source, "EXECUTION_FACTS_UNRESOLVED")
        deduction = max(
            Decimal(0),
            commitment.principal
            + commitment.purchase_cost
            + commitment.reconciliation_cash_hold
            - frozen_cash,
        )
        account_cash[commitment.account_id] -= max(
            Decimal(0),
            commitment.principal
            + commitment.purchase_cost
            + commitment.reconciliation_cash_hold
            - own_account_frozen_cash,
        )
        cash -= deduction
        existing[commitment.issuer_id] += commitment.principal
        security_committed[commitment.security_id] += commitment.principal
        commitment_stress += (
            commitment.principal * stress.calculation_policy.shock_ratio
            + commitment.disposal_friction
        )
    stress_remaining = (
        equity * budget.stress.target_ratio - stress.gross_stress_loss - commitment_stress
    )
    if stress_remaining < 0 or cash < 0:
        return _blocked(case, source, "COMMITTED_PROTECTION_CAPACITY_EXCEEDED")

    issuer_ids = tuple(
        held_issuers.get(member.security_id, securities[member.security_id].issuer_id)
        if member.security_id in securities
        else "UNKNOWN"
        for member in candidates
    )
    reasons: list[list[str]] = [[] for _ in candidates]
    targets = tuple(
        max(Decimal(0), equity * policy.entry_target_ratio - existing[issuer])
        for issuer in issuer_ids
    )
    capacities: list[Capacity] = []
    for issuer in sorted(set(issuer_ids)):
        indices = tuple(index for index, identity in enumerate(issuer_ids) if identity == issuer)
        capacities.extend(
            (
                Capacity(indices, targets[indices[0]], "ENTRY_TARGET_REACHED"),
                Capacity(
                    indices,
                    equity * budget.concentration.target_ratio - existing[issuer],
                    "ISSUER_CAPACITY_EXHAUSTED",
                ),
            )
        )
    correlations = command.correlations
    committed_issuers = {issuer for issuer, exposure in existing.items() if exposure > 0}
    relevant = set(issuer_ids) | committed_issuers
    valid_series: dict[str, tuple[Decimal, ...]] = {}
    if correlations is not None and not correlations.evidence.problem_codes(
        command.cutoff_at, require_current_completeness=True
    ):
        dates = correlations.market_dates
        calendar = synthetic_market_calendar(correlations.market_calendar_version)
        expected_dates = (
            tuple(
                session.closed_at.date()
                for session in calendar.recent_completed_sessions(
                    command.cutoff_at, policy.correlation_window
                )
            )
            if calendar is not None
            else ()
        )
        if (
            len(dates) == policy.correlation_window
            and dates == expected_dates
            and correlations.market_calendar_version == release.market_calendar_version
        ):
            for issuer in relevant:
                series = correlations.returns.get(issuer)
                if series is not None and len(series) == len(dates) and len(set(series)) > 1:
                    valid_series[issuer] = series
    neighborhoods: dict[str, set[str]] = {}
    for issuer in sorted(set(issuer_ids)):
        neighborhood = {issuer}
        if issuer in valid_series:
            for other in sorted(relevant):
                if other in valid_series and _correlated(
                    valid_series[issuer], valid_series[other], policy.correlation_ceiling
                ):
                    neighborhood.add(other)
        neighborhoods[issuer] = neighborhood
        capacities.append(
            Capacity(
                tuple(
                    index for index, identity in enumerate(issuer_ids) if identity in neighborhood
                ),
                equity * policy.neighborhood_ratio
                - sum((existing[identity] for identity in neighborhood), Decimal(0)),
                "CORRELATION_CAPACITY_EXHAUSTED",
            )
        )
    for index, member in enumerate(candidates):
        security = securities.get(member.security_id)
        if security is not None and security.issuer_id != issuer_ids[index]:
            reasons[index].append("ISSUER_IDENTITY_MISMATCH")
        if (
            not member.valid_market_dates
            or member.valid_market_dates[-1] < command.cutoff_at.date()
        ):
            reasons[index].append("ENTRY_WINDOW_EXPIRED")
        if security is None or security.evidence.problem_codes(
            command.cutoff_at, require_current_completeness=True
        ):
            reasons[index].append("MARKET_CAPACITY_EVIDENCE_FAILED")
        if not (committed_issuers | {issuer_ids[index]}).issubset(valid_series):
            reasons[index].append("CORRELATION_EVIDENCE_FAILED")
        if targets[index] == 0:
            reasons[index].append("ENTRY_TARGET_REACHED")
        capacities.append(
            Capacity(
                (index,),
                security.median_turnover * policy.turnover_ratio
                - security_committed[member.security_id]
                if security
                else Decimal(0),
                "LIQUIDITY_CAPACITY_EXHAUSTED",
            )
        )
        if reasons[index]:
            capacities.append(Capacity((index,), Decimal(0), reasons[index][0]))

    routes = []
    route_members = []
    route_failures: dict[int, list[AllocationRouteFailure]] = defaultdict(list)
    awaiting = False
    for index, member in enumerate(candidates):
        eligible = []
        for route in sorted(command.routes, key=lambda row: (row.security_id, row.account_id)):
            if route.security_id != member.security_id:
                continue
            failures = []
            if route.account_id not in account_cash:
                failures.append("ACCOUNT_SCOPE_MISMATCH")
            if not route.permission:
                failures.append("ACCOUNT_PERMISSION_DENIED")
            if route.evidence.problem_codes(command.cutoff_at, require_current_completeness=True):
                failures.append("BUY_ROUTE_EVIDENCE_FAILED")
            if any(
                restriction.active
                and restriction.account_id == route.account_id
                and restriction.security_id in {None, route.security_id}
                for restriction in snapshot.execution_restrictions
            ):
                failures.append("ACCOUNT_EXECUTION_RESTRICTED")
            if route.current_price is None:
                failures.append("CURRENT_PRICE_UNAVAILABLE")
            elif not route.minimum_price <= route.current_price <= route.maximum_price:
                failures.append("CURRENT_PRICE_OUT_OF_RANGE")
            if route.price_cap is not None and route.price_cap_confirmed:
                if not route.minimum_price <= route.price_cap <= route.maximum_price:
                    failures.append("PRICE_CAP_OUT_OF_RANGE")
                if route.price_cap % route.price_tick != 0:
                    failures.append("PRICE_TICK_INVALID")
            if failures:
                route_failures[index].append(
                    AllocationRouteFailure(
                        route=route,
                        reasons=_ordered_reasons(failures),
                    )
                )
                continue
            if route.price_cap is None or not route.price_cap_confirmed:
                awaiting = True
                route = route.model_copy(update={"price_cap": route.current_price})
            eligible.append(route)
        if not eligible:
            reasons[index].extend(
                reason for failure in route_failures[index] for reason in failure.reasons
            )
            if not route_failures[index]:
                reasons[index].append("BUY_ROUTE_UNAVAILABLE")
        routes.extend(eligible)
        route_members.extend([index] * len(eligible))
    problem = AllocationProblem(
        routes=tuple(routes),
        route_members=tuple(route_members),
        issuers=issuer_ids,
        existing=tuple(existing[issuer] for issuer in issuer_ids),
        targets=targets,
        probabilities=tuple(member.calibrated_probability or Decimal(0) for member in candidates),
        account_cash=account_cash,
        cash=cash,
        stress_remaining=stress_remaining,
        shock=stress.calculation_policy.shock_ratio,
        capacities=tuple(capacities),
    )
    beta_outcome = None
    try:
        if command.beta is not None:
            normal_continuous = continuous_allocation(problem)
            if awaiting:
                normal_capacity = sum(normal_continuous.principals, Decimal(0))
            else:
                normal_discrete = discrete_allocation(problem, normal_continuous.principals)
                normal_capacity = sum(
                    (
                        quantity * (route.price_cap or Decimal(0))
                        for route, quantity in zip(routes, normal_discrete.quantities, strict=True)
                    ),
                    Decimal(0),
                )
            committed_beta = sum(
                (item.principal for item in command.commitments), Decimal(0)
            ) + _held_beta_exposure(case, snapshot, ledger, connection)
            envelope, beta_reasons, operations, enabled = envelope_decision(
                command.beta,
                datetime.fromisoformat(ledger.observed_at()),
                ledger.governance_history(connection, scope),
                evidence_cutoff=command.cutoff_at,
            )
            ratio = (
                command.beta.policy.expanded_ratio
                if envelope == "EXPANDED"
                else command.beta.policy.initial_ratio
            )
            beta_capacity = (
                min(max(Decimal(0), equity * ratio - committed_beta), normal_capacity)
                if enabled
                else Decimal(0)
            )
            beta_outcome = BetaEnvelopeOutcome(
                activation_id=command.beta.activation_id,
                permission_envelope=envelope,
                reasons=beta_reasons,
                operations=operations,
                ratio=ratio,
                net_liquidation_equity=equity,
                normal_feasible_capacity=normal_capacity,
                committed_exposure=committed_beta,
                capacity=beta_capacity,
                qualification_decision_ids=tuple(
                    item.decision_id for item in command.beta.qualification_bindings
                ),
            )
            capacities.append(
                Capacity(tuple(range(len(candidates))), beta_capacity, "BETA_CAPACITY_EXHAUSTED")
            )
            problem = replace(problem, capacities=tuple(capacities))
        continuous_solution = continuous_allocation(problem)
        continuous = continuous_solution.principals
        if not capacity_is_feasible(problem, continuous, continuous_solution.route_principals):
            return _blocked(case, source, "ALLOCATION_CAPACITY_VERIFICATION_FAILED")
        discrete = (
            DiscreteAllocation(tuple(Decimal(0) for _ in routes), (), {}, frozenset())
            if awaiting
            else discrete_allocation(problem, continuous)
        )
        quantities = discrete.quantities
    except AllocationSolveFailed:
        return _blocked(case, source, "ALLOCATION_OPTIMUM_UNAVAILABLE")
    legs_by_member: dict[int, list[AcquisitionLeg]] = defaultdict(list)
    for route, member_index, quantity in zip(routes, route_members, quantities, strict=True):
        if quantity == 0:
            continue
        assert route.price_cap is not None
        principal = quantity * route.price_cap
        if (
            quantity < route.minimum_quantity
            or (quantity - route.minimum_quantity) % route.quantity_increment != 0
        ):
            return _blocked(case, source, "ALLOCATION_QUANTITY_VERIFICATION_FAILED")
        legs_by_member[member_index].append(
            AcquisitionLeg(
                route=route,
                quantity=quantity,
                principal=principal,
                purchase_cost=route.cost(principal),
            )
        )
    principals = tuple(
        sum((leg.principal for leg in legs_by_member[index]), Decimal(0))
        for index in range(len(candidates))
    )
    total = sum(principals, Decimal(0))
    costs = sum((leg.purchase_cost for legs in legs_by_member.values() for leg in legs), Decimal(0))
    if (
        total > sum(continuous, Decimal(0))
        or any(
            principal > discrete_principal_ceiling(problem, continuous, index, quantities)
            for index, principal in enumerate(principals)
        )
        or not capacity_is_feasible(
            problem,
            principals,
            tuple(
                quantity * (route.price_cap or Decimal(0))
                for route, quantity in zip(routes, quantities, strict=True)
            ),
        )
    ):
        return _blocked(case, source, "ALLOCATION_CAPACITY_VERIFICATION_FAILED")
    capacity_checks = []
    all_security_ids = tuple(member.security_id for member in candidates)
    continuous_cost = sum(
        (
            route.cost(amount)
            for route, amount in zip(routes, continuous_solution.route_principals, strict=True)
        ),
        Decimal(0),
    )
    continuous_stress = sum(
        (
            amount * (problem.shock + route.disposal_friction_ratio)
            for route, amount in zip(routes, continuous_solution.route_principals, strict=True)
        ),
        Decimal(0),
    )
    planned_stress = sum(
        (
            leg.principal * (problem.shock + leg.route.disposal_friction_ratio)
            for legs in legs_by_member.values()
            for leg in legs
        ),
        Decimal(0),
    )
    for gate_id, reason, margin, used_continuous, used_plan in (
        (
            "global:cash",
            "CASH_CAPACITY_EXHAUSTED",
            cash,
            sum(continuous, Decimal(0)) + continuous_cost,
            total + costs,
        ),
        (
            "global:stress",
            "STRESS_CAPACITY_EXHAUSTED",
            stress_remaining,
            continuous_stress,
            planned_stress,
        ),
    ):
        capacity_checks.append(
            AllocationCapacityCheck(
                gate_id=gate_id,
                reason=reason,
                security_ids=all_security_ids,
                committed_margin=margin,
                available_before=max(Decimal(0), margin),
                remaining_after_continuous=max(Decimal(0), margin) - used_continuous,
                remaining_after_plan=max(Decimal(0), margin) - used_plan,
            )
        )
    for index, capacity in enumerate(capacities):
        capacity_checks.append(
            AllocationCapacityCheck(
                gate_id=f"capacity:{index}",
                reason=capacity.reason,
                security_ids=tuple(candidates[member].security_id for member in capacity.members),
                committed_margin=capacity.remaining,
                available_before=max(Decimal(0), capacity.remaining),
                remaining_after_continuous=max(Decimal(0), capacity.remaining)
                - sum((continuous[member] for member in capacity.members), Decimal(0)),
                remaining_after_plan=max(Decimal(0), capacity.remaining)
                - sum((principals[member] for member in capacity.members), Decimal(0)),
            )
        )
    for account, available in sorted(account_cash.items()):
        capacity_checks.append(
            AllocationCapacityCheck(
                gate_id=f"account:{account}",
                reason="ACCOUNT_CASH_CAPACITY_EXHAUSTED",
                security_ids=tuple(
                    sorted({route.security_id for route in routes if route.account_id == account})
                ),
                account_id=account,
                committed_margin=available,
                available_before=available,
                remaining_after_continuous=available
                - sum(
                    (
                        amount + route.cost(amount)
                        for route, amount in zip(
                            routes, continuous_solution.route_principals, strict=True
                        )
                        if route.account_id == account
                    ),
                    Decimal(0),
                ),
                remaining_after_plan=available
                - sum(
                    (
                        leg.principal + leg.purchase_cost
                        for legs in legs_by_member.values()
                        for leg in legs
                        if leg.route.account_id == account
                    ),
                    Decimal(0),
                ),
            )
        )
    rows = []
    for index, member in enumerate(candidates):
        principal = principals[index]
        issuer_members = tuple(
            identity for identity, issuer in enumerate(issuer_ids) if issuer == issuer_ids[index]
        )
        issuer_continuous = sum((continuous[identity] for identity in issuer_members), Decimal(0))
        issuer_principal = sum((principals[identity] for identity in issuer_members), Decimal(0))
        member_routes = [
            route
            for route, identity in zip(routes, route_members, strict=True)
            if identity == index
        ]
        unit = min(
            (
                leg.route.minimum_quantity * (leg.route.price_cap or Decimal(0))
                for leg in legs_by_member[index]
            ),
            default=Decimal(0),
        )
        row_reasons = list(reasons[index])
        if issuer_continuous < targets[index]:
            # A minimum commission can close positive capacity before any cash is
            # consumed. Preserve that cash gate rather than publishing an unexplained zero.
            if member_routes and all(route.minimum_commission >= cash for route in member_routes):
                row_reasons.append("CASH_CAPACITY_EXHAUSTED")
            if member_routes and all(
                route.minimum_commission >= account_cash[route.account_id]
                for route in member_routes
            ):
                row_reasons.append("ACCOUNT_CASH_CAPACITY_EXHAUSTED")
            for check in capacity_checks:
                if (
                    member.security_id in check.security_ids
                    and check.reason != "ENTRY_TARGET_REACHED"
                    and _capacity_binding(problem, check)
                ):
                    row_reasons.append(check.reason)
        if awaiting:
            row_reasons.append("PRICE_CAP_REQUIRED")
        elif continuous[index] > 0 and principal == 0:
            row_reasons.append(
                "BELOW_MINIMUM_BUY_UNIT"
                if index in discrete.below_minimum
                else "UNALLOCATED_CAPACITY_PRIORITY"
            )
        elif principal < continuous[index]:
            row_reasons.append("ROUNDING_REMAINDER_UNUSABLE")
        ordered_reasons = _ordered_reasons(row_reasons)
        full = (
            principal > 0
            and not any(reason.endswith("CAPACITY_EXHAUSTED") for reason in row_reasons)
            and targets[index] - issuer_principal < unit
        )
        rows.append(
            CandidateAllocationRow(
                candidate=member,
                issuer_id=issuer_ids[index],
                committed_exposure=existing[issuer_ids[index]],
                target_gap=targets[index],
                continuous_principal=continuous[index],
                principal=principal,
                outcome="FULLY_ALLOCATED"
                if full
                else "PARTIALLY_ALLOCATED"
                if principal > 0
                else "UNALLOCATED",
                reasons=ordered_reasons,
                primary_reason=ordered_reasons[0] if ordered_reasons else None,
                comparisons=discrete.comparisons.get(index, ()),
                route_failures=tuple(route_failures[index]),
                legs=tuple(legs_by_member[index]),
            )
        )
    ordered = sorted(
        (index for index, principal in enumerate(principals) if principal > 0),
        key=lambda index: (
            candidates[index].valid_market_dates[-1],
            existing[issuer_ids[index]],
            sum((existing[issuer] for issuer in neighborhoods[issuer_ids[index]]), Decimal(0)),
            -(candidates[index].calibrated_probability or Decimal(0)),
            sum((leg.purchase_cost for leg in legs_by_member[index]), Decimal(0))
            / principals[index],
            candidates[index].security_id,
        ),
    )
    return CandidateAllocationOutcome(
        beta=beta_outcome,
        contract_version=command.contract_version,
        disposition="AWAITING_PRICE_CAP" if awaiting else "PLANNED",
        reasons=("PRICE_CAP_REQUIRED",) if awaiting else (),
        rows=tuple(rows),
        plan_id=case.decision_event_id,
        formed_at=command.cutoff_at,
        candidate_event_id=command.candidate_event_id,
        candidate_batch_id=release.batch_id,
        candidate_conclusion_version=source.report_version_id,
        policy=policy,
        position_snapshot_id=snapshot.snapshot_id,
        risk_budget_version_id=budget.version_id,
        position_snapshot=snapshot,
        risk_budget=budget,
        total_principal=total,
        remaining_cash=cash - total - costs,
        risk_handoff=command.risk_handoff,
        correlations=command.correlations,
        commitments=command.commitments,
        securities=command.securities,
        routes=command.routes,
        purchase_sequence=tuple(candidates[index].security_id for index in ordered),
        eligibility_evidence_ids=eligibility_ids,
        discrete_objectives=discrete.objectives,
        capacity_checks=tuple(capacity_checks),
    )
