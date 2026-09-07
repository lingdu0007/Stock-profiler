"""Deadline-aware hypothetical funding, separate from confirmed cash availability."""

from collections import deque
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal

from pydantic import AwareDatetime, Field

from stock_profiler.modules.portfolio.contracts import DatedCashObligation, PortfolioContract
from stock_profiler.modules.position_management.contracts import (
    PositionActionUnit,
    PositionEvidence,
    ReconciledPositionSnapshot,
)


class SaleFundingTerms(PortfolioContract):
    account_id: str = Field(min_length=1)
    security_id: str = Field(min_length=1)
    minimum_quantity: Decimal = Field(gt=0)
    quantity_increment: Decimal = Field(gt=0)
    allow_full_odd_lot: bool
    commission_ratio: Decimal = Field(ge=0, lt=1)
    minimum_commission: Decimal = Field(ge=0)
    other_cost_ratio: Decimal = Field(ge=0, lt=1)
    transferable_at: AwareDatetime
    evidence: PositionEvidence


class MaximumFundingLeg(PortfolioContract):
    account_id: str
    security_id: str
    quantity: Decimal
    gross_proceeds: Decimal
    disposal_cost: Decimal
    net_proceeds: Decimal
    deadline_funding_contribution: Decimal = Decimal(0)
    transferable_at: AwareDatetime
    terms: SaleFundingTerms


class CashTransferRoute(PortfolioContract):
    route_id: str = Field(min_length=1)
    source_account_id: str = Field(min_length=1)
    target_account_id: str = Field(min_length=1)
    departure_at: AwareDatetime
    arrival_at: AwareDatetime
    capacity: Decimal = Field(ge=0)
    evidence: PositionEvidence


class ObligationFunding(PortfolioContract):
    obligation_id: str
    target_account_id: str
    latest_usable_at: AwareDatetime
    required_cash: Decimal
    maximum_covered_cash: Decimal
    uncovered_gap: Decimal


class FundingAssessment(PortfolioContract):
    reasons: tuple[str, ...]
    maximum_fundable_cash: Decimal | None
    uncovered_gap: Decimal | None
    plan: tuple[MaximumFundingLeg, ...]
    obligations: tuple[ObligationFunding, ...]


def assess_funding(
    snapshot: ReconciledPositionSnapshot,
    terms: tuple[SaleFundingTerms, ...],
    obligations: tuple[DatedCashObligation, ...],
    routes: tuple[CashTransferRoute, ...],
) -> FundingAssessment:
    account_ids = {cash.account_id for cash in snapshot.cash_states}
    if len({route.route_id for route in routes}) != len(routes) or any(
        route.source_account_id not in account_ids
        or route.target_account_id not in account_ids
        or route.source_account_id == route.target_account_id
        or not snapshot.cutoff_at <= route.departure_at <= route.arrival_at
        or route.evidence.problem_codes(snapshot.cutoff_at, require_current_completeness=True)
        for route in routes
    ):
        return FundingAssessment(
            reasons=("CASH_TRANSFER_EVIDENCE_FAILED",),
            maximum_fundable_cash=None,
            uncovered_gap=None,
            plan=(),
            obligations=(),
        )
    by_security = {(item.account_id, item.security_id): item for item in terms}
    captured = {(unit.account_id, unit.security_id) for unit in snapshot.action_units}
    required = {
        (unit.account_id, unit.security_id)
        for unit in snapshot.action_units
        if not _known_unsellable(unit)
    }
    if (
        len(by_security) != len(terms)
        or not required.issubset(by_security)
        or not set(by_security).issubset(captured)
    ):
        return FundingAssessment(
            reasons=("SALE_FUNDING_TERMS_INCOMPLETE",),
            maximum_fundable_cash=None,
            uncovered_gap=None,
            plan=(),
            obligations=(),
        )
    plan: list[MaximumFundingLeg] = []
    for unit in snapshot.action_units:
        if _known_unsellable(unit):
            continue
        term = by_security[unit.account_id, unit.security_id]
        if (
            term.evidence.problem_codes(snapshot.cutoff_at, require_current_completeness=True)
            or term.transferable_at < snapshot.cutoff_at
        ):
            return FundingAssessment(
                reasons=("SALE_FUNDING_EVIDENCE_FAILED",),
                maximum_fundable_cash=None,
                uncovered_gap=None,
                plan=(),
                obligations=(),
            )
        maximum = unit.exact_statistical_action_quantity
        if maximum is None or unit.market_price is None:
            return FundingAssessment(
                reasons=("SALE_FUNDING_QUANTITY_UNKNOWN",),
                maximum_fundable_cash=None,
                uncovered_gap=None,
                plan=(),
                obligations=(),
            )
        if maximum < term.minimum_quantity:
            quantity = Decimal(0)
        else:
            quantity = (
                term.minimum_quantity
                + ((maximum - term.minimum_quantity) / term.quantity_increment).to_integral_value(
                    rounding=ROUND_FLOOR
                )
                * term.quantity_increment
            )
        if term.allow_full_odd_lot and maximum == unit.total_quantity:
            quantity = maximum
        gross = quantity * unit.market_price
        cost = (
            max(term.minimum_commission, gross * term.commission_ratio)
            + gross * term.other_cost_ratio
            if quantity > 0
            else Decimal(0)
        )
        if gross <= cost:
            continue
        plan.append(
            MaximumFundingLeg(
                account_id=unit.account_id,
                security_id=unit.security_id,
                quantity=quantity,
                gross_proceeds=gross,
                disposal_cost=cost,
                net_proceeds=gross - cost,
                transferable_at=term.transferable_at,
                terms=term,
            )
        )
    resources: list[tuple[str, datetime, Decimal]] = [
        (cash.account_id, snapshot.cutoff_at, cash.transferable_cash)
        for cash in snapshot.cash_states
        if cash.transferable_cash is not None
    ] + [(leg.account_id, leg.transferable_at, leg.net_proceeds) for leg in plan]
    maximum_cash, results, used = _fund_obligations(resources, routes, obligations)
    cash_resource_count = len(resources) - len(plan)
    return FundingAssessment(
        reasons=(),
        maximum_fundable_cash=maximum_cash,
        uncovered_gap=sum((item.uncovered_gap for item in results), Decimal(0)),
        plan=tuple(
            leg.model_copy(
                update={"deadline_funding_contribution": used[cash_resource_count + index]}
            )
            for index, leg in enumerate(plan)
            if not obligations or used[cash_resource_count + index] > 0
        ),
        obligations=tuple(results),
    )


def _known_unsellable(unit: PositionActionUnit) -> bool:
    return unit.exact_statistical_action_quantity == 0 or (
        bool(unit.reasons)
        and all(reason.startswith("EXECUTION_RESTRICTION_ACTIVE:") for reason in unit.reasons)
    )


_Node = tuple[str, str]
_Graph = dict[_Node, dict[_Node, Decimal]]
_SOURCE: _Node = ("source", "")
_SINK: _Node = ("sink", "")


def _add_edge(graph: _Graph, source: _Node, target: _Node, capacity: Decimal) -> None:
    graph.setdefault(source, {})[target] = graph.get(source, {}).get(target, Decimal(0)) + capacity
    graph.setdefault(target, {}).setdefault(source, Decimal(0))


def _maximum_flow(graph: _Graph) -> Decimal:
    """Augment shortest residual paths; reverse edges undo an earlier funding allocation."""
    total = Decimal(0)
    while True:
        parents: dict[_Node, _Node] = {}
        queue = deque([_SOURCE])
        visited = {_SOURCE}
        while queue and _SINK not in visited:
            source = queue.popleft()
            for target, capacity in graph[source].items():
                if capacity > 0 and target not in visited:
                    visited.add(target)
                    parents[target] = source
                    queue.append(target)
        if _SINK not in visited:
            return total
        target = _SINK
        capacities: list[Decimal] = []
        while target != _SOURCE:
            source = parents[target]
            capacities.append(graph[source][target])
            target = source
        amount = min(capacities)
        target = _SINK
        while target != _SOURCE:
            source = parents[target]
            graph[source][target] -= amount
            graph[target][source] += amount
            target = source
        total += amount


def _fund_obligations(
    resources: list[tuple[str, datetime, Decimal]],
    routes: tuple[CashTransferRoute, ...],
    obligations: tuple[DatedCashObligation, ...],
) -> tuple[Decimal, tuple[ObligationFunding, ...], list[Decimal]]:
    total_cash = sum((item[2] for item in resources), Decimal(0))
    if not obligations:
        return total_cash, (), [item[2] for item in resources]
    times = sorted(
        {
            *(instant for _, instant, _ in resources),
            *(route.departure_at for route in routes),
            *(route.arrival_at for route in routes),
            *(item.latest_usable_at for item in obligations),
        }
    )
    accounts = sorted({item[0] for item in resources})
    timeline = {
        (account, instant): ("account", f"{account}:{instant.isoformat()}")
        for account in accounts
        for instant in times
    }
    base: _Graph = {_SOURCE: {}, _SINK: {}}
    for account in accounts:
        for before, after in zip(times, times[1:], strict=False):
            _add_edge(base, timeline[account, before], timeline[account, after], total_cash)
    for index, (account, instant, amount) in enumerate(resources):
        resource = ("resource", str(index))
        _add_edge(base, _SOURCE, resource, amount)
        _add_edge(base, resource, timeline[account, instant], amount)
    for route in sorted(routes, key=lambda item: item.route_id):
        transfer = ("route", route.route_id)
        _add_edge(
            base, timeline[route.source_account_id, route.departure_at], transfer, route.capacity
        )
        _add_edge(
            base, transfer, timeline[route.target_account_id, route.arrival_at], route.capacity
        )
    ordered = sorted(obligations, key=lambda item: (item.latest_usable_at, item.obligation_id))
    graphs = [{node: dict(edges) for node, edges in base.items()} for _ in range(2)]
    for graph, limited in zip(graphs, (True, False), strict=True):
        for obligation in ordered:
            node = ("obligation", obligation.obligation_id)
            capacity = obligation.amount if limited else total_cash
            _add_edge(
                graph,
                timeline[obligation.target_account_id, obligation.latest_usable_at],
                node,
                capacity,
            )
            _add_edge(graph, node, _SINK, capacity)
    covered_graph, maximum_graph = graphs
    _maximum_flow(covered_graph)
    maximum = _maximum_flow(maximum_graph)
    results = tuple(
        ObligationFunding(
            obligation_id=item.obligation_id,
            target_account_id=item.target_account_id,
            latest_usable_at=item.latest_usable_at,
            required_cash=item.amount,
            maximum_covered_cash=item.amount
            - covered_graph[("obligation", item.obligation_id)][_SINK],
            uncovered_gap=covered_graph[("obligation", item.obligation_id)][_SINK],
        )
        for item in ordered
    )
    used = [
        amount - maximum_graph[_SOURCE][("resource", str(index))]
        for index, (_, _, amount) in enumerate(resources)
    ]
    return maximum, results, used
