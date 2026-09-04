"""Repository-role and synthetic-fixture guard for the public source repository."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(os.environ.get("REPOSITORY_GUARD_ROOT", Path(__file__).resolve().parents[1]))
MAX_FILE_BYTES = 5 * 1024 * 1024
FORBIDDEN_PARTS = frozenset(
    {
        ".scratch",
        "control",
        "implementation",
        "__pycache__",
    }
)
FORBIDDEN_DATA_PATH_PARTS = frozenset({"data", "evidence"})
ALLOWED_TOP_LEVEL_PATHS = frozenset(
    {
        ".dockerignore",
        ".github",
        ".gitignore",
        ".pre-commit-config.yaml",
        "CONTRIBUTING.md",
        "Dockerfile",
        "LICENSE",
        "Makefile",
        "NOTICE",
        "README.md",
        "SECURITY.md",
        "THIRD_PARTY_NOTICES.md",
        "alembic.ini",
        "deploy",
        "docs",
        "migrations",
        "pyproject.toml",
        "scripts",
        "src",
        "tests",
        "uv.lock",
        "web",
    }
)
FORBIDDEN_SUFFIXES = frozenset(
    {
        ".bak",
        ".backup",
        ".cer",
        ".crt",
        ".db",
        ".der",
        ".key",
        ".log",
        ".pem",
        ".pfx",
        ".p12",
        ".sqlite",
        ".sqlite3",
    }
)
FORBIDDEN_TEXT = (
    ".scratch" + "/",
    "\nAssignee:",
    "\nParent:",
    "\nTriage:",
    "stock-profiler" + "-control",
)
STRUCTURED_DATA_SUFFIXES = frozenset(
    {
        ".avro",
        ".csv",
        ".feather",
        ".gif",
        ".jpeg",
        ".jpg",
        ".json",
        ".jsonl",
        ".ndjson",
        ".orc",
        ".parquet",
        ".pdf",
        ".png",
        ".tsv",
        ".webp",
        ".xlsx",
        ".xls",
    }
)
ALLOWED_PUBLIC_JSON_PATHS = frozenset(
    {
        Path("web/openapi.json"),
        Path("web/package.json"),
        Path("web/tsconfig.json"),
    }
)
SYNTHETIC_FIXTURE_ROOT = Path("tests/fixtures/synthetic")
SECRET_EXAMPLE_PATHS = frozenset(
    {
        Path("deploy/secrets/api_shared_secret.example"),
        Path("deploy/secrets/auth_bootstrap_token.example"),
        Path("deploy/secrets/auth_recovery_token.example"),
    }
)
SECRET_EXAMPLE_CONTENT = b"synthetic-placeholder-do-not-deploy\n"


@dataclass(frozen=True)
class CandidateFile:
    """A public candidate read from an index blob whenever one exists."""

    relative_path: Path
    content: bytes


def git_paths(*arguments: str) -> list[Path]:
    """Read NUL-delimited Git paths without consulting ignored output."""
    completed = subprocess.run(
        ["git", "ls-files", "-z", *arguments],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return sorted(Path(os.fsdecode(path)) for path in completed.stdout.split(b"\0") if path)


def staged_gitlink_paths() -> list[Path]:
    """Return index entries that would introduce a submodule Gitlink."""
    completed = subprocess.run(
        ["git", "ls-files", "-s", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    gitlinks: list[Path] = []
    for entry in completed.stdout.split(b"\0"):
        if not entry:
            continue
        metadata, path = entry.split(b"\t", maxsplit=1)
        mode = metadata.split(maxsplit=1)[0]
        if mode == b"160000":
            gitlinks.append(Path(os.fsdecode(path)))
    return sorted(gitlinks)


def candidate_files() -> list[CandidateFile]:
    """Read staged content for indexed files and worktree content for untracked files."""
    indexed_paths = set(git_paths("--cached"))
    paths = git_paths("--cached", "--others", "--exclude-standard")
    candidates: list[CandidateFile] = []
    for relative_path in paths:
        if relative_path in indexed_paths:
            completed = subprocess.run(
                ["git", "show", f":{relative_path.as_posix()}"],
                cwd=ROOT,
                check=True,
                capture_output=True,
            )
            content = completed.stdout
        else:
            content = (ROOT / relative_path).read_bytes()
        candidates.append(CandidateFile(relative_path=relative_path, content=content))
    return candidates


def history_candidate_trees() -> list[tuple[str, list[CandidateFile], list[Path]]]:
    """Read each committed tree so a later correction cannot conceal an earlier violation."""
    commits = subprocess.run(
        ["git", "rev-list", "--all"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    trees: list[tuple[str, list[CandidateFile], list[Path]]] = []
    for commit in commits:
        completed = subprocess.run(
            ["git", "ls-tree", "-rz", "--full-tree", commit],
            cwd=ROOT,
            check=True,
            capture_output=True,
        )
        candidates: list[CandidateFile] = []
        gitlinks: list[Path] = []
        for entry in completed.stdout.split(b"\0"):
            if not entry:
                continue
            metadata, encoded_path = entry.split(b"\t", maxsplit=1)
            mode = metadata.split(maxsplit=1)[0]
            relative_path = Path(os.fsdecode(encoded_path))
            if mode == b"160000":
                gitlinks.append(relative_path)
                continue
            content = subprocess.run(
                ["git", "show", f"{commit}:{relative_path.as_posix()}"],
                cwd=ROOT,
                check=True,
                capture_output=True,
            ).stdout
            candidates.append(CandidateFile(relative_path=relative_path, content=content))
        trees.append((commit, candidates, sorted(set(gitlinks))))
    return trees


def check_path(candidate: CandidateFile) -> list[str]:
    """Detect forbidden path shapes and oversized public files."""
    relative = candidate.relative_path
    errors: list[str] = []
    if relative.parts[0] not in ALLOWED_TOP_LEVEL_PATHS:
        errors.append(f"top-level path is not allowlisted: {relative}")
    if any(part in FORBIDDEN_PARTS or part.endswith("-session") for part in relative.parts):
        errors.append(f"forbidden path: {relative}")
    if any(marker in relative.as_posix().casefold() for marker in FORBIDDEN_TEXT):
        errors.append(f"forbidden repository-role identifier in path {relative}")
    if any(part.casefold() in FORBIDDEN_DATA_PATH_PARTS for part in relative.parts):
        errors.append(f"forbidden data or evidence path: {relative}")
    if relative.parts[:2] == ("tests", "fixtures") and relative.parts[:3] != (
        "tests",
        "fixtures",
        "synthetic",
    ):
        errors.append(f"fixture is outside the synthetic fixture allowlist: {relative}")
    if relative.name == ".env" or relative.name.startswith(".env."):
        errors.append(f"forbidden environment file: {relative}")
    if relative.suffix.casefold() in FORBIDDEN_SUFFIXES:
        errors.append(f"forbidden file type: {relative}")
    if relative.suffix.casefold() in STRUCTURED_DATA_SUFFIXES:
        is_synthetic_fixture = relative.is_relative_to(SYNTHETIC_FIXTURE_ROOT)
        if not is_synthetic_fixture and relative not in ALLOWED_PUBLIC_JSON_PATHS:
            errors.append(f"structured data is not allowlisted: {relative}")
    if len(candidate.content) > MAX_FILE_BYTES:
        errors.append(f"file exceeds {MAX_FILE_BYTES} bytes: {relative}")
    return errors


def check_text(candidate: CandidateFile) -> list[str]:
    """Reject explicit control-surface identifiers without parsing private content."""
    if candidate.relative_path.suffix in {".png", ".jpg", ".jpeg", ".gif", ".woff", ".woff2"}:
        return []
    try:
        content = candidate.content.decode(encoding="utf-8")
    except UnicodeDecodeError:
        return [f"binary file is not allowed: {candidate.relative_path}"]
    return [
        f"forbidden repository-role identifier in {candidate.relative_path}"
        for marker in FORBIDDEN_TEXT
        if marker in content
    ]


def check_synthetic_fixture(candidate: CandidateFile, paths: set[Path]) -> list[str]:
    """Require provenance metadata for structured fixtures."""
    relative = candidate.relative_path
    if not relative.is_relative_to(SYNTHETIC_FIXTURE_ROOT):
        return []
    if relative.suffix.casefold() != ".json":
        if relative.suffix.casefold() not in STRUCTURED_DATA_SUFFIXES:
            return []
        metadata_path = Path(f"{relative.as_posix()}.metadata.json")
        if metadata_path not in paths:
            return [f"synthetic fixture metadata sidecar missing: {relative}"]
        return []
    try:
        payload = json.loads(candidate.content.decode(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return [f"synthetic fixture must be valid JSON: {relative}"]
    required = ("synthetic", "generator_version", "seed")
    if not all(key in payload for key in required) or payload["synthetic"] is not True:
        return [f"synthetic fixture metadata missing: {relative}"]
    return []


def check_secret_examples(candidates: dict[Path, CandidateFile]) -> list[str]:
    """Keep tracked secret templates unusable as credentials or deployment inputs."""
    if Path("deploy/compose.yml") not in candidates:
        return []
    errors: list[str] = []
    for path in SECRET_EXAMPLE_PATHS:
        candidate = candidates.get(path)
        if candidate is None:
            errors.append(f"required secret example missing: {path}")
        elif candidate.content != SECRET_EXAMPLE_CONTENT:
            message = "secret example must contain only the approved synthetic placeholder"
            errors.append(f"{message}: {path}")
    return errors


def guard_errors(candidates: list[CandidateFile], gitlinks: list[Path]) -> list[str]:
    """Evaluate common content rules for staged or historical candidate blobs."""
    paths = {candidate.relative_path for candidate in candidates}
    candidates_by_path = {candidate.relative_path: candidate for candidate in candidates}
    errors = [error for candidate in candidates for error in check_path(candidate)]
    errors.extend(error for candidate in candidates for error in check_text(candidate))
    errors.extend(
        error for candidate in candidates for error in check_synthetic_fixture(candidate, paths)
    )
    errors.extend(check_secret_examples(candidates_by_path))
    if gitlinks:
        errors.append("gitlinks are not allowed in the public baseline")
    if any(candidate.relative_path == Path(".gitmodules") for candidate in candidates):
        errors.append("git submodules are not allowed in the public baseline")
    return errors


def main() -> None:
    """Exit nonzero if public-source safety invariants are violated."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--history", action="store_true")
    arguments = parser.parse_args()
    if arguments.history:
        errors = [
            f"{commit}: {error}"
            for commit, candidates, gitlinks in history_candidate_trees()
            for error in guard_errors(candidates, gitlinks)
        ]
    else:
        candidates = candidate_files()
        gitlinks = staged_gitlink_paths()
        errors = guard_errors(candidates, gitlinks)
    if errors:
        raise SystemExit("\n".join(sorted(set(errors))))
    print("repository guard passed")


if __name__ == "__main__":
    main()
