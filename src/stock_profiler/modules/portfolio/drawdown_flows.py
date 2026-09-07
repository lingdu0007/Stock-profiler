"""Exact unit accounting for complete, broker-bound capital flows."""

from dataclasses import dataclass
from decimal import Context, Decimal, localcontext
from fractions import Fraction

from stock_profiler.modules.portfolio.drawdown_contracts import (
    DrawdownCommand,
    DrawdownState,
    DrawdownValuation,
    FlowLedgerKey,
)
from stock_profiler.modules.position_management.contracts import PositionReconciliationOutcome


@dataclass(frozen=True)
class UnitAccounting:
    units: Fraction
    peak: Fraction
    flow_ids: tuple[str, ...]
    interval_drawdown: Fraction


def decimal_value(value: Fraction) -> Decimal:
    with localcontext(Context(prec=50)):
        return Decimal(value.numerator) / Decimal(value.denominator)


def equity_for(
    valuation: DrawdownValuation,
    position: PositionReconciliationOutcome | None,
    account_ids: tuple[str, ...],
    currency: str,
) -> Fraction:
    if (
        position is None
        or position.disposition != "RECONCILED"
        or position.snapshot.valuation_currency != currency
        or position.snapshot.total_account_equity is None
        or valuation.liquidation_cost is None
        or set(item.account_id for item in position.snapshot.cash_states) != set(account_ids)
        or valuation.evidence.problem_codes(
            position.snapshot.cutoff_at, require_current_completeness=True
        )
    ):
        raise ValueError("DRAWDOWN_EVIDENCE_UNKNOWN")
    return Fraction(position.snapshot.total_account_equity) - Fraction(valuation.liquidation_cost)


def transfer_keys(position: PositionReconciliationOutcome) -> tuple[FlowLedgerKey, ...]:
    return tuple(
        FlowLedgerKey(account_id=entry.account_id, entry_id=entry.entry_id)
        for entry in position.snapshot.authoritative_ledger
        if entry.entry_type in {"TRANSFER_IN", "TRANSFER_OUT"}
    )


def adjusted_units(
    command: DrawdownCommand,
    prior: DrawdownState,
    position: PositionReconciliationOutcome,
    before_positions: dict[str, PositionReconciliationOutcome | None],
) -> UnitAccounting:
    units = prior.exact_units.fraction
    peak = prior.exact_peak.fraction
    interval_drawdown = Fraction(0)
    new_keys = set(transfer_keys(position)) - set(prior.processed_transfers)
    declared = tuple(key for flow in command.capital_flows for key in flow.ledger_keys)
    if len(set(declared)) != len(declared) or set(declared) != new_keys:
        raise ValueError("CAPITAL_FLOW_COVERAGE_UNKNOWN")
    flows = command.capital_flows
    ids = tuple(flow.flow_id for flow in flows)
    if len(set(ids)) != len(ids) or set(ids).intersection(prior.processed_flow_ids):
        raise ValueError("CAPITAL_FLOW_IDENTITY_CONFLICT")
    if any(
        left.occurred_at >= right.occurred_at for left, right in zip(flows, flows[1:], strict=False)
    ):
        raise ValueError("CAPITAL_FLOW_CLOCK_UNKNOWN")
    entries = {
        FlowLedgerKey(account_id=entry.account_id, entry_id=entry.entry_id): entry
        for entry in position.snapshot.authoritative_ledger
    }
    processed = set(prior.processed_transfers)
    preceding_flow_at = None
    for flow in flows:
        before = before_positions.get(flow.before_valuation.position_event_id)
        if before is None or not (
            prior.accounting_cutoff_at
            <= before.snapshot.cutoff_at
            <= flow.occurred_at
            <= command.cutoff_at
        ):
            raise ValueError("CAPITAL_FLOW_CLOCK_UNKNOWN")
        if (preceding_flow_at is not None and before.snapshot.cutoff_at < preceding_flow_at) or set(
            transfer_keys(before)
        ) != processed:
            raise ValueError("CAPITAL_FLOW_SEQUENCE_UNKNOWN")
        pre_equity = equity_for(
            flow.before_valuation,
            before,
            prior.account_ids,
            position.snapshot.valuation_currency,
        )
        if pre_equity <= 0 or units <= 0:
            raise ValueError("CAPITAL_FLOW_VALUE_UNKNOWN")
        pre_nav = pre_equity / units
        peak = max(peak, pre_nav)
        interval_drawdown = max(interval_drawdown, 1 - pre_nav / peak)
        amount = Fraction(0)
        for key in flow.ledger_keys:
            entry = entries[key]
            if entry.occurred_at != flow.occurred_at or entry.corrects_entry_id is not None:
                raise ValueError("CAPITAL_FLOW_CLOCK_UNKNOWN")
            amount += Fraction(entry.cash_delta)
            if entry.quantity_delta:
                holding = next(
                    (
                        unit
                        for unit in position.snapshot.action_units
                        if unit.account_id == key.account_id
                        and unit.security_id == entry.security_id
                    ),
                    None,
                )
                if (
                    holding is None
                    or holding.market_price is None
                    or holding.position_evidence.business_effective_at != flow.occurred_at
                ):
                    raise ValueError("CAPITAL_FLOW_ASSET_VALUE_UNKNOWN")
                amount += Fraction(entry.quantity_delta) * Fraction(holding.market_price)
        if flow.kind == "INTERNAL":
            if amount != 0 or len({key.account_id for key in flow.ledger_keys}) < 2:
                raise ValueError("INTERNAL_TRANSFER_UNRECONCILED")
        else:
            units += amount / pre_nav
        if units <= 0:
            raise ValueError("CAPITAL_UNITS_EXHAUSTED")
        processed.update(flow.ledger_keys)
        preceding_flow_at = flow.occurred_at
    return UnitAccounting(units, peak, (*prior.processed_flow_ids, *ids), interval_drawdown)
