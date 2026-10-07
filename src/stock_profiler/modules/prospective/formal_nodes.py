"""Advance only original nodes; skipped watermarks never spend alpha."""

from decimal import Decimal

from stock_profiler.modules.prospective.contracts import (
    CycleFormalLook,
    CycleWatermark,
    FormalPolicy,
)


def prepare_formal_look(
    policy: FormalPolicy,
    previous: tuple[CycleFormalLook, ...],
    node_month: str,
    watermark: CycleWatermark,
    *,
    missed: bool = False,
) -> CycleFormalLook:
    if node_month not in policy.node_months:
        raise ValueError("PROSPECTIVE_FORMAL_NODE_UNAVAILABLE")
    consumed = tuple(look.node_month for look in previous)
    if node_month in consumed:
        raise ValueError("PROSPECTIVE_FORMAL_NODE_ALREADY_CONSUMED")
    if (
        consumed != policy.node_months[: len(consumed)]
        or node_month != policy.node_months[len(consumed)]
    ):
        raise ValueError("PROSPECTIVE_FORMAL_NODE_ORDER_CONFLICT")
    executed = tuple(
        look for look in previous if look.disposition in {"PASSED", "FAILED", "INDETERMINATE"}
    )
    last = executed[-1] if executed else None
    ordinal = last.actual_ordinal if last else 0
    required_batches = (
        last.mature_batches + policy.increment_batches
        if last
        else watermark.required.mature_batches
    )
    required_windows = (
        last.nonoverlapping_windows + policy.increment_windows
        if last
        else watermark.required.nonoverlapping_windows
    )
    waiting = tuple(
        name
        for name, actual, required in (
            ("MATURE_BATCHES", watermark.mature_batches, required_batches),
            ("NONOVERLAPPING_WINDOWS", watermark.nonoverlapping_windows, required_windows),
            (
                "HIGH_BAND_RECORDS",
                watermark.high_band_records,
                watermark.required.high_band_records,
            ),
        )
        if actual < required
    )
    if missed:
        waiting = (*waiting, "MISSED_ORIGINAL_NODE")
    ready = not waiting
    if ready:
        ordinal += 1
    return CycleFormalLook(
        disposition="READY" if ready else "SKIPPED",
        actual_ordinal=ordinal,
        alpha_this=policy.total_alpha / (ordinal * (ordinal + 1)) if ready else Decimal(0),
        alpha_spent=policy.total_alpha * ordinal / (ordinal + 1),
        node_month=node_month,
        mature_batches=watermark.mature_batches,
        nonoverlapping_windows=watermark.nonoverlapping_windows,
        waiting_for=waiting,
    )
