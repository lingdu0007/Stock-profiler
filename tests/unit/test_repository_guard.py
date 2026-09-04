from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GUARD = ROOT / "scripts" / "repository_guard.py"
PRIVATE_REPOSITORY_MARKER = "stock-profiler" + "-control"


def run_git(repository: Path, *arguments: str) -> None:
    """Run a Git setup command in an isolated repository."""
    subprocess.run(["git", *arguments], cwd=repository, check=True)


def test_repository_guard_reads_staged_content_not_replaced_worktree(tmp_path: Path) -> None:
    run_git(tmp_path, "init")
    run_git(tmp_path, "config", "user.email", "synthetic@example.invalid")
    run_git(tmp_path, "config", "user.name", "Synthetic Test")
    source = tmp_path / "README.md"
    source.write_text("safe\nAssignee: forbidden\n", encoding="utf-8")
    run_git(tmp_path, "add", "README.md")
    source.write_text("safe\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(GUARD)],
        cwd=tmp_path,
        capture_output=True,
        env={**os.environ, "REPOSITORY_GUARD_ROOT": str(tmp_path)},
        text=True,
    )

    assert result.returncode != 0
    assert "forbidden repository-role identifier" in result.stderr


@pytest.mark.parametrize(
    ("filename", "content", "expected_error"),
    [
        (".env", "VALUE=synthetic\n", "forbidden environment file"),
        ("deployment.crt", "synthetic certificate\n", "forbidden file type"),
        ("README.md", PRIVATE_REPOSITORY_MARKER + "\n", "forbidden repository-role identifier"),
    ],
)
def test_repository_guard_rejects_forbidden_public_content(
    tmp_path: Path, filename: str, content: str, expected_error: str
) -> None:
    run_git(tmp_path, "init")
    run_git(tmp_path, "config", "user.email", "synthetic@example.invalid")
    run_git(tmp_path, "config", "user.name", "Synthetic Test")
    source = tmp_path / filename
    source.write_text(content, encoding="utf-8")
    run_git(tmp_path, "add", filename)

    result = subprocess.run(
        [sys.executable, str(GUARD)],
        cwd=tmp_path,
        capture_output=True,
        env={**os.environ, "REPOSITORY_GUARD_ROOT": str(tmp_path)},
        text=True,
    )

    assert result.returncode != 0
    assert expected_error in result.stderr


def test_repository_guard_requires_all_test_fixtures_to_be_declared_synthetic(
    tmp_path: Path,
) -> None:
    run_git(tmp_path, "init")
    run_git(tmp_path, "config", "user.email", "synthetic@example.invalid")
    run_git(tmp_path, "config", "user.name", "Synthetic Test")
    fixture = tmp_path / "tests" / "fixtures" / "unspecified.json"
    fixture.parent.mkdir(parents=True)
    fixture.write_text('{"instrument": "FABRICATED"}\n', encoding="utf-8")
    run_git(tmp_path, "add", "tests/fixtures/unspecified.json")

    result = subprocess.run(
        [sys.executable, str(GUARD)],
        cwd=tmp_path,
        capture_output=True,
        env={**os.environ, "REPOSITORY_GUARD_ROOT": str(tmp_path)},
        text=True,
    )

    assert result.returncode != 0
    assert "fixture is outside the synthetic fixture allowlist" in result.stderr


def test_repository_guard_rejects_gitlinks(tmp_path: Path) -> None:
    run_git(tmp_path, "init")
    run_git(tmp_path, "config", "user.email", "synthetic@example.invalid")
    run_git(tmp_path, "config", "user.name", "Synthetic Test")
    source = tmp_path / "README.md"
    source.write_text("synthetic\n", encoding="utf-8")
    run_git(tmp_path, "add", "README.md")
    run_git(tmp_path, "commit", "-m", "synthetic fixture")
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    run_git(tmp_path, "update-index", "--add", "--cacheinfo", f"160000,{commit},vendor")

    result = subprocess.run(
        [sys.executable, str(GUARD)],
        cwd=tmp_path,
        capture_output=True,
        env={**os.environ, "REPOSITORY_GUARD_ROOT": str(tmp_path)},
        text=True,
    )

    assert result.returncode != 0
    assert "gitlinks are not allowed" in result.stderr


@pytest.mark.parametrize(
    ("filename", "content", "expected_error"),
    [
        (
            PRIVATE_REPOSITORY_MARKER + "/README.md",
            "safe\n",
            "forbidden repository-role identifier",
        ),
        ("data/derived.csv", "symbol,score\nFABRICATED,1\n", "structured data"),
        ("data/derived.json", '{"synthetic": true}\n', "structured data"),
        ("data/evidence.png", "not-a-real-image\n", "structured data"),
    ],
)
def test_repository_guard_rejects_private_markers_and_unallowlisted_data(
    tmp_path: Path, filename: str, content: str, expected_error: str
) -> None:
    run_git(tmp_path, "init")
    run_git(tmp_path, "config", "user.email", "synthetic@example.invalid")
    run_git(tmp_path, "config", "user.name", "Synthetic Test")
    source = tmp_path / filename
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(content, encoding="utf-8")
    run_git(tmp_path, "add", filename)

    result = subprocess.run(
        [sys.executable, str(GUARD)],
        cwd=tmp_path,
        capture_output=True,
        env={**os.environ, "REPOSITORY_GUARD_ROOT": str(tmp_path)},
        text=True,
    )

    assert result.returncode != 0
    assert expected_error in result.stderr


def test_repository_guard_requires_a_synthetic_sidecar_for_non_json_fixture(tmp_path: Path) -> None:
    run_git(tmp_path, "init")
    run_git(tmp_path, "config", "user.email", "synthetic@example.invalid")
    run_git(tmp_path, "config", "user.name", "Synthetic Test")
    fixture = tmp_path / "tests" / "fixtures" / "synthetic" / "case.csv"
    fixture.parent.mkdir(parents=True)
    fixture.write_text("instrument,value\nFABRICATED,1\n", encoding="utf-8")
    run_git(tmp_path, "add", fixture.relative_to(tmp_path).as_posix())

    missing_sidecar = subprocess.run(
        [sys.executable, str(GUARD)],
        cwd=tmp_path,
        capture_output=True,
        env={**os.environ, "REPOSITORY_GUARD_ROOT": str(tmp_path)},
        text=True,
    )

    assert missing_sidecar.returncode != 0
    assert "synthetic fixture metadata sidecar missing" in missing_sidecar.stderr

    sidecar = fixture.with_name("case.csv.metadata.json")
    sidecar.write_text(
        '{"synthetic": true, "generator_version": "test", "seed": 1}\n',
        encoding="utf-8",
    )
    run_git(tmp_path, "add", sidecar.relative_to(tmp_path).as_posix())

    accepted = subprocess.run(
        [sys.executable, str(GUARD)],
        cwd=tmp_path,
        capture_output=True,
        env={**os.environ, "REPOSITORY_GUARD_ROOT": str(tmp_path)},
        text=True,
    )

    assert accepted.returncode == 0


def test_repository_guard_requires_secret_templates_to_remain_synthetic(tmp_path: Path) -> None:
    run_git(tmp_path, "init")
    run_git(tmp_path, "config", "user.email", "synthetic@example.invalid")
    run_git(tmp_path, "config", "user.name", "Synthetic Test")
    secrets = tmp_path / "deploy" / "secrets"
    secrets.mkdir(parents=True)
    for name in (
        "api_shared_secret.example",
        "auth_bootstrap_token.example",
        "auth_recovery_token.example",
    ):
        (secrets / name).write_text("synthetic-placeholder-do-not-deploy\n", encoding="utf-8")
    (secrets / "api_shared_secret.example").write_text("not-a-placeholder\n", encoding="utf-8")
    (tmp_path / "deploy" / "compose.yml").write_text("name: synthetic\n", encoding="utf-8")
    run_git(tmp_path, "add", "deploy/secrets")
    run_git(tmp_path, "add", "deploy/compose.yml")

    result = subprocess.run(
        [sys.executable, str(GUARD)],
        cwd=tmp_path,
        capture_output=True,
        env={**os.environ, "REPOSITORY_GUARD_ROOT": str(tmp_path)},
        text=True,
    )

    assert result.returncode != 0
    assert "secret example must contain only the approved synthetic placeholder" in result.stderr


@pytest.mark.parametrize("filename", ("data/derived-notes.md", "evidence/source-record.txt"))
def test_repository_guard_rejects_textual_data_and_evidence_paths(
    tmp_path: Path, filename: str
) -> None:
    run_git(tmp_path, "init")
    run_git(tmp_path, "config", "user.email", "synthetic@example.invalid")
    run_git(tmp_path, "config", "user.name", "Synthetic Test")
    source = tmp_path / filename
    source.parent.mkdir(parents=True)
    source.write_text("synthetic-looking text\n", encoding="utf-8")
    run_git(tmp_path, "add", filename)

    result = subprocess.run(
        [sys.executable, str(GUARD)],
        cwd=tmp_path,
        capture_output=True,
        env={**os.environ, "REPOSITORY_GUARD_ROOT": str(tmp_path)},
        text=True,
    )

    assert result.returncode != 0
    assert "forbidden data or evidence path" in result.stderr


def test_repository_guard_rejects_unknown_top_level_content_categories(tmp_path: Path) -> None:
    run_git(tmp_path, "init")
    run_git(tmp_path, "config", "user.email", "synthetic@example.invalid")
    run_git(tmp_path, "config", "user.name", "Synthetic Test")
    source = tmp_path / "records" / "relabelled-evidence.md"
    source.parent.mkdir(parents=True)
    source.write_text("synthetic-looking text\n", encoding="utf-8")
    run_git(tmp_path, "add", source.relative_to(tmp_path).as_posix())

    result = subprocess.run(
        [sys.executable, str(GUARD)],
        cwd=tmp_path,
        capture_output=True,
        env={**os.environ, "REPOSITORY_GUARD_ROOT": str(tmp_path)},
        text=True,
    )

    assert result.returncode != 0
    assert "top-level path is not allowlisted" in result.stderr


@pytest.mark.parametrize(
    ("filename", "content", "expected_error"),
    [
        ("docs/Control/note.md", "synthetic\n", "forbidden path"),
        ("docs/review-Session/note.md", "synthetic\n", "forbidden path"),
        ("docs/.ENV", "SYNTHETIC=1\n", "forbidden environment file"),
        ("README.md", "assignee: synthetic\n", "forbidden repository-role identifier"),
        ("README.md", "PARENT: synthetic\n", "forbidden repository-role identifier"),
        ("README.md", "triage: synthetic\n", "forbidden repository-role identifier"),
    ],
)
def test_repository_guard_rejects_casefolded_paths_and_first_line_role_markers(
    tmp_path: Path, filename: str, content: str, expected_error: str
) -> None:
    run_git(tmp_path, "init")
    run_git(tmp_path, "config", "user.email", "synthetic@example.invalid")
    run_git(tmp_path, "config", "user.name", "Synthetic Test")
    source = tmp_path / filename
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(content, encoding="utf-8")
    run_git(tmp_path, "add", filename)

    result = subprocess.run(
        [sys.executable, str(GUARD)],
        cwd=tmp_path,
        capture_output=True,
        env={**os.environ, "REPOSITORY_GUARD_ROOT": str(tmp_path)},
        text=True,
    )

    assert result.returncode != 0
    assert expected_error in result.stderr


@pytest.mark.parametrize("suffix", (".txt", ".yaml", ".unknown"))
def test_repository_guard_requires_verified_sidecars_for_every_synthetic_payload(
    tmp_path: Path, suffix: str
) -> None:
    run_git(tmp_path, "init")
    run_git(tmp_path, "config", "user.email", "synthetic@example.invalid")
    run_git(tmp_path, "config", "user.name", "Synthetic Test")
    fixture = tmp_path / "tests" / "fixtures" / "synthetic" / f"case{suffix}"
    fixture.parent.mkdir(parents=True)
    fixture.write_text("fabricated payload\n", encoding="utf-8")
    run_git(tmp_path, "add", fixture.relative_to(tmp_path).as_posix())

    result = subprocess.run(
        [sys.executable, str(GUARD)],
        cwd=tmp_path,
        capture_output=True,
        env={**os.environ, "REPOSITORY_GUARD_ROOT": str(tmp_path)},
        text=True,
    )

    assert result.returncode != 0
    assert "synthetic fixture metadata sidecar missing" in result.stderr


def test_repository_guard_rejects_an_unverified_synthetic_payload_sidecar(tmp_path: Path) -> None:
    run_git(tmp_path, "init")
    run_git(tmp_path, "config", "user.email", "synthetic@example.invalid")
    run_git(tmp_path, "config", "user.name", "Synthetic Test")
    fixture = tmp_path / "tests" / "fixtures" / "synthetic" / "case.txt"
    fixture.parent.mkdir(parents=True)
    fixture.write_text("fabricated payload\n", encoding="utf-8")
    sidecar = fixture.with_name("case.txt.metadata.json")
    sidecar.write_text("{}\n", encoding="utf-8")
    run_git(tmp_path, "add", fixture.relative_to(tmp_path).as_posix())
    run_git(tmp_path, "add", sidecar.relative_to(tmp_path).as_posix())

    result = subprocess.run(
        [sys.executable, str(GUARD)],
        cwd=tmp_path,
        capture_output=True,
        env={**os.environ, "REPOSITORY_GUARD_ROOT": str(tmp_path)},
        text=True,
    )

    assert result.returncode != 0
    assert "synthetic fixture metadata missing" in result.stderr


def test_repository_guard_history_scans_deleted_content(tmp_path: Path) -> None:
    run_git(tmp_path, "init")
    run_git(tmp_path, "config", "user.email", "synthetic@example.invalid")
    run_git(tmp_path, "config", "user.name", "Synthetic Test")
    source = tmp_path / "evidence" / "removed-record.md"
    source.parent.mkdir(parents=True)
    source.write_text("synthetic-looking text\n", encoding="utf-8")
    run_git(tmp_path, "add", source.relative_to(tmp_path).as_posix())
    run_git(tmp_path, "commit", "-m", "add forbidden synthetic-looking content")
    source.unlink()
    run_git(tmp_path, "add", "--update")
    run_git(tmp_path, "commit", "-m", "remove forbidden synthetic-looking content")

    result = subprocess.run(
        [sys.executable, str(GUARD), "--history"],
        cwd=tmp_path,
        capture_output=True,
        env={**os.environ, "REPOSITORY_GUARD_ROOT": str(tmp_path)},
        text=True,
    )

    assert result.returncode != 0
    assert "forbidden data or evidence path" in result.stderr


def test_repository_guard_history_rechecks_each_commit_tree(tmp_path: Path) -> None:
    run_git(tmp_path, "init")
    run_git(tmp_path, "config", "user.email", "synthetic@example.invalid")
    run_git(tmp_path, "config", "user.name", "Synthetic Test")
    secrets = tmp_path / "deploy" / "secrets"
    secrets.mkdir(parents=True)
    for name in (
        "api_shared_secret.example",
        "auth_bootstrap_token.example",
        "auth_recovery_token.example",
    ):
        (secrets / name).write_text("synthetic-placeholder-do-not-deploy\n", encoding="utf-8")
    (tmp_path / "deploy" / "compose.yml").write_text("name: synthetic\n", encoding="utf-8")
    run_git(tmp_path, "add", "deploy")
    run_git(tmp_path, "commit", "-m", "add synthetic secret templates")

    changed_template = secrets / "api_shared_secret.example"
    changed_template.write_text("unsafe-intermediate-value\n", encoding="utf-8")
    run_git(tmp_path, "add", changed_template.relative_to(tmp_path).as_posix())
    run_git(tmp_path, "commit", "-m", "break synthetic secret template")

    changed_template.write_text("synthetic-placeholder-do-not-deploy\n", encoding="utf-8")
    run_git(tmp_path, "add", changed_template.relative_to(tmp_path).as_posix())
    run_git(tmp_path, "commit", "-m", "restore synthetic secret template")

    result = subprocess.run(
        [sys.executable, str(GUARD), "--history"],
        cwd=tmp_path,
        capture_output=True,
        env={**os.environ, "REPOSITORY_GUARD_ROOT": str(tmp_path)},
        text=True,
    )

    assert result.returncode != 0
    assert "secret example must contain only the approved synthetic placeholder" in result.stderr


def test_repository_guard_history_does_not_backfill_missing_fixture_metadata(
    tmp_path: Path,
) -> None:
    run_git(tmp_path, "init")
    run_git(tmp_path, "config", "user.email", "synthetic@example.invalid")
    run_git(tmp_path, "config", "user.name", "Synthetic Test")
    fixture = tmp_path / "tests" / "fixtures" / "synthetic" / "case.csv"
    fixture.parent.mkdir(parents=True)
    fixture.write_text("instrument,value\nFABRICATED,1\n", encoding="utf-8")
    run_git(tmp_path, "add", fixture.relative_to(tmp_path).as_posix())
    run_git(tmp_path, "commit", "-m", "add fixture without metadata")

    metadata = fixture.with_name("case.csv.metadata.json")
    metadata.write_text(
        '{"synthetic": true, "generator_version": "test", "seed": 1}\n',
        encoding="utf-8",
    )
    run_git(tmp_path, "add", metadata.relative_to(tmp_path).as_posix())
    run_git(tmp_path, "commit", "-m", "add fixture metadata")

    result = subprocess.run(
        [sys.executable, str(GUARD), "--history"],
        cwd=tmp_path,
        capture_output=True,
        env={**os.environ, "REPOSITORY_GUARD_ROOT": str(tmp_path)},
        text=True,
    )

    assert result.returncode != 0
    assert "synthetic fixture metadata sidecar missing" in result.stderr
