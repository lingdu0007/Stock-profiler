"""Run the frozen, non-actionable deterministic protection contract matrix."""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from importlib.metadata import version
from pathlib import Path
from typing import NamedTuple
from xml.etree import ElementTree

GROUPS = {
    "account_scope": ["tests/integration/test_portfolio_authorization.py"],
    "position_facts": ["tests/integration/test_position_state_reconciliation.py"],
    "concentration": ["tests/integration/test_issuer_concentration.py"],
    "stress": ["tests/integration/test_portfolio_stress.py"],
    "liquidity": ["tests/integration/test_liquidity_protection.py"],
    "drawdown": ["tests/integration/test_drawdown_protection.py"],
    "execution": ["tests/integration/test_execution_plans.py"],
    "monitoring": ["tests/integration/test_monitoring_workspace.py"],
    "publication": [
        "tests/integration/test_replayable_decision_case.py",
        "tests/acceptance/test_decision_case_cli.py",
    ],
    "isolation": [
        "tests/security",
        "tests/integration/test_authenticated_report_delivery.py",
        "tests/integration/test_result_access_isolation.py",
    ],
    "governance": [
        "tests/integration/test_scoped_qualification.py",
        "tests/integration/test_governance_proof_inputs.py",
    ],
    "journey": ["tests/integration/test_protection_contract_journey.py"],
}


class GuardMutation(NamedTuple):
    path: str
    function: str
    condition: str
    replacement: str
    test: str


MUTATIONS = {
    "concentration_hard_gate": GuardMutation(
        "src/stock_profiler/modules/position_management/concentration.py",
        "_assess",
        "value <= equity * budget.concentration.hard_ratio and not pending",
        "True",
        "tests/integration/test_issuer_concentration.py"
        "::test_buffer_and_hard_boundaries_have_distinct_actions",
    ),
    "stress_hard_gate": GuardMutation(
        "src/stock_profiler/modules/portfolio/stress.py",
        "assess_stress",
        "loss > budget.hard_ratio * equity",
        "False",
        "tests/integration/test_portfolio_stress.py"
        "::test_thresholds_preserve_stock_conclusions_and_create_only_a_portfolio_obligation",
    ),
    "cash_restoration_gate": GuardMutation(
        "src/stock_profiler/modules/portfolio/liquidity.py",
        "assess_liquidity",
        "qualified < floor or prior_remediation_id is not None",
        "False",
        "tests/integration/test_liquidity_protection.py::test_liquidity_cash_threshold_boundaries",
    ),
    "capital_preservation_gate": GuardMutation(
        "src/stock_profiler/modules/portfolio/drawdown.py",
        "adjudicate",
        "interval_drawdown >= budget.drawdown.preservation_ratio",
        "False",
        "tests/integration/test_drawdown_protection.py"
        "::test_single_observation_escalates_at_exact_boundaries_without_assuming_execution",
    ),
    "user_scope": GuardMutation(
        "src/stock_profiler/modules/delivery/access.py",
        "read_denial",
        "principal.user_id != SINGLE_USER_ID or principal.user_id != owner_id",
        "False",
        "tests/integration/test_result_access_isolation.py"
        "::test_scope_denials_are_durable_without_disclosing_result_content",
    ),
    "account_scope": GuardMutation(
        "src/stock_profiler/modules/delivery/access.py",
        "read_denial",
        "not account_ids or not set(account_ids).issubset(principal.account_ids)",
        "False",
        "tests/integration/test_result_access_isolation.py"
        "::test_scope_denials_are_durable_without_disclosing_result_content",
    ),
    "notification_fallback": GuardMutation(
        "src/stock_profiler/modules/delivery/monitoring_notifications.py",
        "route_synthetic_notifications",
        'item.priority == "P0" or request.immediate_result != "ACCEPTED"',
        "False",
        "tests/integration/test_monitoring_workspace.py"
        "::test_notification_fallback_preserves_action_and_minimizes_external_content",
    ),
}

# -I prevents a caller's PYTHONPATH from replacing the isolated source under test.
PYTEST_WORKER = """
import pathlib
import sys
root = pathlib.Path.cwd()
sys.path.insert(0, str(root / "src"))
import stock_profiler
assert pathlib.Path(stock_profiler.__file__).resolve().is_relative_to(root / "src")
import pytest
raise SystemExit(pytest.main(sys.argv[1:]))
"""


def mutate_guard(path: Path, function: str, condition: str, replacement: str) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    expected = ast.dump(ast.parse(condition, mode="eval").body)
    matches = [
        node
        for definition in ast.walk(tree)
        if isinstance(definition, ast.FunctionDef) and definition.name == function
        for node in ast.walk(definition)
        if isinstance(node, ast.If | ast.IfExp) and ast.dump(node.test) == expected
    ]
    if len(matches) != 1:
        raise ValueError("Mutation must match exactly one frozen guard")
    matches[0].test = ast.parse(replacement, mode="eval").body
    ast.fix_missing_locations(tree)
    compile(tree, str(path), "exec")
    path.write_text(ast.unparse(tree) + "\n", encoding="utf-8")


def read_result(
    path: Path, returncode: int, *, expected_failure: str | None = None
) -> dict[str, str]:
    if returncode != (1 if expected_failure else 0):
        raise ValueError("Contract process did not pass")
    root = ElementTree.parse(path).getroot()
    outcomes: dict[str, str] = {}
    for case in root.iter("testcase"):
        identity = f"{case.attrib['classname']}::{case.attrib['name']}"
        if identity in outcomes or any(
            case.find(kind) is not None for kind in ("error", "skipped")
        ):
            raise ValueError("Contract results contain duplicate or nonpassing cases")
        failure = case.find("failure")
        if failure is None:
            outcomes[identity] = "passed"
            continue
        message = failure.attrib.get("message", "")
        assertion = (
            message.startswith("assert ")
            or message.startswith("AssertionError")
            or "DID NOT RAISE" in message
        )
        if (
            expected_failure is None
            or case.attrib["name"].split("[", 1)[0] != expected_failure
            or not assertion
        ):
            raise ValueError("Unexpected failure is not a killed protection mutation")
        outcomes[identity] = "assertion_failed"
    if not outcomes:
        raise ValueError("Contract results are empty")
    if expected_failure and "assertion_failed" not in outcomes.values():
        raise ValueError("Protection mutation survived")
    return outcomes


def catalog() -> dict[str, object]:
    return {
        "contract_version": "1.0.0",
        "synthetic": True,
        "evidence_stage": "D0",
        "purpose": "OFFLINE_CONTRACT",
        "actionability": "NON_ACTIONABLE",
        "qualification_granted": False,
        "activation_authorized": False,
        "required_repetitions": 2,
        "failure_tolerance": 0,
        "groups": GROUPS,
        "mutations": {name: mutation._asdict() for name, mutation in MUTATIONS.items()},
    }


def verify_coverage(outcomes: dict[str, str], groups: dict[str, list[str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for group, paths in groups.items():
        covered: set[str] = set()
        for path in paths:
            prefix = path.removesuffix(".py").replace("/", ".")
            matches = {
                identity
                for identity in outcomes
                if identity.split("::")[0] == prefix
                or identity.split("::")[0].startswith(prefix + ".")
            }
            if not matches:
                raise ValueError(f"Missing required coverage: {group}")
            covered.update(matches)
        counts[group] = len(covered)
    return counts


def run_tests(
    root: Path, destination: Path, paths: list[str], *, expected_failure: str | None = None
) -> dict[str, str]:
    destination.mkdir()
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("STOCK_PROFILER_", "PYTHON", "PYTEST", "COVERAGE"))
    }
    environment.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONDONTWRITEBYTECODE="1")
    report = destination / "results.xml"
    with (destination / "pytest.log").open("w", encoding="utf-8") as log:
        process = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                PYTEST_WORKER,
                "-o",
                "addopts=--strict-markers",
                "-p",
                "no:cacheprovider",
                "--basetemp",
                str(destination / "storage"),
                "--junitxml",
                str(report),
                *paths,
            ],
            cwd=root,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=1800,
        )
    try:
        return read_result(report, process.returncode, expected_failure=expected_failure)
    except (ValueError, OSError, ElementTree.ParseError):
        print((destination / "pytest.log").read_text(encoding="utf-8")[-6000:], file=sys.stderr)
        raise


def unpack_source(archive: bytes, destination: Path) -> None:
    destination.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as source:
        source.extractall(destination, filter="data")


def run_matrix(root: Path, source: str, output: Path) -> int:
    output = output.resolve()
    if output.is_relative_to(root) or output.exists():
        raise ValueError("Output must be a new file outside the source repository")
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True).strip():
        raise ValueError("Matrix requires a clean, committed source")
    if not __debug__ or sys.flags.optimize:
        raise ValueError("Assertions must be enabled")
    for name in ("api_shared_secret", "auth_bootstrap_token", "auth_recovery_token"):
        if (Path("/run/secrets") / name).exists():
            raise ValueError("Mounted runtime secrets are not permitted")
    archive = subprocess.check_output(["git", "archive", "--format=tar", source], cwd=root)
    artifact = catalog() | {
        "source_sha": source,
        "source_archive_sha256": hashlib.sha256(archive).hexdigest(),
        "lock_sha256": hashlib.sha256((root / "uv.lock").read_bytes()).hexdigest(),
        "python_version": sys.version,
        "framework_version": version("m-agent"),
        "verdict": "FAILED",
        "baseline_runs": [],
        "mutation_results": {},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    paths = list(dict.fromkeys(path for paths in GROUPS.values() for path in paths))
    baseline: list[dict[str, str]] = []
    mutations: dict[str, dict[str, str]] = {}
    artifact["baseline_runs"] = baseline
    artifact["mutation_results"] = mutations
    try:
        with tempfile.TemporaryDirectory(prefix="protection-contract-") as temporary:
            workspace = Path(temporary)
            for repetition in range(2):
                isolated = workspace / f"baseline-source-{repetition}"
                unpack_source(archive, isolated)
                print(f"Baseline {repetition + 1}/2", file=sys.stderr, flush=True)
                baseline.append(run_tests(isolated, workspace / f"baseline-{repetition}", paths))
                artifact["group_counts"] = verify_coverage(baseline[-1], GROUPS)
            if baseline[0] != baseline[1]:
                raise ValueError("Repeated contract inventories or results differ")
            for name, mutation in MUTATIONS.items():
                isolated = workspace / f"mutation-source-{name}"
                unpack_source(archive, isolated)
                mutate_guard(
                    isolated / mutation.path,
                    mutation.function,
                    mutation.condition,
                    mutation.replacement,
                )
                print(f"Mutation {name}", file=sys.stderr, flush=True)
                result = run_tests(
                    isolated,
                    workspace / f"mutation-{name}",
                    [mutation.test],
                    expected_failure=mutation.test.split("::")[1],
                )
                expected = {
                    identity
                    for identity in baseline[0]
                    if identity.split("::")[-1].split("[", 1)[0] == mutation.test.split("::")[1]
                }
                if set(result) != expected:
                    raise ValueError("Mutation did not exercise the baseline test inventory")
                mutations[name] = result
        artifact["verdict"] = "PASSED"
    finally:
        output.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", action="store_true")
    parser.add_argument("--source")
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    if arguments.catalog:
        print(json.dumps(catalog(), sort_keys=True))
        return 0
    if arguments.source is None or arguments.output is None:
        parser.error("--source and --output are required for a matrix run")
    root = Path(__file__).resolve().parents[1]
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    if arguments.source != head:
        print("Source identity mismatch", file=sys.stderr)
        return 1
    try:
        return run_matrix(root, head, arguments.output)
    except (ValueError, OSError, subprocess.SubprocessError, ElementTree.ParseError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
