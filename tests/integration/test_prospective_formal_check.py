"""Formal node operations save skipped watermarks and preserve the original lineage."""

import pytest
from test_prospective_population_join import registered_cycle, registration_payload, sources
from test_prospective_shadow_cycle import review_case, run_case, saved

from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.prospective.contracts import CycleCommand


def test_delivery_only_changes_cannot_start_a_new_scientific_alpha_sequence(
    migrated_settings: Settings,
) -> None:
    settings = migrated_settings
    facts = sources(settings)
    registered_cycle(settings, facts, formal=True)
    payload = registration_payload(settings, facts, formal=True)
    payload.update(
        case_id="fictional-mechanical-reregister",
        business_identity="fictional-mechanical-reregister",
    )
    payload["version_bundle"]["host_source_sha"] = "f" * 40
    registration = payload["prospective"]["registration"]
    registration["source_version_bundle"] = dict(payload["version_bundle"])
    registration.update(
        version_id="fictional-reissued-cycle", capability_version="fictional-reissued-capability"
    )
    for name in ("selection_bundle", "research_bundle", "candidate_bundle", "standard_bundle"):
        registration["population_policy"][name]["host_source_sha"] = "f" * 40
    registration["formal_policy"]["cohort"]["source_version_bundle"]["host_source_sha"] = "f" * 40
    payload["prospective"] = CycleCommand.model_validate(payload["prospective"]).model_dump(
        mode="json"
    )
    payload["input"]["prospective"] = payload["prospective"]
    with pytest.raises(ValueError, match="PROSPECTIVE_SCIENTIFIC_SEQUENCE_ALREADY_REGISTERED"):
        run_case(settings.model_copy(update={"source_sha": "f" * 40}), payload)


def test_original_formal_node_saves_an_insufficient_look_without_spending_alpha(
    migrated_settings: Settings,
) -> None:
    facts = sources(migrated_settings)
    registered = registered_cycle(migrated_settings, facts, formal=True)
    payload = review_case(
        migrated_settings,
        registered.decision_event_id,
        cutoff="2042-07-07T15:00:00Z",
        identity="check-june",
    )
    payload["prospective"].update(operation="CHECK", formal_node_month="2042-06")
    checked = saved(migrated_settings, run_case(migrated_settings, payload))
    assert checked.result.prospective is not None
    look = checked.result.prospective.formal_look
    assert look is not None and look.disposition == "SKIPPED"
    assert look.actual_ordinal == 0 and look.alpha_spent == look.alpha_this == 0
    assert look.inference is None and "MATURE_BATCHES" in look.waiting_for
    assert not checked.result.prospective.authorization_granted
    assert saved(migrated_settings, run_case(migrated_settings, payload)) == checked
    retry = review_case(
        migrated_settings,
        registered.decision_event_id,
        checked.decision_event_id,
        cutoff="2042-07-07T15:00:00Z",
        identity="retry-june",
    )
    retry["prospective"].update(operation="CHECK", formal_node_month="2042-06")
    with pytest.raises(ValueError, match="PROSPECTIVE_FORMAL_NODE_ALREADY_CONSUMED"):
        run_case(migrated_settings, retry)


def test_a_check_requires_its_saved_original_formal_policy(migrated_settings: Settings) -> None:
    facts = sources(migrated_settings)
    registered = registered_cycle(migrated_settings, facts)
    payload = review_case(migrated_settings, registered.decision_event_id)
    payload["prospective"].update(operation="CHECK", formal_node_month="2042-06")
    with pytest.raises(ValueError, match="PROSPECTIVE_FORMAL_POLICY_REQUIRED"):
        run_case(migrated_settings, payload)
