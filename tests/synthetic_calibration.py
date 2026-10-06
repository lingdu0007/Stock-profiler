"""Shared original synthetic temporal calibration records, generated deterministically."""

from calendar import monthrange
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from stock_profiler.modules.candidate_selection.calibrated_candidates import CalibrationRecord
from stock_profiler.modules.portfolio.market_calendar import six_month_terminal_evaluation_at


def _calibration_record(
    month: str,
    member: int,
    *,
    record_prefix: str,
    research_prefix: str,
    security_prefix: str,
    source_prefix: str,
    raw_score_frozen_at: datetime,
    entry_window_ends_at: datetime,
    terminal_success: bool,
) -> CalibrationRecord:
    return CalibrationRecord(
        record_id=f"{record_prefix}-label-{month}-{member:02d}",
        month=month,
        source_research_event_id=f"{source_prefix}-research-event-{month}",
        security_id=f"{security_prefix}-{member:02d}",
        research_id=f"{research_prefix}-{month}-{member:02d}",
        raw_score_model_version="synthetic-elastic-net-v1",
        raw_score_frozen_at=raw_score_frozen_at,
        raw_score_training_watermark_at=raw_score_frozen_at - timedelta(days=1),
        raw_success_score=Decimal(member) / Decimal("10"),
        out_of_sample_probability=(
            Decimal("0.1") + Decimal(member) / Decimal("10") * Decimal("0.8")
        ),
        terminal_success=terminal_success,
        entry_window_ends_at=entry_window_ends_at,
        entry_at=entry_window_ends_at - timedelta(days=1),
        label_available_at=six_month_terminal_evaluation_at(
            entry_window_ends_at - timedelta(days=1), "synthetic-calendar-v1"
        ),
        unified_maturity_at=six_month_terminal_evaluation_at(
            entry_window_ends_at - timedelta(days=1), "synthetic-calendar-v1"
        ),
        market_calendar_version="synthetic-calendar-v1",
    )


def training_records() -> tuple[CalibrationRecord, ...]:
    records = calibration_history_records()
    months = tuple(dict.fromkeys(record.month for record in records))[24:84]
    return tuple(record for record in records if record.month in months)


def calibration_history_records() -> tuple[CalibrationRecord, ...]:
    records: list[CalibrationRecord] = []
    months = tuple(f"{year}-{month:02d}" for year in range(2036, 2046) for month in range(1, 13))[
        8:119
    ]
    for month in months:
        year, month_number = (int(part) for part in month.split("-"))
        month_end = datetime(year, month_number, monthrange(year, month_number)[1], 8, tzinfo=UTC)
        raw_score_frozen_at = month_end - timedelta(hours=1)
        entry_window_ends_at = month_end + timedelta(days=5)
        for member in range(10):
            records.append(
                _calibration_record(
                    month,
                    member,
                    record_prefix="synthetic",
                    source_prefix="synthetic",
                    security_prefix="SYNTH-SECURITY",
                    research_prefix="synthetic-research",
                    raw_score_frozen_at=raw_score_frozen_at,
                    entry_window_ends_at=entry_window_ends_at,
                    terminal_success=member >= 5,
                )
            )
    return tuple(records)
