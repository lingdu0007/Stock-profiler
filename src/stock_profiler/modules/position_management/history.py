"""Resolve authoritative position history outside persistence concerns."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from stock_profiler.modules.position_management.contracts import (
    AccountCashState,
    AuthoritativeLedgerEntry,
    PositionReconciliationOutcome,
)
from stock_profiler.modules.position_management.contracts import (
    ledger_entry_evidence_is_visible as ledger_entry_evidence_is_visible,
)


def authoritative_ledger_history(
    position_history: Iterable[PositionReconciliationOutcome],
    *,
    account_ids: frozenset[str],
    cutoff_at: datetime,
) -> tuple[AuthoritativeLedgerEntry, ...]:
    """Return first-observed valid ledger facts at a frozen cutoff."""
    resolver = _LedgerHistoryResolver()
    for position in position_history:
        entries_by_account: dict[str, list[AuthoritativeLedgerEntry]] = {}
        for entry in position.snapshot.authoritative_ledger:
            if entry.account_id in account_ids:
                entries_by_account.setdefault(entry.account_id, []).append(entry)
        for account_id, entries in entries_by_account.items():
            resolver.accept_if_preserving(
                _LedgerSnapshot.from_entries(account_id, tuple(entries)),
                position.snapshot.cutoff_at,
            )
    return resolver.entries_at(cutoff_at)


def authoritative_cash_history(
    position_history: Iterable[PositionReconciliationOutcome],
    *,
    account_ids: frozenset[str],
    cutoff_at: datetime,
) -> tuple[AccountCashState, ...]:
    """Return first-observed valid opening cash baselines at a frozen cutoff."""
    resolver = _CashHistoryResolver()
    for position in position_history:
        for cash_state in position.snapshot.cash_states:
            if cash_state.account_id in account_ids:
                resolver.accept_if_preserving(cash_state, position.snapshot.cutoff_at)
    return resolver.cash_states_at(cutoff_at)


@dataclass(frozen=True)
class _LedgerSnapshot:
    account_id: str
    entries: tuple[AuthoritativeLedgerEntry, ...]
    entries_by_id: dict[str, tuple[AuthoritativeLedgerEntry, ...]]
    visible_after_by_entry_id: dict[str, datetime | None]

    @classmethod
    def from_entries(
        cls,
        account_id: str,
        entries: tuple[AuthoritativeLedgerEntry, ...],
    ) -> _LedgerSnapshot:
        entries_by_id: dict[str, list[AuthoritativeLedgerEntry]] = {}
        for entry in entries:
            entries_by_id.setdefault(entry.entry_id, []).append(entry)
        indexed_entries = {
            entry_id: tuple(entries_for_id) for entry_id, entries_for_id in entries_by_id.items()
        }
        unique_entries = {
            entry_id: entries_for_id[0]
            for entry_id, entries_for_id in indexed_entries.items()
            if len(entries_for_id) == 1
        }
        visible_after_by_entry_id: dict[str, datetime | None] = {}

        def visible_after(
            entry_id: str,
            lineage: frozenset[str] = frozenset(),
        ) -> datetime | None:
            if entry_id in visible_after_by_entry_id:
                return visible_after_by_entry_id[entry_id]
            if entry_id in lineage:
                return None
            entry = unique_entries.get(entry_id)
            if entry is None or entry.evidence.cutoff_at is None:
                visible_after_by_entry_id[entry_id] = None
                return None
            entry_visible_after = max(entry.occurred_at, entry.evidence.cutoff_at)
            if not ledger_entry_evidence_is_visible(entry, entry_visible_after) or (
                entry.security_id is None
                and (
                    entry.entry_type in {"FILL", "CORPORATE_ACTION"}
                    or entry.quantity_delta != 0
                    or entry.cost_basis_delta != 0
                )
            ):
                visible_after_by_entry_id[entry_id] = None
                return None
            if entry.corrects_entry_id is None:
                visible_after_by_entry_id[entry_id] = entry_visible_after
                return entry_visible_after
            target_id = entry.corrects_entry_id
            target = unique_entries.get(target_id)
            if (
                target is None
                or target_id == entry.entry_id
                or entry.evidence.source_observed_at is None
                or entry.evidence.source_observed_at < target.occurred_at
            ):
                visible_after_by_entry_id[entry_id] = None
                return None
            target_visible_after = visible_after(target_id, lineage | {entry_id})
            if target_visible_after is None:
                visible_after_by_entry_id[entry_id] = None
                return None
            visible_after_by_entry_id[entry_id] = max(
                entry_visible_after,
                target_visible_after,
            )
            return visible_after_by_entry_id[entry_id]

        for entry_id in indexed_entries:
            visible_after(entry_id)
        return cls(
            account_id=account_id,
            entries=entries,
            entries_by_id=indexed_entries,
            visible_after_by_entry_id=visible_after_by_entry_id,
        )

    def preserves(self, canonical_entries: dict[str, AuthoritativeLedgerEntry]) -> bool:
        """Return whether this account snapshot retains every established fact."""
        return all(
            (current_entries := self.entries_by_id.get(entry_id, ()))
            and len(current_entries) == 1
            and current_entries[0].model_dump() == canonical_entry.model_dump()
            for entry_id, canonical_entry in canonical_entries.items()
        )

    def add_visible_entries_to(
        self,
        canonical_entries: dict[tuple[str, str], AuthoritativeLedgerEntry],
        cutoff_at: datetime,
    ) -> None:
        """Add this snapshot's valid first-observed entries to one canonical cache."""
        for entry in self.entries:
            key = (self.account_id, entry.entry_id)
            visible_after = self.visible_after_by_entry_id.get(entry.entry_id)
            if (
                key not in canonical_entries
                and visible_after is not None
                and visible_after <= cutoff_at
            ):
                canonical_entries[key] = entry


class _LedgerHistoryResolver:
    """Incrementally retain accepted snapshots and their cutoff-specific canons."""

    def __init__(self) -> None:
        self._accepted_snapshots: list[_LedgerSnapshot] = []
        self._canonical_by_cutoff: dict[
            datetime,
            dict[tuple[str, str], AuthoritativeLedgerEntry],
        ] = {}

    def accept_if_preserving(
        self,
        snapshot: _LedgerSnapshot,
        cutoff_at: datetime,
    ) -> None:
        canonical_entries = self._canonical_entries_at(cutoff_at)
        canonical_account_entries = {
            entry_id: entry
            for (account_id, entry_id), entry in canonical_entries.items()
            if account_id == snapshot.account_id
        }
        if not snapshot.preserves(canonical_account_entries):
            return
        self._accepted_snapshots.append(snapshot)
        for cached_cutoff, cached_entries in self._canonical_by_cutoff.items():
            snapshot.add_visible_entries_to(cached_entries, cached_cutoff)

    def entries_at(self, cutoff_at: datetime) -> tuple[AuthoritativeLedgerEntry, ...]:
        return tuple(self._canonical_entries_at(cutoff_at).values())

    def _canonical_entries_at(
        self,
        cutoff_at: datetime,
    ) -> dict[tuple[str, str], AuthoritativeLedgerEntry]:
        cached_entries = self._canonical_by_cutoff.get(cutoff_at)
        if cached_entries is not None:
            return cached_entries
        canonical_entries: dict[tuple[str, str], AuthoritativeLedgerEntry] = {}
        for snapshot in self._accepted_snapshots:
            snapshot.add_visible_entries_to(canonical_entries, cutoff_at)
        self._canonical_by_cutoff[cutoff_at] = canonical_entries
        return canonical_entries


class _CashHistoryResolver:
    """Incrementally retain accepted opening-cash baselines by cutoff."""

    def __init__(self) -> None:
        self._accepted_cash_states: list[AccountCashState] = []
        self._canonical_by_cutoff: dict[datetime, dict[str, AccountCashState]] = {}

    def accept_if_preserving(self, cash_state: AccountCashState, cutoff_at: datetime) -> None:
        if not _opening_cash_is_historically_valid(cash_state, cutoff_at):
            return
        existing = self._cash_states_at(cutoff_at).get(cash_state.account_id)
        if existing is not None and existing.opening_ledger_cash != cash_state.opening_ledger_cash:
            return
        self._accepted_cash_states.append(cash_state)
        for cached_cutoff, cached_states in self._canonical_by_cutoff.items():
            if cash_state.account_id not in cached_states and _opening_cash_is_historically_valid(
                cash_state, cached_cutoff
            ):
                cached_states[cash_state.account_id] = cash_state

    def cash_states_at(self, cutoff_at: datetime) -> tuple[AccountCashState, ...]:
        return tuple(self._cash_states_at(cutoff_at).values())

    def _cash_states_at(self, cutoff_at: datetime) -> dict[str, AccountCashState]:
        cached_states = self._canonical_by_cutoff.get(cutoff_at)
        if cached_states is not None:
            return cached_states
        canonical_cash_states: dict[str, AccountCashState] = {}
        for cash_state in self._accepted_cash_states:
            if (
                cash_state.account_id not in canonical_cash_states
                and _opening_cash_is_historically_valid(cash_state, cutoff_at)
            ):
                canonical_cash_states[cash_state.account_id] = cash_state
        self._canonical_by_cutoff[cutoff_at] = canonical_cash_states
        return canonical_cash_states


def _opening_cash_is_historically_valid(
    cash_state: AccountCashState,
    cutoff_at: datetime,
) -> bool:
    """Ignore an opening baseline only when its own account fact is invalid."""
    return (
        cash_state.opening_ledger_cash is not None
        and not cash_state.opening_ledger_cash_evidence.problem_codes(
            cutoff_at,
            require_current_completeness=False,
        )
    )
