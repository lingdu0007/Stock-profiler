"""Deterministic reconciliation of broker-authoritative position-state facts."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal
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


class ConflictRecorder(Protocol):
    """Type the host-local callback that records a retained reconciliation conflict."""

    def __call__(
        self,
        *,
        account_id: str | None,
        security_id: str | None,
        code: str,
        fields: tuple[str, ...],
        blocks_exact_statistical_quantity: bool,
    ) -> None: ...


def reconcile(command: PositionSnapshotCommand) -> PositionReconciliationOutcome:
    """Retain conflicts and only expose a precise quantity when broker facts agree."""
    conflicts: list[PositionFactConflict] = []
    blocking_scopes: dict[tuple[str | None, str | None], list[str]] = defaultdict(list)

    def record_conflict(
        *,
        account_id: str | None,
        security_id: str | None,
        code: str,
        fields: tuple[str, ...],
        blocks_exact_statistical_quantity: bool,
    ) -> None:
        scope = (account_id, security_id)
        conflict = PositionFactConflict(
            conflict_id=_conflict_id(account_id, security_id, code),
            code=code,
            affected_scope=PositionAffectedScope(
                account_id=account_id,
                security_id=security_id,
                fields=fields,
            ),
            blocks_exact_statistical_quantity=blocks_exact_statistical_quantity,
        )
        if conflict not in conflicts:
            conflicts.append(conflict)
        if blocks_exact_statistical_quantity:
            blocking_scopes[scope].append(code)

    _record_evidence_problems(
        command.snapshot_evidence,
        command.cutoff_at,
        record_conflict,
        account_id=None,
        security_id=None,
        fields=("snapshot_evidence",),
        blocks_exact_statistical_quantity=True,
    )

    action_units: list[PositionActionUnit] = []
    cash_states: list[AccountCashState] = []
    unfinished_orders: list[AccountOpenOrder] = []
    execution_restrictions: list[AccountExecutionRestriction] = []
    authoritative_ledger: list[AuthoritativeLedgerEntry] = []
    issuer_account_exposure: dict[str, list[tuple[str, Decimal | None, bool]]] = defaultdict(list)
    issuer_by_security: dict[str, set[str]] = defaultdict(set)
    unreconciled_security_ids: set[str] = set()
    account_equities: list[Decimal | None] = []

    for annotation in command.annotations:
        if annotation.created_at > command.cutoff_at:
            record_conflict(
                account_id=annotation.account_id,
                security_id=annotation.security_id,
                code="ANNOTATION_AFTER_CUTOFF",
                fields=("annotations.created_at",),
                blocks_exact_statistical_quantity=False,
            )

    for account in command.accounts:
        _record_account_problems(account, command.cutoff_at, record_conflict)
        account_equities.append(account.account_equity)
        cash = account.cash_state
        cash_states.append(
            AccountCashState(
                account_id=account.account_id,
                account_type=account.account_type,
                currency=account.currency,
                account_equity=account.account_equity,
                ledger_cash=cash.ledger_cash,
                trading_cash=cash.trading_cash,
                transferable_cash=cash.transferable_cash,
                frozen_cash=cash.frozen_cash,
                receivable_cash=cash.receivable_cash,
                payable_cash=cash.payable_cash,
            )
        )
        unfinished_orders.extend(
            AccountOpenOrder(account_id=account.account_id, **order.model_dump())
            for order in account.open_orders
        )
        execution_restrictions.extend(
            AccountExecutionRestriction(
                account_id=account.account_id,
                **restriction.model_dump(),
            )
            for restriction in account.execution_restrictions
        )
        ledger_by_security = _ledger_by_security(
            account,
            command.cutoff_at,
            record_conflict,
            authoritative_ledger,
        )
        open_sell_quantity = _open_sell_quantity_by_security(
            account,
            command.cutoff_at,
            record_conflict,
        )
        active_restrictions = _active_restrictions_by_security(
            account,
            command.cutoff_at,
            record_conflict,
        )
        position_security_ids = {position.security_id for position in account.positions}
        for position in account.positions:
            issuer_by_security[position.security_id].add(position.issuer_id)
            _record_position_problems(
                account,
                position,
                command.cutoff_at,
                ledger_by_security,
                open_sell_quantity,
                record_conflict,
            )
            scoped_reasons = _blocking_reasons(
                blocking_scopes,
                account.account_id,
                position.security_id,
            )
            restrictions = (
                *active_restrictions.get(None, ()),
                *active_restrictions.get(position.security_id, ()),
            )
            restriction_reasons = tuple(
                f"EXECUTION_RESTRICTION_ACTIVE:{restriction.restriction_id}"
                for restriction in restrictions
            )
            reasons = tuple(dict.fromkeys((*scoped_reasons, *restriction_reasons)))
            exact_quantity = (
                position.broker_sellable_quantity
                if position.broker_sellable_quantity is not None
                and position.sellable_quantity_semantics == "BROKER_FINAL_SELLABLE"
                and not reasons
                else None
            )
            action_units.append(
                PositionActionUnit(
                    account_id=account.account_id,
                    account_type=account.account_type,
                    currency=account.currency,
                    issuer_id=position.issuer_id,
                    security_id=position.security_id,
                    total_quantity=position.total_quantity,
                    broker_sellable_quantity=position.broker_sellable_quantity,
                    unsettled_quantity=position.unsettled_quantity,
                    frozen_quantity=position.frozen_quantity,
                    restricted_quantity=position.restricted_quantity,
                    open_sell_order_quantity=position.open_sell_order_quantity,
                    exact_statistical_action_quantity=exact_quantity,
                    exact_quantity_status=(
                        "AVAILABLE" if exact_quantity is not None else "BLOCKED"
                    ),
                    reasons=reasons,
                )
            )
            issuer_account_exposure[position.issuer_id].append(
                (
                    account.account_id,
                    (
                        position.total_quantity * position.market_price
                        if position.total_quantity is not None and position.market_price is not None
                        else None
                    ),
                    bool(reasons),
                )
            )
        reconciled_security_ids = (
            set(ledger_by_security)
            | set(open_sell_quantity)
            | {security_id for security_id in active_restrictions if security_id is not None}
        )
        for security_id in reconciled_security_ids - position_security_ids:
            record_conflict(
                account_id=account.account_id,
                security_id=security_id,
                code="BROKER_POSITION_SUMMARY_MISSING",
                fields=("positions", "ledger_entries", "open_orders", "execution_restrictions"),
                blocks_exact_statistical_quantity=True,
            )
            unreconciled_security_ids.add(security_id)

    issuer_exposures = tuple(
        IssuerExposure(
            issuer_id=issuer_id,
            valuation_currency=command.valuation_currency,
            account_ids=tuple(account_id for account_id, _, _ in exposures),
            current_market_exposure=(
                None
                if (
                    issuer_id
                    in {
                        issuer
                        for security_id in unreconciled_security_ids
                        for issuer in issuer_by_security.get(security_id, ())
                    }
                    or any(has_conflict for _, _, has_conflict in exposures)
                )
                else _sum_if_complete(tuple(exposure for _, exposure, _ in exposures))
            ),
        )
        for issuer_id, exposures in issuer_account_exposure.items()
    )
    total_account_equity = _sum_if_complete(tuple(account_equities))
    snapshot = ReconciledPositionSnapshot(
        snapshot_id=command.snapshot_id,
        cutoff_at=command.cutoff_at,
        valuation_currency=command.valuation_currency,
        snapshot_source=command.snapshot_evidence.source,
        evidence_clock=command.snapshot_evidence.clock,
        total_account_equity=total_account_equity,
        action_units=tuple(action_units),
        issuer_exposures=issuer_exposures,
        cash_states=tuple(cash_states),
        unfinished_orders=tuple(unfinished_orders),
        execution_restrictions=tuple(execution_restrictions),
        authoritative_ledger=tuple(authoritative_ledger),
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
    record_conflict: ConflictRecorder,
) -> None:
    _record_evidence_problems(
        account.snapshot_evidence,
        cutoff_at,
        record_conflict,
        account_id=account.account_id,
        security_id=None,
        fields=("snapshot_evidence",),
        blocks_exact_statistical_quantity=True,
    )
    if account.account_equity is None:
        record_conflict(
            account_id=account.account_id,
            security_id=None,
            code="ACCOUNT_EQUITY_MISSING",
            fields=("account_equity",),
            blocks_exact_statistical_quantity=True,
        )
    _record_evidence_problems(
        account.account_equity_evidence,
        cutoff_at,
        record_conflict,
        account_id=account.account_id,
        security_id=None,
        fields=("account_equity_evidence",),
        blocks_exact_statistical_quantity=True,
    )
    cash_fields = (
        "ledger_cash",
        "trading_cash",
        "transferable_cash",
        "frozen_cash",
        "receivable_cash",
        "payable_cash",
    )
    if any(getattr(account.cash_state, field) is None for field in cash_fields):
        record_conflict(
            account_id=account.account_id,
            security_id=None,
            code="CASH_STATE_INCOMPLETE",
            fields=cash_fields,
            blocks_exact_statistical_quantity=True,
        )
    _record_evidence_problems(
        account.cash_state.evidence,
        cutoff_at,
        record_conflict,
        account_id=account.account_id,
        security_id=None,
        fields=("cash_state.evidence",),
        blocks_exact_statistical_quantity=True,
    )


def _ledger_by_security(
    account: AccountPositionSnapshot,
    cutoff_at: datetime,
    record_conflict: ConflictRecorder,
    authoritative_ledger: list[AuthoritativeLedgerEntry],
) -> dict[str, tuple[Decimal, Decimal]]:
    entries_by_security: dict[str, list[tuple[Decimal, Decimal]]] = defaultdict(list)
    seen_entry_ids: set[str] = set()
    for entry in account.ledger_entries:
        if entry.entry_id in seen_entry_ids:
            record_conflict(
                account_id=account.account_id,
                security_id=entry.security_id,
                code="LEDGER_ENTRY_ID_DUPLICATED",
                fields=("ledger_entries.entry_id",),
                blocks_exact_statistical_quantity=True,
            )
        seen_entry_ids.add(entry.entry_id)
        if entry.occurred_at > cutoff_at:
            record_conflict(
                account_id=account.account_id,
                security_id=entry.security_id,
                code="LEDGER_ENTRY_AFTER_CUTOFF",
                fields=("ledger_entries.occurred_at",),
                blocks_exact_statistical_quantity=entry.security_id is not None,
            )
        _record_evidence_problems(
            entry.evidence,
            cutoff_at,
            record_conflict,
            account_id=account.account_id,
            security_id=entry.security_id,
            fields=("ledger_entries.evidence",),
            blocks_exact_statistical_quantity=entry.security_id is not None,
        )
        authoritative_ledger.append(
            AuthoritativeLedgerEntry(account_id=account.account_id, **entry.model_dump())
        )
        if entry.security_id is not None:
            entries_by_security[entry.security_id].append(
                (entry.quantity_delta, entry.cost_basis_delta)
            )
    return {
        security_id: (
            sum((quantity for quantity, _ in entries), Decimal("0")),
            sum((cost for _, cost in entries), Decimal("0")),
        )
        for security_id, entries in entries_by_security.items()
    }


def _open_sell_quantity_by_security(
    account: AccountPositionSnapshot,
    cutoff_at: datetime,
    record_conflict: ConflictRecorder,
) -> dict[str, Decimal | None]:
    quantities: dict[str, list[Decimal | None]] = defaultdict(list)
    for order in account.open_orders:
        _record_evidence_problems(
            order.evidence,
            cutoff_at,
            record_conflict,
            account_id=account.account_id,
            security_id=order.security_id,
            fields=("open_orders.evidence",),
            blocks_exact_statistical_quantity=True,
        )
        if order.side == "SELL":
            quantities[order.security_id].append(order.remaining_quantity)
    return {
        security_id: _sum_if_complete(tuple(values)) for security_id, values in quantities.items()
    }


def _active_restrictions_by_security(
    account: AccountPositionSnapshot,
    cutoff_at: datetime,
    record_conflict: ConflictRecorder,
) -> dict[str | None, tuple[ExecutionRestriction, ...]]:
    by_security: dict[str | None, list[ExecutionRestriction]] = defaultdict(list)
    for restriction in account.execution_restrictions:
        _record_evidence_problems(
            restriction.evidence,
            cutoff_at,
            record_conflict,
            account_id=account.account_id,
            security_id=restriction.security_id,
            fields=("execution_restrictions.evidence",),
            blocks_exact_statistical_quantity=True,
        )
        if restriction.active:
            by_security[restriction.security_id].append(restriction)
    return {security_id: tuple(items) for security_id, items in by_security.items()}


def _record_position_problems(
    account: AccountPositionSnapshot,
    position: BrokerPositionFact,
    cutoff_at: datetime,
    ledger_by_security: dict[str, tuple[Decimal, Decimal]],
    open_sell_quantity: dict[str, Decimal | None],
    record_conflict: ConflictRecorder,
) -> None:
    _record_evidence_problems(
        position.evidence,
        cutoff_at,
        record_conflict,
        account_id=account.account_id,
        security_id=position.security_id,
        fields=("positions.evidence",),
        blocks_exact_statistical_quantity=True,
    )
    if position.total_quantity is None:
        record_conflict(
            account_id=account.account_id,
            security_id=position.security_id,
            code="TOTAL_QUANTITY_MISSING",
            fields=("positions.total_quantity",),
            blocks_exact_statistical_quantity=True,
        )
    if position.broker_sellable_quantity is None:
        record_conflict(
            account_id=account.account_id,
            security_id=position.security_id,
            code="BROKER_SELLABLE_QUANTITY_MISSING",
            fields=("positions.broker_sellable_quantity",),
            blocks_exact_statistical_quantity=True,
        )
    elif position.broker_sellable_quantity < 0 or (
        position.total_quantity is not None
        and position.broker_sellable_quantity > position.total_quantity
    ):
        record_conflict(
            account_id=account.account_id,
            security_id=position.security_id,
            code="BROKER_SELLABLE_QUANTITY_OUT_OF_BOUNDS",
            fields=("positions.broker_sellable_quantity", "positions.total_quantity"),
            blocks_exact_statistical_quantity=True,
        )
    if position.sellable_quantity_semantics == "UNKNOWN":
        record_conflict(
            account_id=account.account_id,
            security_id=position.security_id,
            code="SELLABLE_QUANTITY_SEMANTICS_UNKNOWN",
            fields=("positions.sellable_quantity_semantics",),
            blocks_exact_statistical_quantity=True,
        )
    for field_name, code in (
        ("unsettled_quantity", "UNSETTLED_QUANTITY_MISSING"),
        ("frozen_quantity", "FROZEN_QUANTITY_MISSING"),
        ("restricted_quantity", "RESTRICTED_QUANTITY_MISSING"),
    ):
        if getattr(position, field_name) is None:
            record_conflict(
                account_id=account.account_id,
                security_id=position.security_id,
                code=code,
                fields=(f"positions.{field_name}",),
                blocks_exact_statistical_quantity=True,
            )
    if position.reported_cost_basis is None:
        record_conflict(
            account_id=account.account_id,
            security_id=position.security_id,
            code="COST_BASIS_MISSING",
            fields=("positions.reported_cost_basis",),
            blocks_exact_statistical_quantity=True,
        )
    if position.market_price is None:
        record_conflict(
            account_id=account.account_id,
            security_id=position.security_id,
            code="MARKET_PRICE_MISSING",
            fields=("positions.market_price",),
            blocks_exact_statistical_quantity=True,
        )
    reconstructed_quantity, reconstructed_cost = ledger_by_security.get(
        position.security_id,
        (Decimal("0"), Decimal("0")),
    )
    if position.total_quantity is not None and reconstructed_quantity != position.total_quantity:
        record_conflict(
            account_id=account.account_id,
            security_id=position.security_id,
            code="LEDGER_QUANTITY_MISMATCH",
            fields=("positions.total_quantity", "ledger_entries.quantity_delta"),
            blocks_exact_statistical_quantity=True,
        )
    if (
        position.reported_cost_basis is not None
        and reconstructed_cost != position.reported_cost_basis
    ):
        record_conflict(
            account_id=account.account_id,
            security_id=position.security_id,
            code="LEDGER_COST_BASIS_MISMATCH",
            fields=("positions.reported_cost_basis", "ledger_entries.cost_basis_delta"),
            blocks_exact_statistical_quantity=True,
        )
    order_quantity = open_sell_quantity.get(position.security_id, Decimal("0"))
    if (
        position.open_sell_order_quantity is None
        or order_quantity is None
        or position.open_sell_order_quantity != order_quantity
    ):
        record_conflict(
            account_id=account.account_id,
            security_id=position.security_id,
            code="OPEN_SELL_ORDER_MISMATCH",
            fields=("positions.open_sell_order_quantity", "open_orders.remaining_quantity"),
            blocks_exact_statistical_quantity=True,
        )


def _record_evidence_problems(
    evidence: PositionEvidence,
    cutoff_at: datetime,
    record_conflict: ConflictRecorder,
    *,
    account_id: str | None,
    security_id: str | None,
    fields: tuple[str, ...],
    blocks_exact_statistical_quantity: bool,
) -> None:
    for code in evidence.problem_codes(cutoff_at):
        record_conflict(
            account_id=account_id,
            security_id=security_id,
            code=code,
            fields=fields,
            blocks_exact_statistical_quantity=blocks_exact_statistical_quantity,
        )


def _blocking_reasons(
    blocking_scopes: dict[tuple[str | None, str | None], list[str]],
    account_id: str,
    security_id: str,
) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            (
                *blocking_scopes.get((None, None), ()),
                *blocking_scopes.get((account_id, None), ()),
                *blocking_scopes.get((account_id, security_id), ()),
            )
        )
    )


def _conflict_id(account_id: str | None, security_id: str | None, code: str) -> str:
    account = account_id or "SNAPSHOT"
    security = security_id or "ACCOUNT"
    return f"position-conflict:{account}:{security}:{code}"


def _sum_if_complete(values: tuple[Decimal | None, ...]) -> Decimal | None:
    """Keep missing-value aggregate semantics identical across snapshot projections."""
    if not all(value is not None for value in values):
        return None
    return sum((value for value in values if value is not None), Decimal("0"))
