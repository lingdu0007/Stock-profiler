SHELL := /usr/bin/env bash

PNPM := corepack pnpm@10.17.1 --dir web
GITLEAKS_VERSION := 8.18.4
GITLEAKS_ARCHIVE_SHA256 := ba6dbb656933921c775ee5a2d1c13a91046e7952e9d919f9bac4cec61d628e7d
GITLEAKS_HOME := .tools/gitleaks-$(GITLEAKS_VERSION)
GITLEAKS := $(GITLEAKS_HOME)/gitleaks
ARTIFACT_VERSION := $(shell uv run python -c 'from stock_profiler.foundation.versioning import APPLICATION_VERSION; print(APPLICATION_VERSION)')
SOURCE_SHA ?= $(shell git rev-parse --verify HEAD 2>/dev/null || printf '%040d' 0)
SOURCE_DATE_EPOCH ?= $(shell git log -1 --format=%ct 2>/dev/null || printf '0')
export STOCK_PROFILER_SOURCE_SHA := $(SOURCE_SHA)
export SOURCE_DATE_EPOCH

.PHONY: setup format lint type-check test build artifacts repo-guard secrets-scan \
	license-check api-client-check dev dev-web compose-config verify protection-contracts

setup:
	uv sync --locked --extra dev --python 3.11
	uv run pre-commit install --install-hooks
	corepack enable
	$(PNPM) install --frozen-lockfile
	$(PNPM) exec playwright install chromium

format:
	uv run ruff format src tests scripts migrations
	$(PNPM) exec prettier --write .

lint:
	uv run ruff check src tests scripts migrations
	$(PNPM) lint
	$(PNPM) format

type-check:
	uv run mypy src tests
	$(PNPM) type-check

test:
	uv run pytest
	uv run coverage report --include='*/candidate_selection/*' --fail-under=100
	$(PNPM) test
	$(PNPM) test:e2e

build:
	uv build --out-dir dist/python
	uv run python scripts/normalize_sdist.py dist/python/stock_profiler-$(ARTIFACT_VERSION).tar.gz
	$(PNPM) build

artifacts: build
	mkdir -p dist
	tar -C web/dist --sort=name --mtime=@$(SOURCE_DATE_EPOCH) --owner=0 --group=0 --numeric-owner \
		--use-compress-program="gzip -n" -cf dist/stock-profiler-web-$(ARTIFACT_VERSION).tar.gz .
	uv run python scripts/write_version_bundle.py dist/version-bundle.json
	uv run python scripts/generate_spdx_sbom.py dist/stock-profiler.spdx.json
	cd dist && sha256sum python/*.whl python/*.tar.gz stock-profiler-web-$(ARTIFACT_VERSION).tar.gz version-bundle.json stock-profiler.spdx.json > SHA256SUMS

repo-guard:
	uv run python scripts/repository_guard.py

$(GITLEAKS):
	mkdir -p $(GITLEAKS_HOME)
	curl --fail --location --silent --show-error \
		--output $(GITLEAKS_HOME)/gitleaks.tar.gz \
		https://github.com/gitleaks/gitleaks/releases/download/v$(GITLEAKS_VERSION)/gitleaks_$(GITLEAKS_VERSION)_linux_x64.tar.gz
	echo "$(GITLEAKS_ARCHIVE_SHA256)  $(GITLEAKS_HOME)/gitleaks.tar.gz" | sha256sum --check
	tar --extract --gzip --file $(GITLEAKS_HOME)/gitleaks.tar.gz --directory $(GITLEAKS_HOME) gitleaks
	chmod +x $(GITLEAKS)

secrets-scan: $(GITLEAKS)
	@$(GITLEAKS) version | grep -F "$(GITLEAKS_VERSION)"
	$(GITLEAKS) protect --staged --redact --no-banner

license-check:
	uv run python scripts/check_license.py

api-client-check:
	@temporary_directory="$$(mktemp -d)"; \
	uv run python scripts/export_openapi.py "$$temporary_directory/openapi.json"; \
	$(PNPM) exec openapi-typescript "$$temporary_directory/openapi.json" -o "$$temporary_directory/schema.d.ts"; \
	cmp "$$temporary_directory/openapi.json" web/openapi.json; \
	cmp "$$temporary_directory/schema.d.ts" web/src/api/schema.d.ts

dev:
	STOCK_PROFILER_ENVIRONMENT=development STOCK_PROFILER_PROCESS_ROLE=migrate uv run alembic upgrade head
	STOCK_PROFILER_ENVIRONMENT=development STOCK_PROFILER_PROCESS_ROLE=api uv run uvicorn stock_profiler.entrypoints.http.app:app --reload

dev-web:
	$(PNPM) dev

compose-config:
	$(MAKE) build
	STOCK_PROFILER_SOURCE_SHA=$(SOURCE_SHA) SOURCE_DATE_EPOCH=$(SOURCE_DATE_EPOCH) docker compose -f deploy/compose.yml config

verify: repo-guard secrets-scan license-check api-client-check lint type-check test build

protection-contracts:
	uv run --locked --python 3.11 python scripts/protection_matrix.py \
		--source "$(SOURCE_SHA)" --output "$(CONTRACT_OUTPUT)"
