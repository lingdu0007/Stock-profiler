"""Compose committed security and personal-risk evidence at the host boundary."""

import json
from decimal import ROUND_CEILING, Context, Decimal, localcontext
from hashlib import sha256
from typing import Literal

from stock_profiler.modules.decision_cases.domain import FrozenDecisionCase
from stock_profiler.modules.decision_cases.execution_plans import adjudicate_execution_plan
from stock_profiler.modules.decision_cases.ports import DecisionLedger, Transaction
from stock_profiler.modules.position_management.action_contracts import (
    PositionActionOutcome,
    StatisticalReductionAnchor,
)
from stock_profiler.modules.position_management.execution_contracts import (
    EstablishedQuantityTarget,
    ExecutionPlanOutcome,
    ExecutionTarget,
)
from stock_profiler.modules.position_management.exit_contracts import SecurityAssessment
from stock_profiler.modules.qualification.service import (
    current_qualification,
    qualification_is_current,
)


def adjudicate_position_action(
    case: FrozenDecisionCase,
    ledger: DecisionLedger[Transaction],
    connection: Transaction,
    *,
    business_prerequisite_met: bool,
) -> PositionActionOutcome:
    with localcontext(Context(prec=38)):
        return _adjudicate(case, ledger, connection, business_prerequisite_met)


def _adjudicate(
    case: FrozenDecisionCase,
    ledger: DecisionLedger[Transaction],
    connection: Transaction,
    business_prerequisite_met: bool,
) -> PositionActionOutcome:
    command = case.position_action
    scope = case.access_scope
    assert command is not None and scope is not None
    missing = PositionActionOutcome(
        disposition="BLOCKED",
        security_id=command.security_id,
        lifecycle_id=command.lifecycle_id,
        reasons=("ACTION_HANDOFF_UNAVAILABLE",),
    )
    base_plan = adjudicate_execution_plan(
        case.model_copy(update={"execution_plan": command.execution}), ledger, connection
    )
    independent_target = next(
        (
            t
            for t in base_plan.targets
            if t.security_id == command.security_id and t.source_obligation_ids
        ),
        None,
    )
    if independent_target is not None:
        missing = missing.model_copy(
            update={
                "disposition": "CONCLUDED",
                "direction": "EXIT" if independent_target.target_quantity == 0 else "REDUCE",
                "target_quantity": independent_target.target_quantity,
                "execution": base_plan.model_copy(update={"disposition": "BLOCKED"}),
            }
        )
    history = ledger.position_action_history(
        connection, scope, command.execution.portfolio_id, command.lifecycle_id
    )
    for fact in history:
        prior = fact.case.position_action
        assert prior is not None and fact.case.access_scope is not None
        if prior.security_id != command.security_id:
            return missing.model_copy(update={"reasons": ("ACTION_HISTORY_SECURITY_MISMATCH",)})
        if not scope.same_scope_as(fact.case.access_scope):
            return missing.model_copy(update={"reasons": ("ACTION_HISTORY_SCOPE_INCOMPLETE",)})
        if prior.cutoff_at > command.cutoff_at:
            return missing.model_copy(update={"reasons": ("ACTION_SNAPSHOT_NOT_FORWARD",)})
    prior_anchor = next(
        (
            fact.result.position_action.anchor
            for fact in history
            if fact.result.position_action is not None
            and fact.result.position_action.anchor is not None
        ),
        None,
    )
    prior_cost_anchor = next(
        (
            fact.result.position_action.cost_anchor
            for fact in history
            if fact.result.position_action is not None
            and fact.result.position_action.cost_anchor is not None
        ),
        None,
    )
    historical_targets = [
        target
        for fact in history
        if fact.result.position_action is not None
        and fact.result.position_action.execution is not None
        for target in fact.result.position_action.execution.targets
        if target.security_id == command.security_id and target.source_obligation_ids
    ]
    if independent_target is not None:
        historical_targets.append(independent_target)
    if historical_targets:
        ceiling = min(target.target_quantity for target in historical_targets)
        sources = tuple(
            dict.fromkeys(
                source for target in historical_targets for source in target.source_obligation_ids
            )
        )
        retained = ExecutionTarget(
            security_id=command.security_id,
            target_quantity=ceiling,
            required_sale_quantity=None,
            source_obligation_ids=sources,
            direction="EXIT" if ceiling == 0 else "REDUCE",
        )
        missing = missing.model_copy(
            update={
                "disposition": "CONCLUDED",
                "direction": retained.direction,
                "target_quantity": ceiling,
                "anchor": prior_anchor,
                "cost_anchor": prior_cost_anchor,
                "execution": base_plan.model_copy(
                    update={
                        "disposition": "BLOCKED",
                        "legs": (),
                        "targets": (
                            retained,
                            *(t for t in base_plan.targets if t.security_id != command.security_id),
                        ),
                    }
                ),
            }
        )
    report = ledger.get_formal_report_for_event(command.security_event_id, connection)
    position_report = ledger.get_formal_report_for_event(
        command.execution.concentration_event_id, connection
    )
    if (
        report is None
        or report.access_scope is None
        or not scope.same_scope_as(report.access_scope)
    ):
        return missing
    security_fact = ledger.get_decision_event(command.security_event_id, connection)
    assessment = security_fact.case.exit_assessment if security_fact else None
    security = report.result.exit_assessment
    if (
        security is None
        or not isinstance(assessment, SecurityAssessment)
        or assessment.security_id != command.security_id
        or assessment.lifecycle_id != command.lifecycle_id
        or assessment.cutoff_at != command.cutoff_at
    ):
        return missing
    position = (
        position_report.result.position
        if position_report is not None
        and position_report.access_scope is not None
        and scope.same_scope_as(position_report.access_scope)
        else None
    )
    if (
        position is None
        or position.snapshot.cutoff_at != command.cutoff_at
        or any(
            u.total_quantity is None
            for u in position.snapshot.action_units
            if u.security_id == command.security_id
        )
    ):
        if not security.hard_gates:
            return missing
        zero = ExecutionTarget(
            security_id=command.security_id,
            target_quantity=Decimal(0),
            required_sale_quantity=None,
            source_obligation_ids=(command.security_event_id,),
            direction="EXIT",
        )
        return missing.model_copy(
            update={
                "disposition": "CONCLUDED",
                "direction": "EXIT",
                "target_quantity": Decimal(0),
                "current_quantity": None,
                "reasons": (*missing.reasons, *(gate.gate_id for gate in security.hard_gates)),
                "execution": base_plan.model_copy(
                    update={
                        "disposition": "BLOCKED",
                        "targets": (
                            zero,
                            *(t for t in base_plan.targets if t.security_id != command.security_id),
                        ),
                    }
                ),
            }
        )
    assert position_report is not None
    units = [u for u in position.snapshot.action_units if u.security_id == command.security_id]
    if not units or any(u.total_quantity is None for u in units):
        return missing
    quantity = sum((u.total_quantity or Decimal(0) for u in units), Decimal(0))
    retained_targets = []
    for fact in history:
        prior_action = fact.result.position_action
        assert prior_action is not None
        if prior_action.execution is None:
            continue
        for prior_target in prior_action.execution.targets:
            if prior_target.security_id != command.security_id:
                continue
            for source in prior_target.source_obligation_ids:
                retained_targets.append(
                    EstablishedQuantityTarget(
                        target_id=source,
                        security_id=command.security_id,
                        target_quantity=prior_target.target_quantity,
                        direction="EXIT" if prior_target.target_quantity == 0 else "REDUCE",
                        qualified=True,
                        policy_version=command.policy.version.version_id,
                        evidence=position.snapshot.snapshot_evidence,
                    )
                )
    execution_command = command.execution.model_copy(
        update={
            "established_targets": (
                *command.execution.established_targets,
                *retained_targets,
            )
        }
    )
    if security.hard_gates:
        execution_command = execution_command.model_copy(
            update={
                "established_targets": (
                    *execution_command.established_targets,
                    EstablishedQuantityTarget(
                        target_id=command.security_event_id,
                        security_id=command.security_id,
                        target_quantity=Decimal(0),
                        direction="EXIT",
                        qualified=True,
                        policy_version=command.policy.version.version_id,
                        evidence=position.snapshot.snapshot_evidence,
                        priority="P0",
                    ),
                )
            }
        )
    plan = adjudicate_execution_plan(
        case.model_copy(update={"execution_plan": execution_command}), ledger, connection
    )
    zero_cap = any(
        t.security_id == command.security_id and t.target_quantity == 0 and t.source_obligation_ids
        for t in plan.targets
    )
    if security.hard_gates or zero_cap:
        return PositionActionOutcome(
            disposition="CONCLUDED",
            security_id=command.security_id,
            lifecycle_id=command.lifecycle_id,
            direction="EXIT",
            target_quantity=Decimal(0),
            current_quantity=quantity,
            reasons=tuple(gate.gate_id for gate in security.hard_gates)
            or ("ESTABLISHED_LIQUIDATION_TARGET",),
            execution=_complete_quantity_gaps(plan, command.security_id, quantity),
            anchor=prior_anchor,
            cost_anchor=prior_cost_anchor,
        )
    stress_report = ledger.get_formal_report_for_event(
        command.execution.stress_event_id, connection
    )
    liquidity_report = ledger.get_formal_report_for_event(
        command.execution.liquidity_event_id, connection
    )
    drawdown_report = ledger.get_formal_report_for_event(
        command.execution.drawdown_event_id, connection
    )
    concentration = position_report.result.concentration
    stress = stress_report.result.stress if stress_report is not None else None
    liquidity = liquidity_report.result.liquidity if liquidity_report is not None else None
    drawdown = drawdown_report.result.drawdown if drawdown_report is not None else None
    risk_buffer = (
        concentration is None
        or any(
            issuer.new_exposure_blocked
            for issuer in concentration.issuers
            if any(u.issuer_id == issuer.issuer_id for u in units)
        )
        or stress is None
        or stress.new_exposure_blocked
        or liquidity is None
        or liquidity.new_exposure_blocked
        or drawdown is None
        or drawdown.state is None
        or drawdown.state.risk_state != "NORMAL"
    )
    grid: Literal["0.05", "0.10"] = (
        command.policy.risk_grid if risk_buffer else command.policy.normal_grid
    )
    reasons: list[str] = []
    unresolved_basis = prior_anchor is not None and any(
        entry.security_id == command.security_id
        and entry.entry_type == "CORPORATE_ACTION"
        and entry.quantity_delta != 0
        and prior_anchor.frozen_at < entry.occurred_at <= command.cutoff_at
        for entry in position.snapshot.authoritative_ledger
    )
    if unresolved_basis:
        reasons.append("EXECUTION_QUANTITY_BASIS_UNRESOLVED")
    if (
        quantity <= 0
        or position.disposition != "RECONCILED"
        or any(u.market_price != security.standard_price for u in units)
    ):
        reasons.append("POSITION_FACTS_UNRESOLVED")
    if any(
        reason.startswith(
            ("RISK_HANDOFF", "EXECUTION_HISTORY", "EXECUTION_SNAPSHOT", "TARGET_SCOPE")
        )
        for reason in plan.reasons
    ):
        reasons.append("RISK_HANDOFF_UNAVAILABLE")
    authorizations = ledger.portfolio_authorization_history(
        connection, scope, command.execution.portfolio_id, case.knowledge_cutoff
    )
    latest = next(
        (
            outcome.authorization
            for outcome in reversed(authorizations)
            if outcome.authorization is not None
        ),
        None,
    )
    if (
        latest is None
        or latest.authorization_id != command.execution.authorization_id
        or not latest.evidence_available_by(command.cutoff_at)
        or latest.proposal.risk_budget.expires_at <= command.cutoff_at
        or latest.proposal.risk_budget.action_policy_version_id != command.policy.version.version_id
        or Decimal(grid) not in latest.proposal.risk_budget.downside_grid
    ):
        reasons.append("RISK_BUDGET_UNAVAILABLE_OR_EXPIRED")

    qualification = command.qualification_scope
    record = current_qualification(
        ledger.governance_history(connection, scope), qualification, command.policy.version
    )
    basis = (record.formal_passing_evidence or record.authorization_evidence) if record else None
    digest = sha256(
        json.dumps(
            command.policy.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    if (
        qualification.capability != "position-action"
        or qualification.purpose != "initial-position-action"
        or qualification.market_state != assessment.market_state
        or qualification.board != assessment.board
        or qualification.target != "D20/V60"
        or qualification.probability_grid != grid
        or qualification.user_id != scope.user_id
        or set(qualification.account_ids) != set(scope.account_ids)
        or qualification.account_type != "SIMULATED_CASH"
        or qualification.holding_age_domain != "INITIAL"
        or qualification.portfolio_scope != command.execution.portfolio_id
        or security.thesis is None
        or qualification.source != security.thesis.origin
        or any(u.origin != qualification.source for u in units)
        or record is None
        or record.decision_id != command.qualification_id
        or record.recorded_at > command.cutoff_at
        or record.evidence.available_at > command.cutoff_at
        or not qualification_is_current(record, command.cutoff_at)
        or record.status not in {"VALID", "AT_RISK"}
        or basis is None
        or basis.available_at > command.cutoff_at
        or basis.digest != digest
        or basis.market_calendar_version != assessment.calendar_version
    ):
        reasons.append("ACTION_QUALIFICATION_UNAVAILABLE")
    down = next(
        (p for p in security.probabilities if p.target == "D20" and p.loss_boundary == grid),
        None,
    )
    value = next((p for p in security.probabilities if p.target == "V60"), None)
    direction: Literal["HOLD", "REDUCE", "EXIT"] | None = None
    if (
        business_prerequisite_met
        and not reasons
        and value is not None
        and value.guarded_upper is not None
        and value.guarded_lower is not None
    ):
        if value.guarded_upper <= command.policy.value_denied_upper:
            direction = "EXIT"
        elif (
            down is not None
            and down.guarded_lower is not None
            and (down.guarded_lower >= command.policy.downside_risky_lower)
        ):
            direction = "REDUCE"
        elif (
            down is not None
            and down.guarded_upper is not None
            and down.guarded_upper <= command.policy.downside_safe_upper
            and value.guarded_lower >= command.policy.value_established_lower
        ):
            direction = "HOLD"
    anchor = None
    target = quantity if direction == "HOLD" else Decimal(0) if direction == "EXIT" else None
    if direction == "REDUCE":
        anchor = prior_anchor or StatisticalReductionAnchor(
            event_id=case.decision_event_id,
            quantity=quantity,
            market_value=quantity * (security.standard_price or Decimal(0)),
            frozen_at=command.cutoff_at,
        )
        target = min(quantity, anchor.quantity * command.policy.reduction_ratio)
    if direction in {"REDUCE", "EXIT"}:
        assert target is not None
        execution_command = execution_command.model_copy(
            update={
                "established_targets": (
                    *execution_command.established_targets,
                    EstablishedQuantityTarget(
                        target_id=anchor.event_id if anchor is not None else case.decision_event_id,
                        security_id=command.security_id,
                        target_quantity=target,
                        direction="EXIT" if direction == "EXIT" else "REDUCE",
                        qualified=True,
                        policy_version=command.policy.version.version_id,
                        evidence=position.snapshot.snapshot_evidence,
                        priority="P2",
                    ),
                )
            }
        )
        plan = adjudicate_execution_plan(
            case.model_copy(update={"execution_plan": execution_command}), ledger, connection
        )
    model_direction = direction
    if not business_prerequisite_met:
        reasons.append("BUSINESS_PREREQUISITE_FAILED")
    if direction is None and (
        down is None
        or value is None
        or down.guarded_upper is None
        or down.guarded_lower is None
        or value.guarded_upper is None
        or value.guarded_lower is None
    ):
        reasons.append("STATISTICAL_EVIDENCE_UNAVAILABLE")
    cost_failure = False
    outside = False
    route_map = {(r.account_id, r.security_id): r for r in command.execution.routes}
    for unit in units:
        route = route_map.get((unit.account_id, unit.security_id))
        curve = route.cost_curve if route is not None else None
        if curve is None or curve.evidence.problem_codes(
            command.cutoff_at, require_current_completeness=True
        ):
            cost_failure = True
            continue
        rate = (
            curve.commission_ratio
            + curve.trading_fee_ratio
            + curve.settlement_fee_ratio
            + curve.tax_ratio
            + curve.slippage_ratio
            + curve.market_impact_ratio
        )
        outside = outside or (
            rate > command.policy.maximum_cost_ratio
            or curve.minimum_commission > command.policy.maximum_minimum_commission
            or any(
                lot.tax_per_share > command.policy.maximum_dividend_tax_per_share
                for lot in curve.dividend_lots
            )
        )
    accrued_cost = sum(
        (
            -entry.cash_delta
            for entry in position.snapshot.authoritative_ledger
            if entry.security_id == command.security_id
            and entry.entry_type == "FEE"
            and prior_cost_anchor is not None
            and entry.occurred_at > prior_cost_anchor.frozen_at
        ),
        Decimal(0),
    )
    planned_cost = sum(
        (leg.disposal_cost for leg in plan.legs if leg.security_id == command.security_id),
        Decimal(0),
    )
    if plan.disposition == "BLOCKED" and target is not None and quantity > 0 and not cost_failure:
        # A blocked route is not a zero-cost forecast. Bound the full remaining
        # target sale using account-local legal quantities, even without bids.
        required = max(Decimal(0), quantity - target)
        bound = Decimal(0)
        for unit in units:
            route = route_map[(unit.account_id, unit.security_id)]
            assert route.cost_curve is not None and unit.total_quantity is not None
            amount = required * unit.total_quantity / quantity
            if amount:
                increments = max(Decimal(0), amount - route.minimum_quantity)
                increments = (increments / route.quantity_increment).to_integral_value(
                    rounding=ROUND_CEILING
                )
                amount = min(
                    unit.total_quantity,
                    route.minimum_quantity + increments * route.quantity_increment,
                )
            bound += route.cost_curve.cost(amount, unit.market_price or Decimal(0))
        planned_cost = max(planned_cost, bound)
    outside = (
        outside
        or max(Decimal(0), accrued_cost) + planned_cost
        > (
            prior_cost_anchor.market_value
            if prior_cost_anchor is not None
            else quantity * (security.standard_price or Decimal(0))
        )
        * command.policy.lifecycle_cost_ratio
    )
    cost_anchor = prior_cost_anchor
    if direction in {"REDUCE", "EXIT"} and not cost_failure and not outside:
        cost_anchor = prior_cost_anchor or StatisticalReductionAnchor(
            event_id=case.decision_event_id,
            quantity=quantity,
            market_value=quantity * (security.standard_price or Decimal(0)),
            frozen_at=command.cutoff_at,
        )
    if cost_failure or outside:
        reasons.append(
            "EXECUTION_COST_EVIDENCE_FAILED" if cost_failure else "STATISTICAL_COST_OUT_OF_SCOPE"
        )
        direction, target, anchor = None, None, None
        plan = adjudicate_execution_plan(
            case.model_copy(
                update={
                    "execution_plan": command.execution.model_copy(
                        update={"established_targets": tuple(retained_targets)}
                    )
                }
            ),
            ledger,
            connection,
        )
    effective = next(
        (
            t
            for t in plan.targets
            if t.security_id == command.security_id and t.source_obligation_ids
        ),
        None,
    )
    if effective is not None:
        if target is None or effective.target_quantity < target:
            target = effective.target_quantity
            direction = "EXIT" if target == 0 else "REDUCE"
        reasons.append("STRICTEST_TARGET_CONJUNCTION")
    if unresolved_basis:
        plan = plan.model_copy(
            update={
                "disposition": "BLOCKED",
                "legs": (),
                "reasons": (*plan.reasons, "EXECUTION_QUANTITY_BASIS_UNRESOLVED"),
                "targets": tuple(
                    t.model_copy(
                        update={
                            "remaining_quantity": None,
                            "remaining_gap": None,
                        }
                    )
                    for t in plan.targets
                ),
            }
        )
    plan = _complete_quantity_gaps(plan, command.security_id, quantity)
    return PositionActionOutcome(
        disposition="CONCLUDED"
        if direction
        else "DATA_OBSERVATION"
        if cost_failure
        or any(
            reason
            in {
                "POSITION_FACTS_UNRESOLVED",
                "RISK_HANDOFF_UNAVAILABLE",
                "STATISTICAL_EVIDENCE_UNAVAILABLE",
                "BUSINESS_PREREQUISITE_FAILED",
                "EXECUTION_QUANTITY_BASIS_UNRESOLVED",
            }
            for reason in reasons
        )
        else "ABSTAINED",
        security_id=command.security_id,
        lifecycle_id=command.lifecycle_id,
        direction=direction,
        model_direction=model_direction,
        target_quantity=target,
        current_quantity=quantity,
        selected_grid=grid,
        execution=plan,
        anchor=anchor or prior_anchor,
        cost_anchor=cost_anchor,
        reasons=tuple(reasons)
        + (("STATISTICAL_MATRIX_ABSTAINED",) if model_direction is None else ()),
    )


def _complete_quantity_gaps(
    plan: ExecutionPlanOutcome,
    security_id: str,
    current: Decimal,
) -> ExecutionPlanOutcome:
    remaining = current - sum(
        (leg.quantity for leg in plan.legs if leg.security_id == security_id), Decimal(0)
    )
    return plan.model_copy(
        update={
            "targets": tuple(
                target.model_copy(
                    update={
                        "remaining_quantity": remaining,
                        "remaining_gap": max(Decimal(0), remaining - target.target_quantity),
                    }
                )
                if target.security_id == security_id and target.remaining_gap is None
                else target
                for target in plan.targets
            )
        }
    )
