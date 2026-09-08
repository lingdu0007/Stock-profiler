"""Resolve immutable scoped risk handoffs before forming a host-owned plan."""

from decimal import Context, Decimal, localcontext

from stock_profiler.modules.decision_cases.domain import FrozenDecisionCase
from stock_profiler.modules.decision_cases.ports import DecisionLedger, Transaction
from stock_profiler.modules.position_management.execution_contracts import (
    ExecutionPlanOutcome,
    ExecutionTarget,
)
from stock_profiler.modules.position_management.execution_waterfall import conjoin_portfolio_targets


def adjudicate_execution_plan(
    case: FrozenDecisionCase,
    ledger: DecisionLedger[Transaction],
    connection: Transaction,
) -> ExecutionPlanOutcome:
    command = case.execution_plan
    scope = case.access_scope
    assert command is not None and scope is not None
    history = ledger.execution_plan_history(connection, scope, command.portfolio_id)
    retained: dict[str, ExecutionTarget] = {}
    for fact in history:
        prior_scope = fact.case.access_scope
        prior_plan = fact.result.execution_plan
        assert prior_scope is not None and prior_plan is not None
        if set(prior_scope.account_ids).issubset(scope.account_ids):
            for target in prior_plan.targets:
                if (
                    target.security_id not in retained
                    or target.target_quantity < retained[target.security_id].target_quantity
                ):
                    retained[target.security_id] = target.model_copy(
                        update={
                            "required_sale_quantity": None,
                            "remaining_quantity": None,
                            "remaining_gap": None,
                            "rounding_induced_full_sale": False,
                        }
                    )
    missing = ExecutionPlanOutcome(
        disposition="BLOCKED",
        reasons=("RISK_HANDOFF_UNAVAILABLE",),
        new_exposure_blocked=True,
        targets=tuple(retained[key] for key in sorted(retained)),
    )
    reports = tuple(
        ledger.get_formal_report_for_event(event_id, connection)
        for event_id in (
            command.concentration_event_id,
            command.stress_event_id,
            command.liquidity_event_id,
            command.drawdown_event_id,
        )
    )
    candidate = reports[0]
    if (
        candidate is not None
        and candidate.access_scope is not None
        and scope.same_scope_as(candidate.access_scope)
        and candidate.result.concentration is not None
        and candidate.result.concentration.portfolio_id == command.portfolio_id
        and candidate.result.concentration.cutoff_at == command.cutoff_at
    ):
        for issuer in candidate.result.concentration.issuers:
            if issuer.obligation_id is not None and issuer.state != "RESOLVED":
                for cap in issuer.targets:
                    prior = retained.get(cap.security_id)
                    if prior is None or cap.target_quantity < prior.target_quantity:
                        retained[cap.security_id] = ExecutionTarget(
                            security_id=cap.security_id,
                            target_quantity=cap.target_quantity,
                            required_sale_quantity=None,
                            source_obligation_ids=(issuer.obligation_id,),
                            direction="EXIT" if cap.target_quantity == 0 else "REDUCE",
                        )
    missing = missing.model_copy(
        update={"targets": tuple(retained[key] for key in sorted(retained))}
    )
    if any(
        report is None
        or report.access_scope is None
        or not scope.same_scope_as(report.access_scope)
        for report in reports
    ):
        return missing
    concentration_report, stress_report, liquidity_report, drawdown_report = reports
    assert concentration_report and stress_report and liquidity_report and drawdown_report
    liquidity_fact = ledger.get_original_decision_event(
        liquidity_report.business_object_id, connection
    )
    if liquidity_fact is None or liquidity_fact.case.liquidity is None:
        return missing
    concentration = concentration_report.result.concentration
    stress = stress_report.result.stress
    liquidity = liquidity_report.result.liquidity
    drawdown = drawdown_report.result.drawdown
    if concentration is None or stress is None or liquidity is None or drawdown is None:
        return missing
    position = concentration_report.result.position
    if position is None or drawdown.state is None:
        return missing
    snapshot = position.snapshot
    state = drawdown.state
    capital_position = ledger.position_evidence_for_drawdown(
        connection, scope, state.valuation.position_event_id, command.cutoff_at
    )
    if (
        concentration.portfolio_id != command.portfolio_id
        or stress.portfolio_id != command.portfolio_id
        or state.portfolio_id != command.portfolio_id
        or stress.authorization_id != command.authorization_id
        or liquidity.authorization_id != command.authorization_id
        or state.authorization_id != command.authorization_id
        or concentration.cutoff_at != command.cutoff_at
        or stress.cutoff_at != command.cutoff_at
        or state.cutoff_at != command.cutoff_at
        or snapshot.cutoff_at != command.cutoff_at
        or concentration.snapshot_id != snapshot.snapshot_id
        or stress.snapshot_id != snapshot.snapshot_id
        or liquidity.position_snapshot != position
        or capital_position != position
        or concentration.risk_budget_version_id != stress.risk_budget_version_id
    ):
        return missing.model_copy(update={"reasons": ("RISK_HANDOFF_IDENTITY_MISMATCH",)})
    if (
        concentration.disposition != "ASSESSED"
        or stress.state == "UNKNOWN"
        or drawdown.disposition != "ACCEPTED"
        or liquidity.disposition == "EVIDENCE_FAILED"
    ):
        return missing.model_copy(update={"reasons": ("RISK_HANDOFF_EVIDENCE_FAILED",)})
    with localcontext(Context(prec=38)):
        quantities: dict[str, Decimal] = {}
        for unit in snapshot.action_units:
            if unit.total_quantity is None:
                return missing.model_copy(update={"reasons": ("POSITION_QUANTITY_UNKNOWN",)})
            quantities[unit.security_id] = (
                quantities.get(unit.security_id, Decimal(0)) + unit.total_quantity
            )
        caps = dict(quantities)
        obligations: dict[str, list[str]] = {security: [] for security in quantities}
        for issuer in concentration.issuers:
            if issuer.obligation_id is not None and issuer.state != "RESOLVED":
                for concentration_target in issuer.targets:
                    caps[concentration_target.security_id] = min(
                        caps[concentration_target.security_id], concentration_target.target_quantity
                    )
                    obligations[concentration_target.security_id].append(issuer.obligation_id)
        for established in command.established_targets:
            if not established.qualified or established.evidence.problem_codes(
                command.cutoff_at, require_current_completeness=True
            ):
                continue
            if established.security_id not in quantities:
                return missing.model_copy(update={"reasons": ("TARGET_SCOPE_INVALID",)})
            caps[established.security_id] = min(
                caps[established.security_id], established.target_quantity
            )
            obligations[established.security_id].append(established.target_id)
        if state.risk_state == "PRESERVATION":
            for security in caps:
                caps[security] = Decimal(0)
                obligations[security].append(state.decision_id)
        for fact in history:
            prior_scope = fact.case.access_scope
            prior_command = fact.case.execution_plan
            prior_plan = fact.result.execution_plan
            assert prior_scope is not None and prior_command is not None and prior_plan is not None
            if not set(prior_scope.account_ids).issubset(scope.account_ids):
                return missing.model_copy(
                    update={"reasons": ("EXECUTION_HISTORY_SCOPE_INCOMPLETE",)}
                )
            if prior_command.cutoff_at > command.cutoff_at:
                return missing.model_copy(update={"reasons": ("EXECUTION_SNAPSHOT_NOT_FORWARD",)})
            for prior_target in prior_plan.targets:
                security = prior_target.security_id
                if security in caps:
                    caps[security] = min(caps[security], prior_target.target_quantity)
                    obligations[security].extend(prior_target.source_obligation_ids)
        targets = tuple(
            ExecutionTarget(
                security_id=security,
                target_quantity=caps[security],
                required_sale_quantity=max(Decimal(0), quantities[security] - caps[security]),
                source_obligation_ids=tuple(obligations[security]),
                direction="EXIT"
                if caps[security] == 0
                else "REDUCE"
                if caps[security] < quantities[security]
                else "HOLD",
            )
            for security in sorted(quantities)
        )
        return conjoin_portfolio_targets(
            command, snapshot, targets, stress, liquidity, state, liquidity_fact.case.liquidity
        )
