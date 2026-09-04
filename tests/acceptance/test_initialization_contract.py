from __future__ import annotations

import tomllib
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from stock_profiler.modules.qualification.coverage import deterministic_policy_coverage

ROOT = Path(__file__).resolve().parents[2]


@given(st.text(min_size=0, max_size=32))
def test_policy_coverage_is_explicitly_not_applicable_without_policy_modules(_: str) -> None:
    report = deterministic_policy_coverage()

    assert report.status == "not-applicable"
    assert report.branch_coverage_target == 100
    assert report.modules == ()


def test_controlled_build_and_release_gates_are_pinned() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    ci_workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    security_workflow = (ROOT / ".github" / "workflows" / "security.yml").read_text(
        encoding="utf-8"
    )
    pre_commit = (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    compose = (ROOT / "deploy" / "compose.yml").read_text(encoding="utf-8")
    caddyfile = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
    gateway_dockerfile = (ROOT / "deploy" / "gateway.Dockerfile").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    baseline = (ROOT / "docs" / "development" / "baseline.md").read_text(encoding="utf-8")
    documentation_issue = (ROOT / ".github" / "ISSUE_TEMPLATE" / "documentation.yml").read_text(
        encoding="utf-8"
    )

    assert pyproject["build-system"]["requires"] == ["setuptools==80.9.0"]
    dependencies = pyproject["project"]["dependencies"]
    assert any(requirement.startswith("webauthn") for requirement in dependencies)
    assert "ARTIFACT_VERSION := $(shell uv run python -c" in makefile
    assert "stock_profiler-$(ARTIFACT_VERSION).tar.gz" in makefile
    assert "stock-profiler-web-$(ARTIFACT_VERSION).tar.gz" in makefile
    assert "$(GITLEAKS) protect --staged --redact --no-banner" in makefile
    assert "pre-commit install --install-hooks" in makefile
    assert "repository-guard" in pre_commit
    assert "scripts/repository_guard.py" in pre_commit
    assert "$(PNPM) exec playwright install chromium" in makefile
    assert "STOCK_PROFILER_SOURCE_SHA=${{ github.sha }}" in ci_workflow
    assert "make artifacts" in ci_workflow
    assert "SOURCE_DATE_EPOCH" in ci_workflow
    assert "cache: pnpm" not in ci_workflow
    assert "cache: pnpm" not in workflow
    backend_workflow = ci_workflow.split("  web:", maxsplit=1)[0]
    assert "actions/setup-node@" in backend_workflow
    assert "corepack pnpm@10.17.1 --dir web install --frozen-lockfile" in backend_workflow
    assert backend_workflow.index(
        "corepack pnpm@10.17.1 --dir web install --frozen-lockfile"
    ) < backend_workflow.index("uv run pytest")
    assert "verify-release-candidate:" in workflow
    assert "needs: verify-release-candidate" in workflow
    assert "tags:" in workflow
    assert '"v*"' in workflow
    assert "workflow_dispatch:" not in workflow
    assert "inputs.tag" not in workflow
    assert "EXPECTED_TAG: ${{ github.ref_name }}" in workflow
    assert "persist-credentials: false" in workflow
    assert '[[ "$EXPECTED_TAG" =~ ^v[0-9]+\\.[0-9]+\\.[0-9]+$ ]]' in workflow
    assert (
        "subject-name: ${{ needs.verify-release-candidate.outputs.image_repository }}" in workflow
    )
    assert "subject-digest: ${{ steps.oci-image.outputs.digest }}" in workflow
    assert "push-to-registry: true" in workflow
    assert "--verify-tag" in workflow
    assert "build-controlled-release:" in workflow
    assert "needs: [verify-release-candidate, build-controlled-release]" in workflow
    assert "permissions:\n      contents: read" in workflow
    assert "scripts/repository_guard.py --history" in workflow
    assert "scripts/repository_guard.py --history" in security_workflow
    assert "scripts/repository_guard.py --history" in ci_workflow
    assert "STOCK_PROFILER_AUTH_ORIGIN" in compose
    assert "STOCK_PROFILER_AUTH_RP_ID" in compose
    assert "STOCK_PROFILER_API_SHARED_SECRET_FILE" in compose
    assert "STOCK_PROFILER_AUTH_BOOTSTRAP_TOKEN_FILE" in compose
    assert "STOCK_PROFILER_AUTH_RECOVERY_TOKEN_FILE" in compose
    assert '"process-health", "scheduler"' in compose
    assert '"process-health", "worker"' in compose
    assert "stock-profiler-gateway:${STOCK_PROFILER_SOURCE_SHA" in compose
    assert "healthcheck:" in compose
    assert "SOURCE_SHA: ${STOCK_PROFILER_SOURCE_SHA" in compose
    assert "x-api-secrets:" in compose
    assert "secrets: *api-secrets" in compose
    assert "STOCK_PROFILER_PROCESS_ROLE: api" in compose
    assert "STOCK_PROFILER_PROCESS_ROLE: scheduler" in compose
    assert "STOCK_PROFILER_PROCESS_ROLE: worker" in compose
    assert "STOCK_PROFILER_PROCESS_ROLE: migrate" in compose
    assert "respond /healthz 200" in caddyfile
    assert "org.opencontainers.image.revision=${SOURCE_SHA}" in gateway_dockerfile
    assert "STOCK_PROFILER_API_SHARED_SECRET_FILE" in ci_workflow
    assert "STOCK_PROFILER_AUTH_ORIGIN" in ci_workflow
    assert "SOURCE_DATE_EPOCH" in readme
    assert "STOCK_PROFILER_AUTH_ORIGIN" in readme
    assert "fails closed" in baseline
    assert "Documentation correction" in documentation_issue
    assert "original synthetic" in documentation_issue


def test_web_authn_dependency_contract_is_declared_without_an_auth_route() -> None:
    package_json = (ROOT / "web" / "package.json").read_text(encoding="utf-8")
    openapi = (ROOT / "web" / "openapi.json").read_text(encoding="utf-8")
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")

    assert '"@simplewebauthn/browser"' in package_json
    for dependency in (
        "@hookform/resolvers",
        "@radix-ui/react-slot",
        "@tanstack/react-query",
        "@tailwindcss/vite",
        "lucide-react",
        "react-hook-form",
        "react-router",
        "tailwindcss",
        "zod",
    ):
        assert f'"{dependency}"' in package_json
        assert dependency in notices
    assert "/api/v1/auth" not in openapi
    assert "/api/v1/diagnostics/safety-capabilities" in openapi
