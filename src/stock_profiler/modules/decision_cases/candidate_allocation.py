"""Host-owned allocation from saved candidates and authoritative risk handoffs."""

from collections import defaultdict
from decimal import Context, Decimal, localcontext

from stock_profiler.modules.decision_cases.domain import FrozenDecisionCase
from stock_profiler.modules.decision_cases.execution_plans import adjudicate_execution_plan
from stock_profiler.modules.decision_cases.ports import DecisionLedger, Transaction
from stock_profiler.modules.portfolio.allocation_contracts import (
    AcquisitionLeg,
    CandidateAllocationOutcome,
    CandidateAllocationRow,
)
from stock_profiler.modules.portfolio.allocation_solver import (
    AllocationProblem,
    AllocationSolveFailed,
    Capacity,
    capacity_is_feasible,
    continuous_allocation,
    discrete_allocation,
    discrete_principal_ceiling,
)
from stock_profiler.modules.portfolio.market_calendar import synthetic_market_calendar


def _correlated(left: tuple[Decimal, ...], right: tuple[Decimal, ...], ceiling: Decimal) -> bool:
    count = Decimal(len(left))
    left_mean, right_mean = sum(left) / count, sum(right) / count
    covariance = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right, strict=True))
    left_variance = sum((value - left_mean) ** 2 for value in left)
    right_variance = sum((value - right_mean) ** 2 for value in right)
    return covariance > 0 and covariance**2 > ceiling**2 * left_variance * right_variance


def adjudicate_candidate_allocation(
    case: FrozenDecisionCase,
    ledger: DecisionLedger[Transaction],
    connection: Transaction,
    *,
    business_prerequisite_met: bool,
) -> CandidateAllocationOutcome:
    with localcontext(Context(prec=38)):
        return _adjudicate_candidate_allocation(
            case, ledger, connection, business_prerequisite_met=business_prerequisite_met
        )


def _adjudicate_candidate_allocation(
    case: FrozenDecisionCase,
    ledger: DecisionLedger[Transaction],
    connection: Transaction,
    *,
    business_prerequisite_met: bool,
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
            disposition="BLOCKED", reasons=("CANDIDATE_HANDOFF_UNAVAILABLE",)
        )
    release = source.result.candidate_release
    candidates = tuple(member for member in release.members if member.candidate)
    securities = {row.security_id: row for row in command.securities}

    def blocked(reason: str) -> CandidateAllocationOutcome:
        return CandidateAllocationOutcome(
            disposition="BLOCKED",
            reasons=(reason,),
            candidate_event_id=command.candidate_event_id,
            candidate_batch_id=release.batch_id,
            formed_at=command.cutoff_at,
            policy=command.policy,
            risk_handoff=command.risk_handoff,
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
                    reasons=(reason,),
                )
                for member in candidates
            ),
        )

    if not business_prerequisite_met:
        return blocked("BUSINESS_PREREQUISITE_NOT_MET")
    if command.policy is None:
        return blocked("ALLOCATION_POLICY_REQUIRED")
    policy = command.policy
    if (
        release.disposition != "CANDIDATES"
        or release.knowledge_cutoff > command.cutoff_at
        or release.published_at > command.cutoff_at
        or any(member.calibrated_probability is None for member in candidates)
    ):
        return blocked("CANDIDATE_HANDOFF_INVALID")
    if len({member.security_id for member in candidates}) != len(candidates):
        return blocked("CANDIDATE_IDENTITIES_AMBIGUOUS")
    protection = adjudicate_execution_plan(
        case.model_copy(update={"execution_plan": command.risk_handoff}), ledger, connection
    )
    if (
        protection.new_exposure_blocked
        or protection.disposition == "BLOCKED"
        or any((target.required_sale_quantity or Decimal(0)) > 0 for target in protection.targets)
    ):
        return blocked("NEW_EXPOSURE_BLOCKED")
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
        return blocked("RISK_HANDOFF_SUPERSEDED")
    authorization = liquidity.purchase_authorization
    if not authorization or not authorization.usage or not authorization.usage.allowed:
        return blocked("ACTIVE_RISK_BUDGET_REQUIRED")
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
        return blocked("NEW_EXPOSURE_BLOCKED")
    snapshot = liquidity.position_snapshot.snapshot
    equity, cash = liquidity.net_liquidation_equity, liquidity.deployable_purchase_cash
    assert equity is not None and cash is not None
    liquidity_fact = ledger.get_original_decision_event(
        liquidity_report.business_object_id, connection
    )
    if liquidity_fact is None or liquidity_fact.case.liquidity is None:
        return blocked("LIQUIDITY_COST_BINDING_UNAVAILABLE")
    # Replace the prior aggregate estimate with this plan's actual full route costs.
    cash += liquidity_fact.case.liquidity.expected_purchase_fees
    if equity <= 0 or set(scope.account_ids) != {row.account_id for row in snapshot.cash_states}:
        return blocked("COMPLETE_PORTFOLIO_REQUIRED")
    if any(row.current_market_exposure is None for row in snapshot.issuer_exposures):
        return blocked("COMMITTED_EXPOSURE_UNKNOWN")
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
        order.order_id: order for order in snapshot.unfinished_orders if order.side == "BUY"
    }
    supplied_orders = {
        row.broker_order_id for row in command.commitments if row.broker_order_id is not None
    }
    if set(broker_orders) != supplied_orders:
        return blocked("BUY_COMMITMENTS_INCOMPLETE")
    security_committed: dict[str, Decimal] = defaultdict(Decimal)
    for commitment in command.commitments:
        if (
            commitment.security_id in held_issuers
            and held_issuers[commitment.security_id] != commitment.issuer_id
        ):
            return blocked("BUY_COMMITMENT_IDENTITY_MISMATCH")
        if (
            commitment.quantity is None
            or commitment.price_cap is None
            or commitment.principal != commitment.quantity * commitment.price_cap
        ):
            return blocked("BUY_COMMITMENT_PRICE_BASIS_UNKNOWN")
        if commitment.account_id not in account_cash or commitment.evidence.problem_codes(
            command.cutoff_at, require_current_completeness=True
        ):
            return blocked("BUY_COMMITMENT_EVIDENCE_FAILED")
        if commitment.broker_order_id is not None:
            order = broker_orders[commitment.broker_order_id]
            if (
                order.account_id != commitment.account_id
                or order.security_id != commitment.security_id
                or order.remaining_quantity != commitment.quantity
                or order.evidence.problem_codes(
                    command.cutoff_at, require_current_completeness=True
                )
            ):
                return blocked("BUY_COMMITMENT_IDENTITY_MISMATCH")
        else:
            deduction = commitment.principal + commitment.purchase_cost
            account_cash[commitment.account_id] -= deduction
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
        return blocked("COMMITTED_PROTECTION_CAPACITY_EXCEEDED")

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
    awaiting = False
    for index, member in enumerate(candidates):
        eligible = []
        for route in sorted(command.routes, key=lambda row: (row.security_id, row.account_id)):
            if route.security_id != member.security_id or route.account_id not in account_cash:
                continue
            if not route.permission or route.evidence.problem_codes(
                command.cutoff_at, require_current_completeness=True
            ):
                continue
            if any(
                restriction.active
                and restriction.account_id == route.account_id
                and restriction.security_id in {None, route.security_id}
                for restriction in snapshot.execution_restrictions
            ):
                continue
            if (
                route.current_price is None
                or not route.minimum_price <= route.current_price <= route.maximum_price
            ):
                continue
            if route.price_cap is None or not route.price_cap_confirmed:
                awaiting = True
                route = route.model_copy(update={"price_cap": route.current_price})
            elif (
                not route.minimum_price <= route.price_cap <= route.maximum_price
                or route.price_cap % route.price_tick != 0
            ):
                continue
            eligible.append(route)
        if not eligible and not reasons[index]:
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
    try:
        continuous_solution = continuous_allocation(problem)
        continuous = continuous_solution.principals
        if not capacity_is_feasible(problem, continuous, continuous_solution.route_principals):
            return blocked("ALLOCATION_CAPACITY_VERIFICATION_FAILED")
        quantities = (
            tuple(Decimal(0) for _ in routes)
            if awaiting
            else discrete_allocation(problem, continuous)
        )
    except AllocationSolveFailed:
        return blocked("ALLOCATION_OPTIMUM_UNAVAILABLE")
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
            return blocked("ALLOCATION_QUANTITY_VERIFICATION_FAILED")
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
            principal > discrete_principal_ceiling(problem, continuous, index)
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
        return blocked("ALLOCATION_CAPACITY_VERIFICATION_FAILED")
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
            (route.minimum_quantity * (route.price_cap or Decimal(0)) for route in member_routes),
            default=Decimal(0),
        )
        row_reasons = list(reasons[index])
        if issuer_continuous < targets[index] and not row_reasons:
            if (
                sum(
                    (
                        amount * (problem.shock + route.disposal_friction_ratio)
                        for amount, route in zip(
                            continuous_solution.route_principals, routes, strict=True
                        )
                    ),
                    Decimal(0),
                )
                >= stress_remaining
            ):
                row_reasons.append("STRESS_CAPACITY_EXHAUSTED")
            if (
                sum(
                    (
                        amount + route.cost(amount)
                        for amount, route in zip(
                            continuous_solution.route_principals, routes, strict=True
                        )
                    ),
                    Decimal(0),
                )
                >= cash
            ):
                row_reasons.append("CASH_CAPACITY_EXHAUSTED")
            for capacity in capacities:
                if (
                    index in capacity.members
                    and sum((continuous[identity] for identity in capacity.members), Decimal(0))
                    >= max(Decimal(0), capacity.remaining)
                    and capacity.reason != "ENTRY_TARGET_REACHED"
                ):
                    row_reasons.append(capacity.reason)
            if not row_reasons:
                row_reasons.append("CASH_OR_STRESS_CAPACITY_EXHAUSTED")
        if awaiting:
            row_reasons.append("PRICE_CAP_REQUIRED")
        elif principal < continuous[index]:
            row_reasons.append("LEGAL_UNIT_OR_LEXICOGRAPHIC_LIMIT")
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
                reasons=tuple(dict.fromkeys(row_reasons)),
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
    )
