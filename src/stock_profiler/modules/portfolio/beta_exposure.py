"""Retain acquired coverage using an authoritative position ledger."""

from decimal import Decimal

from stock_profiler.modules.position_management.contracts import AuthoritativeLedgerEntry


def retained_acquired_quantity(
    entries: tuple[AuthoritativeLedgerEntry, ...],
    acquisitions: dict[str, Decimal],
    current_quantity: Decimal,
) -> Decimal:
    """Apply effective correction roots in time order, never banking sales credit.

    The caller supplies one account/security and validated complete ledger.
    Missing attribution or ambiguous lineage conservatively retains its holding.
    Sales consume existing acquired coverage; excess sales cannot affect later buys.
    """
    indexed = {entry.entry_id: entry for entry in entries}
    if len(indexed) != len(entries) or not set(acquisitions) <= set(indexed):
        return current_quantity
    roots = {}
    for entry in entries:
        root = entry
        visited: set[str] = set()
        while root.corrects_entry_id is not None:
            if root.entry_id in visited or root.corrects_entry_id not in indexed:
                return current_quantity
            visited.add(root.entry_id)
            root = indexed[root.corrects_entry_id]
        roots[entry.entry_id] = root.entry_id
    quantities: dict[str, Decimal] = {}
    attributed: dict[str, Decimal] = {}
    for entry in entries:
        root_id = roots[entry.entry_id]
        quantities[root_id] = quantities.get(root_id, Decimal(0)) + entry.quantity_delta
        attributed[root_id] = attributed.get(root_id, Decimal(0)) + acquisitions.get(
            entry.entry_id, Decimal(0)
        )
    acquired_roots = [indexed[key] for key, value in attributed.items() if value > 0]
    if not acquired_roots:
        return Decimal(0)
    first = min(entry.occurred_at for entry in acquired_roots)
    if any(
        entry.entry_type == "CORPORATE_ACTION" and entry.occurred_at >= first for entry in entries
    ):
        return current_quantity
    retained = Decimal(0)
    for root_id in sorted(
        quantities,
        key=lambda key: (indexed[key].occurred_at, quantities[key] >= 0, key),
    ):
        quantity = quantities[root_id]
        if indexed[root_id].entry_type != "FILL":
            continue
        if quantity < 0:
            retained = max(Decimal(0), retained + quantity)
        else:
            retained += min(quantity, max(Decimal(0), attributed[root_id]))
    return min(current_quantity, retained)
