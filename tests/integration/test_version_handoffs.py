from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any

import pytest
from test_scoped_qualification import (
    GovernanceClock,
    case_payload,
    qualification_command,
    scope,
    version,
)

from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
from stock_profiler.bootstrap.decision_cases import get_formal_report, run_frozen_decision_case
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.decision_cases.domain import DecisionCaseExecution
from stock_profiler.modules.delivery.access import AccessPrincipal


def task_node(day: int) -> dict[str, Any]:
    return {
        "node_id": f"synthetic-daily-node-{day}",
        "task_identity": f"synthetic-daily-task-{day}",
        "kind": "DAILY",
        "scheduled_at": f"2042-05-{day:02d}T16:00:00Z",
        "knowledge_cutoff": f"2042-05-{day:02d}T16:00:00Z",
        "valid_until": f"2042-05-{day + 1:02d}T16:00:00Z",
        "deterministic_obligations": [
            {
                "obligation_id": "synthetic-established-protection-4017",
                "basis_reference": "synthetic-original-deterministic-evidence",
                "direction": "REDUCE",
                "quantity_status": "UNKNOWN",
            }
        ],
    }


def handoff_node(
    kind: str,
    identity: str,
    scheduled_at: str,
    *,
    retain_relations: bool = False,
) -> dict[str, Any]:
    scheduled = datetime.fromisoformat(scheduled_at)
    node = {
        "node_id": f"synthetic-{kind.lower()}-node-{identity}",
        "task_identity": f"synthetic-{kind.lower()}-task-{identity}",
        "kind": kind,
        "scheduled_at": scheduled_at,
        "knowledge_cutoff": scheduled_at,
        "valid_until": (scheduled + timedelta(days=1)).isoformat(),
        "deterministic_obligations": [
            {
                "obligation_id": f"synthetic-{kind.lower()}-unfinished-{identity}",
                "basis_reference": f"synthetic-{kind.lower()}-basis-{identity}",
                "direction": "REDUCE",
                "quantity_status": "UNKNOWN",
            }
        ],
    }
    if retain_relations:
        node["retained_objects"] = [
            {
                "kind": object_kind,
                "object_id": f"synthetic-{kind.lower()}-{object_kind.lower()}-{identity}",
            }
            for object_kind in ("CONCLUSION", "CONTRACT", "EVIDENCE", "EVALUATION")
        ]
    return node


def execute_handoff(
    settings: Settings,
    identity: str,
    command: dict[str, Any],
    *,
    now: str = "2042-05-17T16:01:00Z",
) -> DecisionCaseExecution:
    payload = case_payload(settings, identity, command, contract_version="5.0.0")
    payload["knowledge_cutoff"] = (datetime.fromisoformat(now) - timedelta(minutes=1)).isoformat()
    return run_frozen_decision_case(settings, payload, clock=GovernanceClock(now))


@pytest.mark.parametrize(
    ("kind", "old_at", "earliest_at", "later_at"),
    [
        (
            "DAILY",
            "2042-05-18T16:00:00Z",
            "2042-05-19T16:00:00Z",
            "2042-05-20T16:00:00Z",
        ),
        (
            "MONTHLY",
            "2042-06-30T16:00:00Z",
            "2042-07-31T16:00:00Z",
            "2042-08-31T16:00:00Z",
        ),
        (
            "RENEWAL",
            "2042-06-15T16:00:00Z",
            "2042-07-15T16:00:00Z",
            "2042-08-15T16:00:00Z",
        ),
    ],
)
def test_handoff_cannot_bypass_earliest_eligible_node(
    migrated_settings: Settings,
    kind: str,
    old_at: str,
    earliest_at: str,
    later_at: str,
) -> None:
    first_version = version(migrated_settings)
    second_version = version(migrated_settings, f"synthetic-{kind.lower()}-version-two")
    grants = []
    for index, bundle in enumerate((first_version, second_version)):
        command = qualification_command(migrated_settings)
        command["version"] = bundle
        command["evidence"]["version"] = bundle
        command["evidence"]["evidence_id"] = f"synthetic-{kind.lower()}-grant-{index}"
        grants.append(execute_handoff(migrated_settings, f"{kind}-grant-{index}", command))

    old_node = handoff_node(kind, "old", old_at)
    earliest_node = handoff_node(kind, "earliest", earliest_at)
    later_node = handoff_node(kind, "later", later_at)
    for identity, node in (
        ("old", old_node),
        ("earliest", earliest_node),
        ("later", later_node),
    ):
        registration = execute_handoff(
            migrated_settings,
            f"{kind}-register-{identity}",
            {"operation": "REGISTER_TASK_NODE", "scope": scope(), "node": node},
        )
        assert registration.report is not None
        assert registration.report.result.governance is not None
        assert registration.report.result.governance.disposition == "APPROVED"

    first_activation = execute_handoff(
        migrated_settings,
        f"{kind}-activate-first",
        {
            "operation": "ACTIVATE_VERSION",
            "scope": scope(),
            "version": first_version,
            "previous_version": None,
            "previous_activation_id": None,
            "qualification_decision_id": grants[0].decision_event_id,
            "first_node_id": old_node["node_id"],
        },
    )
    execute_handoff(
        migrated_settings,
        f"{kind}-freeze-old",
        {
            "operation": "FREEZE_TASK",
            "scope": scope(),
            "version": first_version,
            "node_id": old_node["node_id"],
            "activation_id": first_activation.decision_event_id,
        },
        now=(datetime.fromisoformat(old_at) + timedelta(minutes=1)).isoformat(),
    )
    handoff_now = (datetime.fromisoformat(old_at) + timedelta(minutes=2)).isoformat()
    later = execute_handoff(
        migrated_settings,
        f"{kind}-activate-later",
        {
            "operation": "ACTIVATE_VERSION",
            "scope": scope(),
            "version": second_version,
            "previous_version": first_version,
            "previous_activation_id": first_activation.decision_event_id,
            "qualification_decision_id": grants[1].decision_event_id,
            "first_node_id": later_node["node_id"],
        },
        now=handoff_now,
    )
    assert later.report is not None
    assert later.report.result.governance is not None
    assert later.report.result.governance.disposition == "DENIED"
    assert later.report.result.governance.reasons == ("NEXT_LEGAL_TASK_NODE_REQUIRED",)

    earliest = execute_handoff(
        migrated_settings,
        f"{kind}-activate-earliest",
        {
            "operation": "ACTIVATE_VERSION",
            "scope": scope(),
            "version": second_version,
            "previous_version": first_version,
            "previous_activation_id": first_activation.decision_event_id,
            "qualification_decision_id": grants[1].decision_event_id,
            "first_node_id": earliest_node["node_id"],
        },
        now=handoff_now,
    )
    assert earliest.report is not None
    assert earliest.report.result.governance is not None
    assert earliest.report.result.governance.disposition == "APPROVED"
    assert earliest.report.result.governance.activation is not None
    assert (
        earliest.report.result.governance.activation.first_node.node_id == earliest_node["node_id"]
    )


@pytest.mark.parametrize(
    ("kind", "old_at", "next_at"),
    [
        ("DAILY", "2042-05-18T16:00:00Z", "2042-05-19T16:00:00Z"),
        ("MONTHLY", "2042-06-30T16:00:00Z", "2042-07-31T16:00:00Z"),
        ("RENEWAL", "2042-06-15T16:00:00Z", "2042-07-15T16:00:00Z"),
    ],
)
def test_handoff_retains_reconstructable_frozen_task_relations(
    migrated_settings: Settings,
    kind: str,
    old_at: str,
    next_at: str,
) -> None:
    first_version = version(migrated_settings)
    second_version = version(migrated_settings, f"synthetic-{kind.lower()}-retained-version")
    grants = []
    for index, bundle in enumerate((first_version, second_version)):
        command = qualification_command(migrated_settings)
        command["version"] = bundle
        command["evidence"]["version"] = bundle
        command["evidence"]["evidence_id"] = f"synthetic-{kind.lower()}-retained-grant-{index}"
        grants.append(execute_handoff(migrated_settings, f"{kind}-retained-grant-{index}", command))

    old_node = handoff_node(kind, "retained-old", old_at, retain_relations=True)
    next_node = handoff_node(kind, "retained-next", next_at)
    for identity, node in (("old", old_node), ("next", next_node)):
        execute_handoff(
            migrated_settings,
            f"{kind}-retained-register-{identity}",
            {"operation": "REGISTER_TASK_NODE", "scope": scope(), "node": node},
        )

    first_activation = execute_handoff(
        migrated_settings,
        f"{kind}-retained-activate-first",
        {
            "operation": "ACTIVATE_VERSION",
            "scope": scope(),
            "version": first_version,
            "previous_version": None,
            "previous_activation_id": None,
            "qualification_decision_id": grants[0].decision_event_id,
            "first_node_id": old_node["node_id"],
        },
    )
    old_task = execute_handoff(
        migrated_settings,
        f"{kind}-retained-freeze-old",
        {
            "operation": "FREEZE_TASK",
            "scope": scope(),
            "version": first_version,
            "node_id": old_node["node_id"],
            "activation_id": first_activation.decision_event_id,
        },
        now=(datetime.fromisoformat(old_at) + timedelta(minutes=1)).isoformat(),
    )
    assert old_task.report is not None
    original_report = old_task.report.model_dump_json()

    second_activation = execute_handoff(
        migrated_settings,
        f"{kind}-retained-activate-second",
        {
            "operation": "ACTIVATE_VERSION",
            "scope": scope(),
            "version": second_version,
            "previous_version": first_version,
            "previous_activation_id": first_activation.decision_event_id,
            "qualification_decision_id": grants[1].decision_event_id,
            "first_node_id": next_node["node_id"],
        },
        now=(datetime.fromisoformat(old_at) + timedelta(minutes=2)).isoformat(),
    )
    assert second_activation.report is not None
    assert second_activation.report.result.governance is not None
    activation = second_activation.report.result.governance.activation
    assert activation is not None
    assert activation.retained_task_ids == (old_node["task_identity"],)
    assert len(activation.retained_tasks) == 1
    retained = activation.retained_tasks[0]
    assert retained.task_identity == old_node["task_identity"]
    assert retained.task_decision_id == old_task.decision_event_id
    assert retained.original_version.model_dump(mode="json") == first_version
    assert retained.node_id == old_node["node_id"]
    assert retained.activation_id == first_activation.decision_event_id
    assert retained.qualification_decision_id == grants[0].decision_event_id
    assert retained.knowledge_cutoff.isoformat() == old_node["knowledge_cutoff"].replace(
        "Z", "+00:00"
    )
    assert retained.scheduled_at.isoformat() == old_node["scheduled_at"].replace("Z", "+00:00")
    assert retained.valid_until.isoformat() == old_node["valid_until"]
    assert [item.model_dump(mode="json") for item in retained.retained_objects] == old_node[
        "retained_objects"
    ]
    assert [
        item.model_dump(mode="json") for item in retained.deterministic_obligations
    ] == old_node["deterministic_obligations"]
    assert old_task.report.model_dump_json() == original_report


def test_account_order_preserves_existing_task_and_activation_identity(
    migrated_settings: Settings,
) -> None:
    accounts = [*scope()["account_ids"], "synthetic-account-orbit"]

    def execute(
        identity: str, command: dict[str, Any], *, reverse: bool = False, day: int = 17
    ) -> DecisionCaseExecution:
        command["scope"]["account_ids"] = list(reversed(accounts)) if reverse else accounts
        if command["operation"] == "QUALIFICATION":
            command["evidence"]["scope"] = deepcopy(command["scope"])
        payload = case_payload(migrated_settings, identity, command, contract_version="5.0.0")
        payload["access_scope"]["account_ids"] = accounts
        payload["knowledge_cutoff"] = f"2042-05-{day:02d}T16:00:00Z"
        return run_frozen_decision_case(
            migrated_settings, payload, clock=GovernanceClock(f"2042-05-{day:02d}T16:01:00Z")
        )

    grant = execute("ordered-task-grant", qualification_command(migrated_settings))
    execute(
        "ordered-task-node",
        {"operation": "REGISTER_TASK_NODE", "scope": scope(), "node": task_node(18)},
    )
    activation_command = {
        "operation": "ACTIVATE_VERSION",
        "scope": scope(),
        "version": version(migrated_settings),
        "previous_version": None,
        "previous_activation_id": None,
        "qualification_decision_id": grant.decision_event_id,
        "first_node_id": task_node(18)["node_id"],
    }
    active = execute("ordered-task-activation", activation_command, reverse=True)
    assert active.report is not None and active.report.result.governance is not None
    assert active.report.result.governance.disposition == "APPROVED"
    repeated = execute("ordered-task-activation-reset", deepcopy(activation_command))
    assert repeated.report is not None and repeated.report.result.governance is not None
    assert repeated.report.result.governance.disposition == "DENIED"
    freeze_command = {
        "operation": "FREEZE_TASK",
        "scope": scope(),
        "version": version(migrated_settings),
        "node_id": task_node(18)["node_id"],
        "activation_id": active.decision_event_id,
    }
    frozen = execute("ordered-task-freeze", freeze_command, day=18)
    assert frozen.report is not None and frozen.report.result.governance is not None
    task = frozen.report.result.governance.task
    assert task is not None and task.freeze_status == "APPROVED"
    repeated = execute("ordered-task-refreeze", deepcopy(freeze_command), reverse=True, day=18)
    assert repeated.report is not None and repeated.report.result.governance is not None
    assert repeated.report.result.governance.reasons == ("TASK_FREEZE_NOT_ALLOWED",)
    used = execute(
        "ordered-task-use",
        {
            "operation": "USE_TASK",
            "scope": scope(),
            "version": version(migrated_settings),
            "task_identity": task_node(18)["task_identity"],
        },
        reverse=True,
        day=18,
    )
    assert used.report is not None and used.report.result.governance is not None
    usage = used.report.result.governance.usage
    assert usage is not None and usage.allowed
    assert usage.task_snapshot == task
    assert task.scope.account_ids == tuple(accounts)


def test_qualification_does_not_activate_or_reopen_a_frozen_task(
    migrated_settings: Settings,
) -> None:
    grant = run_frozen_decision_case(
        migrated_settings,
        case_payload(
            migrated_settings,
            "handoff-grant",
            qualification_command(migrated_settings),
            contract_version="5.0.0",
        ),
        clock=GovernanceClock(),
    )
    for day in (18, 19):
        registration = run_frozen_decision_case(
            migrated_settings,
            case_payload(
                migrated_settings,
                f"node-registration-{day}",
                {"operation": "REGISTER_TASK_NODE", "scope": scope(), "node": task_node(day)},
                contract_version="5.0.0",
            ),
            clock=GovernanceClock(),
        )
        assert registration.report is not None
        assert registration.report.result.model_dump(mode="json")["governance"]["disposition"] == (
            "APPROVED"
        )
    denied_payload = case_payload(
        migrated_settings,
        "unactivated-frozen-task",
        {
            "operation": "FREEZE_TASK",
            "scope": scope(),
            "version": version(migrated_settings),
            "node_id": task_node(18)["node_id"],
            "activation_id": None,
        },
        contract_version="5.0.0",
    )
    denied_payload["knowledge_cutoff"] = task_node(18)["knowledge_cutoff"]
    denied = run_frozen_decision_case(
        migrated_settings, denied_payload, clock=GovernanceClock("2042-05-18T16:01:00Z")
    )
    assert denied.report is not None
    denial = denied.report.result.model_dump(mode="json")["governance"]
    assert denial["disposition"] == "DENIED"
    assert denial["reasons"] == ["EXPLICIT_VERSION_ACTIVATION_REQUIRED"]
    assert denial["task"]["freeze_status"] == "DENIED"
    assert denial["task"]["qualification_snapshot"]["decision_id"] == grant.decision_event_id

    activation_payload = case_payload(
        migrated_settings,
        "explicit-handoff",
        {
            "operation": "ACTIVATE_VERSION",
            "scope": scope(),
            "version": version(migrated_settings),
            "previous_version": None,
            "previous_activation_id": None,
            "qualification_decision_id": grant.decision_event_id,
            "first_node_id": task_node(19)["node_id"],
        },
        contract_version="5.0.0",
    )
    activation_payload["knowledge_cutoff"] = task_node(18)["knowledge_cutoff"]
    activated = run_frozen_decision_case(
        migrated_settings, activation_payload, clock=GovernanceClock("2042-05-18T16:02:00Z")
    )
    assert activated.report is not None
    activation = activated.report.result.model_dump(mode="json")["governance"]["activation"]
    assert activation["version"] == version(migrated_settings)
    assert activation["first_node"]["node_id"] == task_node(19)["node_id"]
    assert activation["retained_task_ids"] == [task_node(18)["task_identity"]]

    next_payload = case_payload(
        migrated_settings,
        "next-legitimate-frozen-task",
        {
            "operation": "FREEZE_TASK",
            "scope": scope(),
            "version": version(migrated_settings),
            "node_id": task_node(19)["node_id"],
            "activation_id": activated.decision_event_id,
        },
        contract_version="5.0.0",
    )
    next_payload["knowledge_cutoff"] = task_node(19)["knowledge_cutoff"]
    frozen = run_frozen_decision_case(
        migrated_settings, next_payload, clock=GovernanceClock("2042-05-19T16:01:00Z")
    )
    assert frozen.report is not None
    result = frozen.report.result.model_dump(mode="json")["governance"]
    assert result["disposition"] == "APPROVED"
    assert result["task"]["version"] == version(migrated_settings)
    assert result["task"]["activation_id"] == activated.decision_event_id
    assert (
        run_frozen_decision_case(
            migrated_settings, denied_payload, clock=GovernanceClock("2042-05-19T16:02:00Z")
        ).report
        == denied.report
    )


def test_handoff_preserves_old_task_binding_and_revocation_blocks_only_new_affected_use(
    migrated_settings: Settings,
) -> None:
    def execute(
        identity: str, command: dict[str, Any], now: str = "2042-05-17T16:01:00Z"
    ) -> DecisionCaseExecution:
        payload = case_payload(migrated_settings, identity, command, contract_version="5.0.0")
        payload["knowledge_cutoff"] = (
            datetime.fromisoformat(now) - timedelta(minutes=1)
        ).isoformat()
        return run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock(now))

    first_version = version(migrated_settings)
    second_version = version(migrated_settings, "synthetic-version-two")
    grants = []
    for number, bundle in enumerate((first_version, second_version)):
        command = qualification_command(migrated_settings)
        command["version"] = bundle
        command["evidence"]["version"] = bundle
        command["evidence"]["evidence_id"] = f"synthetic-version-proof-{number}"
        grants.append(execute(f"version-grant-{number}", command))
    for day in (18, 19):
        execute(
            f"version-node-{day}",
            {"operation": "REGISTER_TASK_NODE", "scope": scope(), "node": task_node(day)},
        )
    activated_first = execute(
        "first-version-active",
        {
            "operation": "ACTIVATE_VERSION",
            "scope": scope(),
            "version": first_version,
            "previous_version": None,
            "previous_activation_id": None,
            "qualification_decision_id": grants[0].decision_event_id,
            "first_node_id": task_node(18)["node_id"],
        },
    )
    old_task = execute(
        "old-task-bound",
        {
            "operation": "FREEZE_TASK",
            "scope": scope(),
            "version": first_version,
            "node_id": task_node(18)["node_id"],
            "activation_id": activated_first.decision_event_id,
        },
        "2042-05-18T16:01:00Z",
    )
    assert old_task.report is not None
    original_snapshot = old_task.report.model_dump_json()
    activated_second = execute(
        "second-version-active",
        {
            "operation": "ACTIVATE_VERSION",
            "scope": scope(),
            "version": second_version,
            "previous_version": first_version,
            "previous_activation_id": activated_first.decision_event_id,
            "qualification_decision_id": grants[1].decision_event_id,
            "first_node_id": task_node(19)["node_id"],
        },
        "2042-05-18T16:02:00Z",
    )
    before = execute(
        "old-use-before-revocation",
        {
            "operation": "USE_TASK",
            "scope": scope(),
            "version": first_version,
            "task_identity": task_node(18)["task_identity"],
        },
        "2042-05-18T16:03:00Z",
    )
    assert before.report is not None
    assert before.report.result.model_dump(mode="json")["governance"]["usage"]["allowed"] is True
    revoke = qualification_command(
        migrated_settings, action="REVOKE", previous=grants[0].decision_event_id
    )
    revoke["evidence"].update(kind="ORIGINAL_BASIS_INVALID", evidence_id="synthetic-old-revocation")
    execute("old-version-revoked", revoke, "2042-05-18T16:04:00Z")
    after = execute(
        "old-use-after-revocation",
        {
            "operation": "USE_TASK",
            "scope": scope(),
            "version": first_version,
            "task_identity": task_node(18)["task_identity"],
        },
        "2042-05-18T16:05:00Z",
    )
    assert after.report is not None
    denied = after.report.result.model_dump(mode="json")["governance"]
    assert denied["disposition"] == "DENIED"
    assert denied["usage"]["allowed"] is False
    assert denied["usage"]["task_snapshot"]["version"] == first_version
    assert denied["usage"]["qualification_snapshot"]["status"] == "REVOKED"
    assert (
        denied["usage"]["task_snapshot"]["node"]["deterministic_obligations"]
        == (task_node(18)["deterministic_obligations"])
    )
    assert old_task.report.model_dump_json() == original_snapshot
    new_task = execute(
        "new-task-bound",
        {
            "operation": "FREEZE_TASK",
            "scope": scope(),
            "version": second_version,
            "node_id": task_node(19)["node_id"],
            "activation_id": activated_second.decision_event_id,
        },
        "2042-05-19T16:01:00Z",
    )
    assert new_task.report is not None
    new_snapshot = new_task.report.result.model_dump(mode="json")["governance"]["task"]
    assert new_snapshot["version"] == second_version
    assert new_snapshot["qualification_snapshot"]["decision_id"] == grants[1].decision_event_id
    new_use = execute(
        "new-version-use",
        {
            "operation": "USE_TASK",
            "scope": scope(),
            "version": second_version,
            "task_identity": task_node(19)["task_identity"],
        },
        "2042-05-19T16:02:00Z",
    )
    assert new_use.report is not None
    assert new_use.report.result.model_dump(mode="json")["governance"]["usage"]["allowed"] is True
    assert (
        get_formal_report(
            old_task.report_version_id,
            migrated_settings,
            principal=AccessPrincipal(
                user_id=scope()["user_id"],
                account_ids=tuple(scope()["account_ids"]),
                permissions=("REPORT_READ",),
            ),
        )
        == old_task.report
    )


def prepare_task(settings: Settings, visibility: str = "USER") -> DecisionCaseExecution:
    def scoped_payload(
        supplied: Settings, identity: str, command: dict[str, Any], *, contract_version: str
    ) -> dict[str, Any]:
        payload = case_payload(supplied, identity, command, contract_version=contract_version)
        payload["access_scope"]["visibility"] = visibility
        return payload

    grant = run_frozen_decision_case(
        settings,
        scoped_payload(
            settings, "matrix-grant", qualification_command(settings), contract_version="5.0.0"
        ),
        clock=GovernanceClock(),
    )
    run_frozen_decision_case(
        settings,
        scoped_payload(
            settings,
            "matrix-node",
            {"operation": "REGISTER_TASK_NODE", "scope": scope(), "node": task_node(18)},
            contract_version="5.0.0",
        ),
        clock=GovernanceClock(),
    )
    activation = run_frozen_decision_case(
        settings,
        scoped_payload(
            settings,
            "matrix-activation",
            {
                "operation": "ACTIVATE_VERSION",
                "scope": scope(),
                "version": version(settings),
                "previous_version": None,
                "previous_activation_id": None,
                "qualification_decision_id": grant.decision_event_id,
                "first_node_id": task_node(18)["node_id"],
            },
            contract_version="5.0.0",
        ),
        clock=GovernanceClock(),
    )
    payload = scoped_payload(
        settings,
        "matrix-frozen-task",
        {
            "operation": "FREEZE_TASK",
            "scope": scope(),
            "version": version(settings),
            "node_id": task_node(18)["node_id"],
            "activation_id": activation.decision_event_id,
        },
        contract_version="5.0.0",
    )
    payload["knowledge_cutoff"] = task_node(18)["knowledge_cutoff"]
    execution = run_frozen_decision_case(
        settings, payload, clock=GovernanceClock("2042-05-18T16:01:00Z")
    )
    with DecisionLedger.from_settings(settings).serialize_case_execution() as connection:
        fact = DecisionLedger.from_settings(settings).get_original_decision_event(
            execution.business_object_id, connection
        )
        assert fact is not None and fact.result.governance is not None
        assert fact.result.governance.disposition == "APPROVED"
    return execution


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("capability", "synthetic-other-capability"),
        ("purpose", "synthetic-evaluation-only"),
        ("source", "fictional-ledger-gamma"),
        ("market_state", "synthetic-variable"),
        ("board", "synthetic-other-board"),
        ("target", "synthetic-other-target"),
        ("account_type", "SIMULATED_MARGIN"),
        ("user_id", "synthetic-other-user"),
        ("account_ids", ["synthetic-account-4017", "synthetic-account-8029"]),
        ("holding_age_domain", "synthetic-later-age"),
        ("renewal_ordinal", "synthetic-next-renewal"),
        ("probability_grid", "synthetic-other-grid"),
        ("portfolio_scope", "synthetic-other-portfolio"),
    ],
)
def test_scope_qualification_and_task_authority_never_expand_implicitly(
    migrated_settings: Settings, field: str, value: Any
) -> None:
    original = prepare_task(migrated_settings)
    requested_scope = {**scope(), field: value}
    payload = case_payload(
        migrated_settings,
        "matrix-foreign-scope",
        {
            "operation": "USE_TASK",
            "scope": requested_scope,
            "version": version(migrated_settings),
            "task_identity": task_node(18)["task_identity"],
        },
        contract_version="5.0.0",
    )
    payload["knowledge_cutoff"] = task_node(18)["knowledge_cutoff"]
    payload["access_scope"].update(
        user_id=requested_scope["user_id"], account_ids=requested_scope["account_ids"]
    )
    execution = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-18T16:02:00Z")
    )
    assert execution.report is not None
    result = execution.report.result.model_dump(mode="json")["governance"]
    assert result["disposition"] == "DENIED"
    assert result["qualification"] is None
    assert result.get("usage") is None
    assert result["reasons"] == ["FROZEN_TASK_REQUIRED"]
    assert (
        get_formal_report(
            original.report_version_id,
            migrated_settings,
            principal=AccessPrincipal(
                user_id=scope()["user_id"],
                account_ids=tuple(scope()["account_ids"]),
                permissions=("REPORT_READ",),
            ),
        )
        == original.report
    )


@pytest.mark.parametrize(
    "field",
    [
        "version_id",
        "policy_version",
        "missing_policy",
        "policy.seed",
        "policy.generator_version",
        "policy.policy_version",
        "policy.evaluation_max_age_months",
        "policy.state_activity_max_age_months",
        "policy.require_state_activity",
        "case_contract_version",
        "host_contract_version",
        "host_application_version",
        "host_source_sha",
        "agent_definition_id",
        "agent_definition_version",
        "model_adapter_id",
        "routing_policy_version",
        "output_contract_version",
        "report_projection_contract_version",
        "m_agent_version",
        "m_agent_wheel_url",
        "m_agent_wheel_sha256",
        "m_agent_release_commit",
    ],
)
def test_every_frozen_capability_version_component_is_required_for_statistical_use(
    migrated_settings: Settings, field: str
) -> None:
    original = prepare_task(migrated_settings)
    requested_version = version(migrated_settings)
    if field in {"version_id", "policy_version"}:
        requested_version[field] = "synthetic-different-version"
    elif field == "missing_policy":
        requested_version.pop("qualification_policy")
    elif field.startswith("policy."):
        key = field.removeprefix("policy.")
        requested_version["qualification_policy"][key] = {
            "seed": 82003,
            "generator_version": "independent-policy-demonstration/2",
            "policy_version": "synthetic-other-policy",
            "evaluation_max_age_months": 7,
            "state_activity_max_age_months": 11,
            "require_state_activity": False,
        }[key]
    else:
        requested_version["implementation"][field] = "synthetic-different-component"
    payload = case_payload(
        migrated_settings,
        "matrix-mixed-version",
        {
            "operation": "USE_TASK",
            "scope": scope(),
            "version": requested_version,
            "task_identity": task_node(18)["task_identity"],
        },
        contract_version="5.0.0",
    )
    payload["knowledge_cutoff"] = task_node(18)["knowledge_cutoff"]
    execution = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-18T16:02:00Z")
    )
    assert execution.report is not None
    result = execution.report.result.model_dump(mode="json")["governance"]
    assert result["disposition"] == "DENIED"
    assert result["usage"]["allowed"] is False
    assert result["reasons"] == ["ORIGINAL_TASK_VERSION_REQUIRED"]
    assert result["usage"]["task_snapshot"]["version"] == version(migrated_settings)
    assert result["usage"]["task_snapshot"]["decision_id"] == original.decision_event_id


@pytest.mark.parametrize(("origin", "target"), [("SHADOW", "USER"), ("USER", "SHADOW")])
def test_task_and_handoff_history_do_not_cross_visibility(
    migrated_settings: Settings, origin: str, target: str
) -> None:
    original = prepare_task(migrated_settings, origin)
    payload = case_payload(
        migrated_settings,
        "opposite-visibility-task-use",
        {
            "operation": "USE_TASK",
            "scope": scope(),
            "version": version(migrated_settings),
            "task_identity": task_node(18)["task_identity"],
        },
        contract_version="5.0.0",
    )
    payload["knowledge_cutoff"] = task_node(18)["knowledge_cutoff"]
    payload["access_scope"]["visibility"] = target
    execution = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-18T16:02:00Z")
    )
    ledger = DecisionLedger.from_settings(migrated_settings)
    with ledger.serialize_case_execution() as connection:
        fact = ledger.get_original_decision_event(execution.business_object_id, connection)
        assert fact is not None and fact.result.governance is not None
        result = fact.result.governance.model_dump(mode="json")
    assert result["disposition"] == "DENIED"
    assert result.get("usage") is None
    assert result["qualification"] is None
    assert result.get("task") is None
    assert result.get("activation") is None
    if origin == "SHADOW":
        assert (
            get_formal_report(
                original.report_version_id,
                migrated_settings,
                principal=AccessPrincipal(
                    user_id=scope()["user_id"],
                    account_ids=tuple(scope()["account_ids"]),
                    permissions=("REPORT_READ",),
                ),
            )
            is None
        )
