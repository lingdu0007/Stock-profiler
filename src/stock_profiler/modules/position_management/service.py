"""Deterministic reconciliation of broker-authoritative position-state facts."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from hashlib import sha256
from typing import Protocol

from stock_profiler.modules.position_management.contracts import (
    AccountCashState,
    AccountExecutionRestriction,
    AccountOpenOrder,
    AccountPositionSnapshot,
    AuthoritativeLedgerEntry,
    BrokerPositionFact,
    ExecutionRestriction,
    IssuerExposure,
    PositionActionUnit,
    PositionAffectedScope,
    PositionEvidence,
    PositionFactConflict,
    PositionReconciliationOutcome,
    PositionSnapshotCommand,
    ReconciledPositionSnapshot,
)

_RECONCILIATION_DECIMAL_CONTEXT = Context(prec=34, rounding=ROUND_HALF_EVEN)


class ConflictRecorder(Protocol):
    """Type the host-local callback that retains a reconciliation conflict."""

    def __call__(
        self,
        *,
        account_id: str | None,
        security_id: str | None,
        code: str,
        fields: tuple[str, ...],
        blocks_exact_statistical_quantity: bool,
        portfolio_dependency: bool = False,
        blocks_current_valuation: bool = False,
    ) -> None: ...


def reconcile(
    command: PositionSnapshotCommand,
    *,
    prior_ledger: tuple[AuthoritativeLedgerEntry, ...] = (),
) -> PositionReconciliationOutcome:
    """Evaluate a frozen snapshot under the host's fixed decimal contract."""
    with localcontext(_RECONCILIATION_DECIMAL_CONTEXT):
        return _reconcile(command, prior_ledger)


def _reconcile(
    command: PositionSnapshotCommand,
    prior_ledger: tuple[AuthoritativeLedgerEntry, ...],
) -> PositionReconciliationOutcome:
    """Retain conflicts and expose exact quantities only from complete broker facts."""
    conflicts: list[PositionFactConflict] = []
    conflict_keys: set[tuple[str | None, str | None, str, tuple[str, ...]]] = set()
    blocking: dict[tuple[str | None, str | None], list[str]] = defaultdict(list)
    valuation: dict[tuple[str | None, str | None], list[str]] = defaultdict(list)
    portfolio_blocking: list[str] = []

    def record(
        *,
        account_id: str | None,
        security_id: str | None,
        code: str,
        fields: tuple[str, ...],
        blocks_exact_statistical_quantity: bool,
        portfolio_dependency: bool = False,
        blocks_current_valuation: bool = False,
    ) -> None:
        key = (account_id, security_id, code, fields)
        if key in conflict_keys:
            return
        conflict_keys.add(key)
        conflicts.append(
            PositionFactConflict(
                conflict_id=_conflict_id(account_id, security_id, code, fields),
                code=code,
                affected_scope=PositionAffectedScope(
                    account_id=account_id,
                    security_id=security_id,
                    fields=fields,
                ),
                blocks_exact_statistical_quantity=blocks_exact_statistical_quantity,
            )
        )
        if blocks_exact_statistical_quantity:
            _append_unique(blocking[(account_id, security_id)], code)
            if portfolio_dependency:
                _append_unique(portfolio_blocking, code)
        if blocks_current_valuation:
            _append_unique(valuation[(account_id, security_id)], code)

    _record_evidence(
        command.snapshot_evidence,
        command.cutoff_at,
        record,
        account_id=None,
        security_id=None,
        fields=("snapshot_evidence",),
        blocks=True,
        current=True,
        portfolio=True,
    )

    equities: list[Decimal | None] = []
    cash_states: list[AccountCashState] = []
    orders_output: list[AccountOpenOrder] = []
    restrictions_output: list[AccountExecutionRestriction] = []
    ledger_output: list[AuthoritativeLedgerEntry] = []
    position_rows: list[tuple[AccountPositionSnapshot, BrokerPositionFact]] = []
    active_restrictions: dict[tuple[str, str | None], list[ExecutionRestriction]] = defaultdict(
        list
    )
    issuer_by_security: dict[str, set[str]] = defaultdict(set)
    missing_summary_security_ids: set[str] = set()
    prior_entries: dict[tuple[str, str], AuthoritativeLedgerEntry] = {}
    for entry in prior_ledger:
        prior_entries.setdefault((entry.account_id, entry.entry_id), entry)

    for account in command.accounts:
        cash = account.cash_state
        equities.append(account.account_equity)
        _record_account_problems(account, command.cutoff_at, record)
        cash_states.append(
            AccountCashState(
                account_id=account.account_id,
                account_type=account.account_type,
                currency=account.currency,
                account_evidence=account.snapshot_evidence,
                account_equity=account.account_equity,
                account_equity_evidence=account.account_equity_evidence,
                cash_availability_semantics=cash.cash_availability_semantics,
                ledger_cash_semantics=cash.ledger_cash_semantics,
                opening_ledger_cash=cash.opening_ledger_cash,
                opening_ledger_cash_evidence=cash.opening_ledger_cash_evidence,
                ledger_cash=cash.ledger_cash,
                trading_cash=cash.trading_cash,
                transferable_cash=cash.transferable_cash,
                frozen_cash=cash.frozen_cash,
                receivable_cash=cash.receivable_cash,
                payable_cash=cash.payable_cash,
                cash_state_evidence=cash.evidence,
            )
        )
        orders_output.extend(
            AccountOpenOrder(account_id=account.account_id, **order.model_dump())
            for order in account.open_orders
        )
        restrictions_output.extend(
            AccountExecutionRestriction(account_id=account.account_id, **restriction.model_dump())
            for restriction in account.execution_restrictions
        )
        _record_ledger_history_conflicts(account, prior_entries, record)

        ledger_by_security, ledger_cash_delta = _reconstruct_ledger(
            account,
            command.cutoff_at,
            record,
            ledger_output,
        )
        sell_orders, buy_reserves = _reconcile_orders(account, command.cutoff_at, record)
        _record_buy_reserve(account, buy_reserves, record)
        for restriction in account.execution_restrictions:
            evidence_codes = _record_evidence(
                restriction.evidence,
                command.cutoff_at,
                record,
                account_id=account.account_id,
                security_id=restriction.security_id,
                fields=("execution_restrictions.evidence",),
                blocks=True,
                current=True,
            )
            if restriction.active and not evidence_codes:
                active_restrictions[(account.account_id, restriction.security_id)].append(
                    restriction
                )

        position_security_ids = {position.security_id for position in account.positions}
        for position in account.positions:
            position_rows.append((account, position))
            issuer_by_security[position.security_id].add(position.issuer_id)
            _record_position_problems(
                account,
                position,
                command.cutoff_at,
                ledger_by_security,
                sell_orders,
                record,
            )

        summary_required = (
            {
                security_id
                for security_id, (quantity, cost) in ledger_by_security.items()
                if quantity != 0 or cost != 0
            }
            | set(sell_orders)
            | {
                restriction.security_id
                for restriction in account.execution_restrictions
                if restriction.active and restriction.security_id is not None
            }
        )
        for security_id in sorted(summary_required - position_security_ids):
            record(
                account_id=account.account_id,
                security_id=security_id,
                code="BROKER_POSITION_SUMMARY_MISSING",
                fields=("positions", "ledger_entries", "open_orders", "execution_restrictions"),
                blocks_exact_statistical_quantity=True,
                blocks_current_valuation=True,
            )
            missing_summary_security_ids.add(security_id)
        _record_cash_and_equity(account, ledger_cash_delta, record)

    conflicting_security_ids: set[str] = set()
    for security_id, issuers in sorted(issuer_by_security.items()):
        if len(issuers) > 1:
            record(
                account_id=None,
                security_id=security_id,
                code="SECURITY_ISSUER_IDENTITY_CONFLICT",
                fields=("positions.security_id", "positions.issuer_id"),
                blocks_exact_statistical_quantity=True,
                blocks_current_valuation=True,
            )
            conflicting_security_ids.add(security_id)

    for annotation in command.annotations:
        if annotation.created_at > command.cutoff_at:
            record(
                account_id=annotation.account_id,
                security_id=annotation.security_id,
                code="ANNOTATION_AFTER_CUTOFF",
                fields=("annotations.created_at",),
                blocks_exact_statistical_quantity=False,
            )

    action_units = _project_actions(
        position_rows,
        active_restrictions,
        blocking,
        portfolio_blocking,
    )
    issuer_exposures = _project_exposures(
        position_rows,
        command.valuation_currency,
        valuation,
        missing_summary_security_ids,
        conflicting_security_ids,
    )
    snapshot = ReconciledPositionSnapshot(
        snapshot_id=command.snapshot_id,
        cutoff_at=command.cutoff_at,
        valuation_currency=command.valuation_currency,
        snapshot_source=command.snapshot_evidence.source,
        evidence_clock=command.snapshot_evidence.clock,
        snapshot_evidence=command.snapshot_evidence,
        total_account_equity=_sum_if_complete(tuple(equities)),
        action_units=action_units,
        issuer_exposures=issuer_exposures,
        cash_states=tuple(cash_states),
        unfinished_orders=tuple(orders_output),
        execution_restrictions=tuple(restrictions_output),
        authoritative_ledger=tuple(ledger_output),
        user_annotations=command.annotations,
    )
    if conflicts:
        return PositionReconciliationOutcome(
            disposition="CONFLICTED",
            reasons=tuple(dict.fromkeys(conflict.code for conflict in conflicts)),
            snapshot=snapshot,
            conflicts=tuple(conflicts),
        )
    return PositionReconciliationOutcome(
        disposition="RECONCILED",
        reasons=("AUTHORITATIVE_POSITION_FACTS_RECONCILED",),
        snapshot=snapshot,
        conflicts=(),
    )


def _record_account_problems(
    account: AccountPositionSnapshot,
    cutoff_at: datetime,
    record: ConflictRecorder,
) -> None:
    cash = account.cash_state
    _record_evidence(
        account.snapshot_evidence,
        cutoff_at,
        record,
        account_id=account.account_id,
        security_id=None,
        fields=("snapshot_evidence",),
        blocks=True,
        current=True,
        portfolio=True,
    )
    if account.account_equity is None:
        record(
            account_id=account.account_id,
            security_id=None,
            code="ACCOUNT_EQUITY_MISSING",
            fields=("account_equity",),
            blocks_exact_statistical_quantity=True,
            portfolio_dependency=True,
        )
    _record_evidence(
        account.account_equity_evidence,
        cutoff_at,
        record,
        account_id=account.account_id,
        security_id=None,
        fields=("account_equity_evidence",),
        blocks=True,
        current=True,
        portfolio=True,
    )
    fields = (
        "opening_ledger_cash",
        "ledger_cash",
        "trading_cash",
        "transferable_cash",
        "frozen_cash",
        "receivable_cash",
        "payable_cash",
    )
    if any(getattr(cash, field) is None for field in fields):
        record(
            account_id=account.account_id,
            security_id=None,
            code="CASH_STATE_INCOMPLETE",
            fields=fields,
            blocks_exact_statistical_quantity=True,
            portfolio_dependency=True,
        )
    _record_cash_layer_relationships(account, record)
    _record_evidence(
        cash.opening_ledger_cash_evidence,
        cutoff_at,
        record,
        account_id=account.account_id,
        security_id=None,
        fields=("cash_state.opening_ledger_cash_evidence",),
        blocks=True,
        current=True,
        portfolio=True,
    )
    _record_evidence(
        cash.evidence,
        cutoff_at,
        record,
        account_id=account.account_id,
        security_id=None,
        fields=("cash_state.evidence",),
        blocks=True,
        current=True,
        portfolio=True,
    )


def _record_ledger_history_conflicts(
    account: AccountPositionSnapshot,
    prior_entries: dict[tuple[str, str], AuthoritativeLedgerEntry],
    record: ConflictRecorder,
) -> None:
    current_entry_ids = {entry.entry_id for entry in account.ledger_entries}
    for (prior_account_id, prior_entry_id), historical_entry in prior_entries.items():
        if prior_account_id == account.account_id and prior_entry_id not in current_entry_ids:
            record(
                account_id=account.account_id,
                security_id=historical_entry.security_id,
                code="LEDGER_ENTRY_REMOVED_ACROSS_SNAPSHOTS",
                fields=("ledger_entries.entry_id",),
                blocks_exact_statistical_quantity=True,
                portfolio_dependency=True,
                blocks_current_valuation=historical_entry.security_id is not None,
            )
    for entry in account.ledger_entries:
        prior = prior_entries.get((account.account_id, entry.entry_id))
        if (
            prior is not None
            and prior.model_dump()
            != AuthoritativeLedgerEntry(
                account_id=account.account_id,
                **entry.model_dump(),
            ).model_dump()
        ):
            record(
                account_id=account.account_id,
                security_id=entry.security_id,
                code="LEDGER_ENTRY_MUTATED_ACROSS_SNAPSHOTS",
                fields=("ledger_entries.entry_id",),
                blocks_exact_statistical_quantity=True,
                portfolio_dependency=True,
                blocks_current_valuation=entry.security_id is not None,
            )
        if entry.corrects_entry_id is not None and (
            entry.corrects_entry_id not in current_entry_ids
            and (account.account_id, entry.corrects_entry_id) not in prior_entries
        ):
            record(
                account_id=account.account_id,
                security_id=entry.security_id,
                code="LEDGER_CORRECTION_TARGET_MISSING",
                fields=("ledger_entries.corrects_entry_id",),
                blocks_exact_statistical_quantity=True,
                portfolio_dependency=True,
                blocks_current_valuation=entry.security_id is not None,
            )


def _reconstruct_ledger(
    account: AccountPositionSnapshot,
    cutoff_at: datetime,
    record: ConflictRecorder,
    output: list[AuthoritativeLedgerEntry],
) -> tuple[dict[str, tuple[Decimal, Decimal]], Decimal | None]:
    entries_by_security: dict[str, list[tuple[Decimal, Decimal]]] = defaultdict(list)
    cash_deltas: list[Decimal] = []
    cash_complete = True
    seen_ids: set[str] = set()
    for entry in account.ledger_entries:
        output.append(AuthoritativeLedgerEntry(account_id=account.account_id, **entry.model_dump()))
        if entry.entry_id in seen_ids:
            record(
                account_id=account.account_id,
                security_id=entry.security_id,
                code="LEDGER_ENTRY_ID_DUPLICATED",
                fields=("ledger_entries.entry_id",),
                blocks_exact_statistical_quantity=True,
                portfolio_dependency=True,
                blocks_current_valuation=entry.security_id is not None,
            )
            cash_complete = False
        seen_ids.add(entry.entry_id)
        if entry.occurred_at > cutoff_at:
            record(
                account_id=account.account_id,
                security_id=entry.security_id,
                code="LEDGER_ENTRY_AFTER_CUTOFF",
                fields=("ledger_entries.occurred_at",),
                blocks_exact_statistical_quantity=True,
                portfolio_dependency=True,
                blocks_current_valuation=entry.security_id is not None,
            )
            cash_complete = False
            continue
        evidence_codes = _record_evidence(
            entry.evidence,
            cutoff_at,
            record,
            account_id=account.account_id,
            security_id=entry.security_id,
            fields=("ledger_entries.evidence",),
            blocks=True,
            current=False,
            portfolio=True,
            valuation=entry.security_id is not None,
        )
        if evidence_codes:
            cash_complete = False
        cash_deltas.append(entry.cash_delta)
        if entry.security_id is not None:
            entries_by_security[entry.security_id].append(
                (entry.quantity_delta, entry.cost_basis_delta)
            )
    return (
        {
            security_id: (
                sum((quantity for quantity, _ in entries), Decimal("0")),
                sum((cost for _, cost in entries), Decimal("0")),
            )
            for security_id, entries in entries_by_security.items()
        },
        sum(cash_deltas, Decimal("0")) if cash_complete else None,
    )


def _reconcile_orders(
    account: AccountPositionSnapshot,
    cutoff_at: datetime,
    record: ConflictRecorder,
) -> tuple[dict[str, Decimal | None], Decimal | None]:
    sell_quantities: dict[str, list[Decimal | None]] = defaultdict(list)
    buy_reserves: list[Decimal | None] = []
    for order in account.open_orders:
        _record_evidence(
            order.evidence,
            cutoff_at,
            record,
            account_id=account.account_id,
            security_id=order.security_id,
            fields=("open_orders.evidence",),
            blocks=True,
            current=True,
            portfolio=order.side == "BUY",
        )
        if order.remaining_quantity is None:
            record(
                account_id=account.account_id,
                security_id=order.security_id,
                code=f"{order.side}_ORDER_REMAINING_QUANTITY_MISSING",
                fields=("open_orders.remaining_quantity",),
                blocks_exact_statistical_quantity=True,
                portfolio_dependency=order.side == "BUY",
            )
        elif order.remaining_quantity < 0:
            record(
                account_id=account.account_id,
                security_id=order.security_id,
                code=f"{order.side}_ORDER_REMAINING_QUANTITY_NEGATIVE",
                fields=("open_orders.remaining_quantity",),
                blocks_exact_statistical_quantity=True,
                portfolio_dependency=order.side == "BUY",
            )
        if order.side == "SELL":
            if order.reserved_cash is not None or order.reserved_cash_semantics != "NOT_APPLICABLE":
                record(
                    account_id=account.account_id,
                    security_id=order.security_id,
                    code="SELL_ORDER_CASH_RESERVATION_INVALID",
                    fields=("open_orders.reserved_cash", "open_orders.reserved_cash_semantics"),
                    blocks_exact_statistical_quantity=True,
                )
            sell_quantities[order.security_id].append(order.remaining_quantity)
            continue
        if order.reserved_cash_semantics != "BROKER_FINAL_RESERVED_CASH":
            record(
                account_id=account.account_id,
                security_id=order.security_id,
                code="BUY_ORDER_RESERVED_CASH_SEMANTICS_UNKNOWN",
                fields=("open_orders.reserved_cash_semantics",),
                blocks_exact_statistical_quantity=True,
                portfolio_dependency=True,
            )
        if order.reserved_cash is None:
            record(
                account_id=account.account_id,
                security_id=order.security_id,
                code="BUY_ORDER_RESERVED_CASH_MISSING",
                fields=("open_orders.reserved_cash",),
                blocks_exact_statistical_quantity=True,
                portfolio_dependency=True,
            )
        elif order.reserved_cash < 0:
            record(
                account_id=account.account_id,
                security_id=order.security_id,
                code="BUY_ORDER_RESERVED_CASH_NEGATIVE",
                fields=("open_orders.reserved_cash",),
                blocks_exact_statistical_quantity=True,
                portfolio_dependency=True,
            )
        buy_reserves.append(order.reserved_cash)
    return (
        {
            security_id: _sum_if_complete(tuple(values))
            for security_id, values in sell_quantities.items()
        },
        _sum_if_complete(tuple(buy_reserves)),
    )


def _record_buy_reserve(
    account: AccountPositionSnapshot,
    buy_reserves: Decimal | None,
    record: ConflictRecorder,
) -> None:
    if (
        buy_reserves is not None
        and account.cash_state.frozen_cash is not None
        and buy_reserves > account.cash_state.frozen_cash
    ):
        record(
            account_id=account.account_id,
            security_id=None,
            code="BUY_ORDER_RESERVE_EXCEEDS_FROZEN_CASH",
            fields=("open_orders.reserved_cash", "cash_state.frozen_cash"),
            blocks_exact_statistical_quantity=True,
            portfolio_dependency=True,
        )


def _record_cash_layer_relationships(
    account: AccountPositionSnapshot,
    record: ConflictRecorder,
) -> None:
    cash = account.cash_state
    for field in (
        "ledger_cash",
        "trading_cash",
        "transferable_cash",
        "frozen_cash",
        "receivable_cash",
        "payable_cash",
    ):
        value = getattr(cash, field)
        if value is not None and value < 0:
            record(
                account_id=account.account_id,
                security_id=None,
                code=f"CASH_{field.upper()}_NEGATIVE",
                fields=(f"cash_state.{field}",),
                blocks_exact_statistical_quantity=True,
                portfolio_dependency=True,
            )
    if (
        cash.ledger_cash is not None
        and cash.trading_cash is not None
        and cash.transferable_cash is not None
        and (cash.trading_cash > cash.ledger_cash or cash.transferable_cash > cash.trading_cash)
    ):
        record(
            account_id=account.account_id,
            security_id=None,
            code="CASH_AVAILABILITY_LAYERS_INCONSISTENT",
            fields=(
                "cash_state.ledger_cash",
                "cash_state.trading_cash",
                "cash_state.transferable_cash",
            ),
            blocks_exact_statistical_quantity=True,
            portfolio_dependency=True,
        )


def _record_cash_and_equity(
    account: AccountPositionSnapshot,
    ledger_cash_delta: Decimal | None,
    record: ConflictRecorder,
) -> None:
    cash = account.cash_state
    if (
        cash.opening_ledger_cash is not None
        and ledger_cash_delta is not None
        and cash.ledger_cash is not None
        and cash.opening_ledger_cash + ledger_cash_delta != cash.ledger_cash
    ):
        record(
            account_id=account.account_id,
            security_id=None,
            code="LEDGER_CASH_MISMATCH",
            fields=(
                "cash_state.ledger_cash",
                "cash_state.opening_ledger_cash",
                "ledger_entries.cash_delta",
            ),
            blocks_exact_statistical_quantity=True,
            portfolio_dependency=True,
        )
    values = tuple(
        position.total_quantity * position.market_price
        if position.total_quantity is not None
        and position.total_quantity >= 0
        and position.market_price is not None
        and position.market_price > 0
        else None
        for position in account.positions
    )
    market_value = _sum_if_complete(values)
    if (
        market_value is not None
        and cash.ledger_cash is not None
        and cash.receivable_cash is not None
        and cash.payable_cash is not None
        and account.account_equity is not None
        and cash.ledger_cash + market_value + cash.receivable_cash - cash.payable_cash
        != account.account_equity
    ):
        record(
            account_id=account.account_id,
            security_id=None,
            code="ACCOUNT_EQUITY_MISMATCH",
            fields=(
                "account_equity",
                "cash_state.ledger_cash",
                "positions.total_quantity",
                "positions.market_price",
                "cash_state.receivable_cash",
                "cash_state.payable_cash",
            ),
            blocks_exact_statistical_quantity=True,
            portfolio_dependency=True,
        )


def _record_position_problems(
    account: AccountPositionSnapshot,
    position: BrokerPositionFact,
    cutoff_at: datetime,
    ledger_by_security: dict[str, tuple[Decimal, Decimal]],
    sell_orders: dict[str, Decimal | None],
    record: ConflictRecorder,
) -> None:
    _record_evidence(
        position.evidence,
        cutoff_at,
        record,
        account_id=account.account_id,
        security_id=position.security_id,
        fields=("positions.evidence",),
        blocks=True,
        current=True,
        valuation=True,
        portfolio=True,
    )
    _record_quantity(
        account,
        position,
        "total_quantity",
        "TOTAL_QUANTITY",
        record,
        valuation=True,
        portfolio=True,
    )
    for field, prefix in (
        ("unsettled_quantity", "UNSETTLED_QUANTITY"),
        ("frozen_quantity", "FROZEN_QUANTITY"),
        ("restricted_quantity", "RESTRICTED_QUANTITY"),
        ("open_sell_order_quantity", "OPEN_SELL_ORDER_QUANTITY"),
    ):
        _record_quantity(account, position, field, prefix, record)
        value = getattr(position, field)
        if (
            value is not None
            and position.total_quantity is not None
            and position.total_quantity >= 0
            and value > position.total_quantity
        ):
            record(
                account_id=account.account_id,
                security_id=position.security_id,
                code=f"{prefix}_OUT_OF_BOUNDS",
                fields=(f"positions.{field}", "positions.total_quantity"),
                blocks_exact_statistical_quantity=True,
            )
    if position.broker_sellable_quantity is None:
        record(
            account_id=account.account_id,
            security_id=position.security_id,
            code="BROKER_SELLABLE_QUANTITY_MISSING",
            fields=("positions.broker_sellable_quantity",),
            blocks_exact_statistical_quantity=True,
        )
    elif position.broker_sellable_quantity < 0 or (
        position.total_quantity is not None
        and position.total_quantity >= 0
        and position.broker_sellable_quantity > position.total_quantity
    ):
        record(
            account_id=account.account_id,
            security_id=position.security_id,
            code="BROKER_SELLABLE_QUANTITY_OUT_OF_BOUNDS",
            fields=("positions.broker_sellable_quantity", "positions.total_quantity"),
            blocks_exact_statistical_quantity=True,
        )
    if position.sellable_quantity_semantics == "UNKNOWN":
        record(
            account_id=account.account_id,
            security_id=position.security_id,
            code="SELLABLE_QUANTITY_SEMANTICS_UNKNOWN",
            fields=("positions.sellable_quantity_semantics",),
            blocks_exact_statistical_quantity=True,
        )
    if position.encumbrance_quantity_semantics == "UNKNOWN":
        record(
            account_id=account.account_id,
            security_id=position.security_id,
            code="ENCUMBRANCE_QUANTITY_SEMANTICS_UNKNOWN",
            fields=("positions.encumbrance_quantity_semantics",),
            blocks_exact_statistical_quantity=True,
        )
    elif (
        position.total_quantity is not None
        and position.total_quantity >= 0
        and position.broker_sellable_quantity is not None
        and position.broker_sellable_quantity >= 0
    ):
        encumbered_quantity = max(
            position.unsettled_quantity or Decimal("0"),
            position.frozen_quantity or Decimal("0"),
            position.restricted_quantity or Decimal("0"),
            position.open_sell_order_quantity or Decimal("0"),
        )
        if position.broker_sellable_quantity + encumbered_quantity > position.total_quantity:
            record(
                account_id=account.account_id,
                security_id=position.security_id,
                code="BROKER_SELLABLE_QUANTITY_CONTRADICTS_ENCUMBRANCES",
                fields=(
                    "positions.broker_sellable_quantity",
                    "positions.unsettled_quantity",
                    "positions.frozen_quantity",
                    "positions.restricted_quantity",
                    "positions.open_sell_order_quantity",
                    "positions.total_quantity",
                ),
                blocks_exact_statistical_quantity=True,
            )
    if position.reported_cost_basis is None:
        record(
            account_id=account.account_id,
            security_id=position.security_id,
            code="COST_BASIS_MISSING",
            fields=("positions.reported_cost_basis",),
            blocks_exact_statistical_quantity=True,
        )
    elif position.reported_cost_basis < 0:
        record(
            account_id=account.account_id,
            security_id=position.security_id,
            code="COST_BASIS_NEGATIVE",
            fields=("positions.reported_cost_basis",),
            blocks_exact_statistical_quantity=True,
        )
    if position.market_price is None:
        record(
            account_id=account.account_id,
            security_id=position.security_id,
            code="MARKET_PRICE_MISSING",
            fields=("positions.market_price",),
            blocks_exact_statistical_quantity=True,
            portfolio_dependency=True,
            blocks_current_valuation=True,
        )
    elif position.market_price <= 0:
        record(
            account_id=account.account_id,
            security_id=position.security_id,
            code="MARKET_PRICE_NON_POSITIVE",
            fields=("positions.market_price",),
            blocks_exact_statistical_quantity=True,
            portfolio_dependency=True,
            blocks_current_valuation=True,
        )
    quantity, cost = ledger_by_security.get(position.security_id, (Decimal("0"), Decimal("0")))
    if position.total_quantity is not None and quantity != position.total_quantity:
        record(
            account_id=account.account_id,
            security_id=position.security_id,
            code="LEDGER_QUANTITY_MISMATCH",
            fields=("positions.total_quantity", "ledger_entries.quantity_delta"),
            blocks_exact_statistical_quantity=True,
            blocks_current_valuation=True,
        )
    if position.reported_cost_basis is not None and cost != position.reported_cost_basis:
        record(
            account_id=account.account_id,
            security_id=position.security_id,
            code="LEDGER_COST_BASIS_MISMATCH",
            fields=("positions.reported_cost_basis", "ledger_entries.cost_basis_delta"),
            blocks_exact_statistical_quantity=True,
        )
    order_quantity = sell_orders.get(position.security_id, Decimal("0"))
    if (
        position.open_sell_order_quantity is None
        or order_quantity is None
        or position.open_sell_order_quantity != order_quantity
    ):
        record(
            account_id=account.account_id,
            security_id=position.security_id,
            code="OPEN_SELL_ORDER_MISMATCH",
            fields=("positions.open_sell_order_quantity", "open_orders.remaining_quantity"),
            blocks_exact_statistical_quantity=True,
        )


def _record_quantity(
    account: AccountPositionSnapshot,
    position: BrokerPositionFact,
    field: str,
    prefix: str,
    record: ConflictRecorder,
    *,
    valuation: bool = False,
    portfolio: bool = False,
) -> None:
    value = getattr(position, field)
    if value is None:
        code = f"{prefix}_MISSING"
    elif value < 0:
        code = f"{prefix}_NEGATIVE"
    else:
        return
    record(
        account_id=account.account_id,
        security_id=position.security_id,
        code=code,
        fields=(f"positions.{field}",),
        blocks_exact_statistical_quantity=True,
        portfolio_dependency=portfolio,
        blocks_current_valuation=valuation,
    )


def _project_actions(
    rows: list[tuple[AccountPositionSnapshot, BrokerPositionFact]],
    active_restrictions: dict[tuple[str, str | None], list[ExecutionRestriction]],
    blocking: dict[tuple[str | None, str | None], list[str]],
    portfolio_blocking: list[str],
) -> tuple[PositionActionUnit, ...]:
    actions: list[PositionActionUnit] = []
    for account, position in rows:
        restrictions = (
            *active_restrictions[(account.account_id, None)],
            *active_restrictions[(account.account_id, position.security_id)],
        )
        reasons = tuple(
            dict.fromkeys(
                (
                    *portfolio_blocking,
                    *_scoped_reasons(blocking, account.account_id, position.security_id),
                    *(
                        f"EXECUTION_RESTRICTION_ACTIVE:{restriction.restriction_id}"
                        for restriction in restrictions
                    ),
                )
            )
        )
        exact = (
            position.broker_sellable_quantity
            if position.broker_sellable_quantity is not None
            and position.sellable_quantity_semantics == "BROKER_FINAL_SELLABLE"
            and not reasons
            else None
        )
        actions.append(
            PositionActionUnit(
                account_id=account.account_id,
                account_type=account.account_type,
                currency=account.currency,
                position_id=position.position_id,
                origin=position.origin,
                lifecycle_id=position.lifecycle_id,
                issuer_id=position.issuer_id,
                security_id=position.security_id,
                total_quantity=position.total_quantity,
                broker_sellable_quantity=position.broker_sellable_quantity,
                sellable_quantity_semantics=position.sellable_quantity_semantics,
                encumbrance_quantity_semantics=position.encumbrance_quantity_semantics,
                unsettled_quantity=position.unsettled_quantity,
                frozen_quantity=position.frozen_quantity,
                restricted_quantity=position.restricted_quantity,
                open_sell_order_quantity=position.open_sell_order_quantity,
                reported_cost_basis=position.reported_cost_basis,
                market_price=position.market_price,
                exact_statistical_action_quantity=exact,
                exact_quantity_status="AVAILABLE" if exact is not None else "BLOCKED",
                reasons=reasons,
                position_evidence=position.evidence,
            )
        )
    return tuple(actions)


def _project_exposures(
    rows: list[tuple[AccountPositionSnapshot, BrokerPositionFact]],
    currency: str,
    valuation: dict[tuple[str | None, str | None], list[str]],
    missing_summary_security_ids: set[str],
    conflicting_security_ids: set[str],
) -> tuple[IssuerExposure, ...]:
    issuer_rows: dict[str, list[tuple[str, BrokerPositionFact]]] = defaultdict(list)
    invalid_issuers: set[str] = set()
    for account, position in rows:
        issuer_rows[position.issuer_id].append((account.account_id, position))
        if (
            position.security_id in conflicting_security_ids
            or position.security_id in missing_summary_security_ids
            or _scoped_reasons(valuation, account.account_id, position.security_id)
        ):
            invalid_issuers.add(position.issuer_id)
    return tuple(
        IssuerExposure(
            issuer_id=issuer_id,
            valuation_currency=currency,
            account_ids=tuple(dict.fromkeys(account_id for account_id, _ in values)),
            current_market_exposure=(
                None
                if issuer_id in invalid_issuers
                else sum(
                    (
                        position.total_quantity * position.market_price
                        for _, position in values
                        if position.total_quantity is not None and position.market_price is not None
                    ),
                    Decimal("0"),
                )
            ),
        )
        for issuer_id, values in sorted(issuer_rows.items())
    )


def _record_evidence(
    evidence: PositionEvidence,
    cutoff_at: datetime,
    record: ConflictRecorder,
    *,
    account_id: str | None,
    security_id: str | None,
    fields: tuple[str, ...],
    blocks: bool,
    current: bool,
    portfolio: bool = False,
    valuation: bool = False,
) -> tuple[str, ...]:
    codes = evidence.problem_codes(
        cutoff_at,
        require_current_completeness=current,
    )
    for code in codes:
        record(
            account_id=account_id,
            security_id=security_id,
            code=code,
            fields=fields,
            blocks_exact_statistical_quantity=blocks,
            portfolio_dependency=portfolio,
            blocks_current_valuation=valuation,
        )
    return codes


def _scoped_reasons(
    values: dict[tuple[str | None, str | None], list[str]],
    account_id: str,
    security_id: str,
) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            (
                *values[(None, None)],
                *values[(None, security_id)],
                *values[(account_id, None)],
                *values[(account_id, security_id)],
            )
        )
    )


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _conflict_id(
    account_id: str | None,
    security_id: str | None,
    code: str,
    fields: tuple[str, ...],
) -> str:
    identity = json.dumps(
        [account_id, security_id, code, fields],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return f"position-conflict:{sha256(identity.encode()).hexdigest()}"


def _sum_if_complete(values: tuple[Decimal | None, ...]) -> Decimal | None:
    if not all(value is not None for value in values):
        return None
    return sum((value for value in values if value is not None), Decimal("0"))
