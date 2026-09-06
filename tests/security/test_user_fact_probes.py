from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from stock_profiler.adapters.persistence.result_delivery import (
    ACCESS_AUDIT,
    USER_FACTS,
    ResultDelivery,
)
from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage
from stock_profiler.bootstrap.decision_cases import run_frozen_decision_case
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.delivery.access import AccessPrincipal
from stock_profiler.modules.delivery.user_facts import UserFactRequest


def test_direct_adapter_revalidates_constructed_user_facts_before_writing(
    migrated_settings: Settings, scoped_payload: dict[str, Any]
) -> None:
    execution = run_frozen_decision_case(migrated_settings, scoped_payload)
    delivery = ResultDelivery.from_settings(migrated_settings)
    principal = AccessPrincipal(
        user_id="stock-profiler-single-user",
        account_ids=("synthetic-account-4017",),
        permissions=("REPORT_READ", "USER_FACT"),
    )
    forged = UserFactRequest.model_construct(
        kind="EXECUTION_DECLARED", idempotency_key="synthetic-forged", declaration="SUBMIT_ORDER"
    )
    assert delivery.record_user_fact(execution.report_version_id, principal, forged) is None
    assert delivery.user_facts(execution.report_version_id, principal) == ()
    assert delivery.audit_history()[-1].reason == "INVALID_USER_FACT"


def test_conflicting_retries_preserve_the_first_fact_and_append_only_a_denial(
    migrated_settings: Settings, scoped_payload: dict[str, Any]
) -> None:
    execution = run_frozen_decision_case(migrated_settings, scoped_payload)
    delivery = ResultDelivery.from_settings(migrated_settings)
    principal = AccessPrincipal(
        user_id="stock-profiler-single-user",
        account_ids=("synthetic-account-4017",),
        permissions=("REPORT_READ", "USER_FACT"),
    )
    original = delivery.record_user_fact(
        execution.report_version_id,
        principal,
        UserFactRequest(kind="CONFIRMED", choice="DEFER", idempotency_key="synthetic-choice"),
    )
    assert original is not None
    assert (
        delivery.record_user_fact(
            execution.report_version_id,
            principal,
            UserFactRequest(kind="CONFIRMED", choice="ACCEPT", idempotency_key="synthetic-choice"),
        )
        is None
    )
    assert delivery.user_facts(execution.report_version_id, principal) == (original,)
    runtime = initialize_runtime_storage(migrated_settings)
    for table in (USER_FACTS, ACCESS_AUDIT):
        for command in (table.update().values(sequence=999), table.delete()):
            with pytest.raises(IntegrityError), runtime.engine.begin() as connection:
                connection.execute(command)
        with runtime.engine.connect() as connection:
            assert len(connection.execute(select(table)).all()) == 1


@pytest.mark.parametrize("grant", ["wrong-user", "wrong-account", "missing-permission", "shadow"])
def test_denied_user_facts_never_append_a_declaration(
    migrated_settings: Settings, scoped_payload: dict[str, Any], grant: str
) -> None:
    if grant == "shadow":
        scoped_payload["access_scope"]["visibility"] = "SHADOW"
    execution = run_frozen_decision_case(migrated_settings, scoped_payload)
    principal = AccessPrincipal(
        user_id="wrong-user" if grant == "wrong-user" else "stock-profiler-single-user",
        account_ids=("wrong-account",) if grant == "wrong-account" else ("synthetic-account-4017",),
        permissions=("REPORT_READ",)
        if grant == "missing-permission"
        else ("REPORT_READ", "USER_FACT"),
    )
    delivery = ResultDelivery.from_settings(migrated_settings)
    assert (
        delivery.record_user_fact(
            execution.report_version_id,
            principal,
            UserFactRequest(
                kind="EXECUTION_DECLARED",
                declaration="REPORTED_FILLED",
                idempotency_key="synthetic-denied",
            ),
        )
        is None
    )
    assert delivery.audit_history()
    with initialize_runtime_storage(migrated_settings).engine.connect() as connection:
        assert connection.execute(select(USER_FACTS)).all() == []
