from __future__ import annotations

from typing import Any

import pytest

from stock_profiler.adapters.persistence.decision_ledger import (
    DecisionEventCommitError,
    DecisionLedger,
)
from stock_profiler.adapters.persistence.result_delivery import ResultDelivery
from stock_profiler.bootstrap.decision_cases import run_frozen_decision_case
from stock_profiler.bootstrap.settings import Settings


def test_direct_persistence_cannot_publish_shadow_content(
    migrated_settings: Settings, scoped_payload: dict[str, Any]
) -> None:
    scoped_payload["access_scope"]["visibility"] = "SHADOW"
    execution = run_frozen_decision_case(migrated_settings, scoped_payload)
    ledger = DecisionLedger.from_settings(migrated_settings)
    with (
        pytest.raises(DecisionEventCommitError, match="shadow"),
        ledger.serialize_case_execution() as connection,
    ):
        fact = ledger.get_decision_event(execution.decision_event_id, connection)
        assert fact is not None
        ledger.publish_report(connection, fact)
    assert ledger.get_formal_report(execution.report_version_id) is None
    audit = ResultDelivery.from_settings(migrated_settings).audit_history()
    assert len(audit) == 1
    assert audit[0].reason == "SHADOW_ISOLATED"
