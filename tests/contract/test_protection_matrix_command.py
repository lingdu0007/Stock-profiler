from __future__ import annotations

import json
import runpy
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_matrix_result_reader_rejects_incomplete_or_nonpassing_runs(tmp_path: Path) -> None:
    module: dict[str, Any] = runpy.run_path(str(ROOT / "scripts/protection_matrix.py"))
    read_result = module["read_result"]
    report = tmp_path / "results.xml"
    report.write_text(
        '<testsuites><testsuite tests="1"><testcase classname="tests.contract" '
        'name="test_saved_result"/></testsuite></testsuites>',
        encoding="utf-8",
    )
    assert read_result(report, 0) == {"tests.contract::test_saved_result": "passed"}
    for child in ("<skipped/>", "<error/>", "<failure/>"):
        report.write_text(
            '<testsuites><testsuite tests="1"><testcase classname="tests.contract" '
            f'name="test_saved_result">{child}</testcase></testsuite></testsuites>',
            encoding="utf-8",
        )
        with pytest.raises(ValueError):
            read_result(report, 0)
    report.write_text("<testsuites/>", encoding="utf-8")
    with pytest.raises(ValueError):
        read_result(report, 0)
    with pytest.raises(ValueError):
        read_result(report, 5)


def test_mutation_requires_the_named_contract_assertion_not_a_broken_test(tmp_path: Path) -> None:
    module: dict[str, Any] = runpy.run_path(str(ROOT / "scripts/protection_matrix.py"))
    read_result = module["read_result"]
    report = tmp_path / "mutation.xml"
    for child, code, accepted in (
        ('<failure message="assert leaked is None">AssertionError</failure>', 1, True),
        ('<failure message="DID NOT RAISE">Failed: DID NOT RAISE</failure>', 1, True),
        ('<failure message="ImportError">ImportError</failure>', 1, False),
        ('<error message="fixture unavailable"/>', 1, False),
        ("<skipped/>", 0, False),
        ("", 0, False),
    ):
        report.write_text(
            '<testsuites><testsuite tests="1"><testcase classname="tests.contract" '
            f'name="test_saved_result">{child}</testcase></testsuite></testsuites>',
            encoding="utf-8",
        )
        if accepted:
            assert read_result(report, code, expected_failure="test_saved_result") == {
                "tests.contract::test_saved_result": "assertion_failed"
            }
        else:
            with pytest.raises(ValueError):
                read_result(report, code, expected_failure="test_saved_result")
    report.write_text(
        '<testsuites><testsuite><testcase classname="tests.contract" name="test_unrelated">'
        '<failure message="assert False">AssertionError</failure>'
        "</testcase></testsuite></testsuites>",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        read_result(report, 1, expected_failure="test_saved_result")


def test_matrix_catalog_exposes_only_non_actionable_contract_evidence() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/protection_matrix.py"), "--catalog"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    catalog = json.loads(result.stdout)
    assert catalog["evidence_stage"] == "D0"
    assert catalog["purpose"] == "OFFLINE_CONTRACT"
    assert catalog["actionability"] == "NON_ACTIONABLE"
    assert catalog["qualification_granted"] is False
    assert catalog["activation_authorized"] is False
    assert set(catalog["groups"]) == {
        "account_scope",
        "position_facts",
        "concentration",
        "stress",
        "liquidity",
        "drawdown",
        "execution",
        "monitoring",
        "publication",
        "isolation",
        "governance",
        "journey",
    }
    assert all(catalog["groups"].values())
    assert catalog["required_repetitions"] == 2
    assert catalog["failure_tolerance"] == 0


def test_matrix_cannot_certify_a_different_source_identity(tmp_path: Path) -> None:
    output = tmp_path / "matrix.json"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/protection_matrix.py"),
            "--source",
            "0" * 40,
            "--output",
            str(output),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "Source identity mismatch" in result.stderr
    assert not output.exists()


def test_matrix_refuses_artifacts_inside_any_git_worktree(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/protection_matrix.py"),
            "--source",
            head,
            "--output",
            str(tmp_path / "nested" / "result.json"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "outside any Git worktree" in result.stderr
    assert not (tmp_path / "nested").exists()


def test_gate_mutation_requires_one_exact_guard_and_leaves_other_code_intact(
    tmp_path: Path,
) -> None:
    module: dict[str, Any] = runpy.run_path(str(ROOT / "scripts/protection_matrix.py"))
    mutate = module["mutate_guard"]
    target = tmp_path / "gate.py"
    original = (
        "def authorize(value):\n    if value is None:\n        return False\n    return True\n"
    )
    target.write_text(original, encoding="utf-8")
    mutate(target, "authorize", "value is None", "False")
    scope: dict[str, Any] = {}
    exec(compile(target.read_text(encoding="utf-8"), str(target), "exec"), scope)
    assert scope["authorize"](None) is True
    assert scope["authorize"]("synthetic") is True
    with pytest.raises(ValueError, match="exactly one"):
        mutate(target, "authorize", "value is None", "False")
    target.write_text(original + "\n" + original, encoding="utf-8")
    with pytest.raises(ValueError, match="exactly one"):
        mutate(target, "authorize", "value is None", "False")


def test_matrix_coverage_cannot_lose_a_required_surface() -> None:
    module: dict[str, Any] = runpy.run_path(str(ROOT / "scripts/protection_matrix.py"))
    verify_coverage = module["verify_coverage"]
    outcomes = {"tests.integration.test_report::test_saved_result": "passed"}
    assert verify_coverage(outcomes, {"publication": ["tests/integration/test_report.py"]}) == {
        "publication": 1
    }
    with pytest.raises(ValueError, match="coverage"):
        verify_coverage(
            outcomes,
            {
                "publication": ["tests/integration/test_report.py"],
                "isolation": ["tests/security"],
            },
        )


def test_mutation_can_remove_a_unique_guard_in_a_result_projection(tmp_path: Path) -> None:
    module: dict[str, Any] = runpy.run_path(str(ROOT / "scripts/protection_matrix.py"))
    target = tmp_path / "projection.py"
    target.write_text(
        'def project(sold, held, cap):\n    return {"reconfirm": sold == held and cap > 0}\n',
        encoding="utf-8",
    )
    module["mutate_guard"](target, "project", "sold == held and cap > 0", "False")
    scope: dict[str, Any] = {}
    exec(compile(target.read_text(encoding="utf-8"), str(target), "exec"), scope)
    assert scope["project"](100, 100, 30) == {"reconfirm": False}


def test_frozen_inventory_rejects_deleted_or_renamed_contracts() -> None:
    module: dict[str, Any] = runpy.run_path(str(ROOT / "scripts/protection_matrix.py"))
    with pytest.raises(ValueError, match="frozen inventory"):
        module["verify_inventory"]({"tests.integration.test_report::test_saved_result": "passed"})
