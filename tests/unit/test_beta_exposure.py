"""Independent synthetic coverage conservation examples, including corrections."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from stock_profiler.modules.portfolio.beta_exposure import retained_acquired_quantity
from stock_profiler.modules.position_management.contracts import (
    AuthoritativeLedgerEntry,
    PositionEvidence,
)


def entry(
    identity: str, quantity: int, minute: int, corrects: str | None = None
) -> AuthoritativeLedgerEntry:
    observed = datetime(2042, 5, 17, 16, tzinfo=UTC)
    return AuthoritativeLedgerEntry(
        entry_id=identity,
        account_id="synthetic-account-4017",
        entry_type="FILL",
        security_id="SYNTHETIC-COVERAGE",
        quantity_delta=Decimal(quantity),
        cost_basis_delta=Decimal(quantity * 10),
        cash_delta=Decimal(-quantity * 10),
        occurred_at=datetime(2042, 5, 17, tzinfo=UTC) + timedelta(minutes=minute),
        corrects_entry_id=corrects,
        correction_reason="Independent synthetic correction" if corrects else None,
        evidence=PositionEvidence(
            source=f"synthetic-{identity}",
            source_version="synthetic-coverage-ledger/1",
            business_effective_at=observed,
            source_observed_at=observed,
            locally_acquired_at=observed,
            validated_at=observed,
            cutoff_at=observed,
            complete_through_at=observed,
        ),
    )


def test_reversed_sale_keeps_restored_acquired_coverage() -> None:
    entries = (entry("buy", 10, 1), entry("sell", -10, 2), entry("reverse", 10, 3, "sell"))
    assert retained_acquired_quantity(entries, {"buy": Decimal(10)}, Decimal(10)) == 10


def test_earlier_external_disposal_is_not_credit_against_later_acquisition() -> None:
    entries = (
        entry("external", 50, 0),
        entry("buy-one", 100, 1),
        entry("sell-all", -150, 2),
        entry("buy-two", 100, 3),
    )
    assert (
        retained_acquired_quantity(
            entries, {"buy-one": Decimal(100), "buy-two": Decimal(100)}, Decimal(100)
        )
        == 100
    )


def test_partial_disposal_releases_only_existing_acquired_quantity() -> None:
    entries = (entry("buy", 100, 1), entry("sell", -30, 2))
    assert retained_acquired_quantity(entries, {"buy": Decimal(100)}, Decimal(70)) == 70
