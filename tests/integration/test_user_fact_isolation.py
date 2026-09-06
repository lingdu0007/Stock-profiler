from __future__ import annotations

from stock_profiler.adapters.persistence.result_delivery import ResultDelivery
from stock_profiler.bootstrap.decision_cases import run_default_frozen_decision_case
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.delivery.access import AccessPrincipal
from stock_profiler.modules.delivery.user_facts import UserFactRequest


def test_four_user_facts_remain_independent_and_do_not_change_the_saved_result(
    migrated_settings: Settings,
) -> None:
    execution = run_default_frozen_decision_case(migrated_settings)
    principal = AccessPrincipal(
        user_id="stock-profiler-single-user",
        account_ids=("synthetic-account-4017",),
        permissions=("REPORT_READ", "USER_FACT"),
    )
    delivery = ResultDelivery.from_settings(migrated_settings)
    requests = (
        UserFactRequest(kind="VIEWED", idempotency_key="synthetic-view"),
        UserFactRequest(kind="ACKNOWLEDGED", idempotency_key="synthetic-ack"),
        UserFactRequest(kind="CONFIRMED", idempotency_key="synthetic-confirm", choice="ACCEPT"),
        UserFactRequest(
            kind="EXECUTION_DECLARED",
            idempotency_key="synthetic-declare",
            declaration="REPORTED_FILLED",
        ),
    )
    facts = tuple(
        delivery.record_user_fact(execution.report_version_id, principal, request)
        for request in requests
    )
    assert all(fact is not None for fact in facts)
    assert len({fact.fact_id for fact in facts if fact is not None}) == 4
    assert tuple(fact.kind for fact in facts if fact is not None) == (
        "VIEWED",
        "ACKNOWLEDGED",
        "CONFIRMED",
        "EXECUTION_DECLARED",
    )
    assert facts[-1] is not None
    assert facts[-1].reconciliation_status == "PENDING"
    assert delivery.user_facts(execution.report_version_id, principal) == facts
    assert delivery.read_report(execution.report_version_id, principal) == execution.report
    assert (
        delivery.record_user_fact(execution.report_version_id, principal, requests[2]) == facts[2]
    )
