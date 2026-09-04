# Stock Profiler

Stock Profiler is an Apache-2.0 licensed engineering baseline for a personal,
single-user investment decision-support host application. This repository
contains no market data, account data, credentials, policy values, or
order-writing capability.

The current `0.1.0.dev0` baseline provides:

- a Python 3.11 host with a pinned M-Agent `0.5.0` release wheel;
- a CLI and a FastAPI `/api/v1/diagnostics/version` contract that expose one
  version bundle;
- startup-frozen configuration, privacy-safe structured logging, health
  diagnostics, and separate SQLite ownership for application state and
  M-Agent run state;
- a React/PWA shell generated from the OpenAPI contract;
- reproducible dependency locks, container build inputs, and public synthetic
  verification only.

It does not implement investment conclusions, durable agent runs, account
access, authentication flows, market or broker integrations, reports,
notifications, or orders.

## Quick Start

```bash
make setup
make verify
make dev
```

The CLI has no network business behavior:

```bash
uv run stock-profiler version
uv run stock-profiler doctor
```

The development API serves the diagnostic contract at
`http://127.0.0.1:8000/api/v1/diagnostics/version`.

## Controlled Compose

The Compose topology has one Caddy gateway plus API, scheduler, worker, and
migration processes. It requires an immutable source identity, a deterministic
build epoch, and these non-secret production values:

```bash
export STOCK_PROFILER_SOURCE_SHA="$(git rev-parse HEAD)"
export SOURCE_DATE_EPOCH="$(git log -1 --format=%ct)"
export STOCK_PROFILER_AUTH_ORIGIN="https://<private-hostname>"
export STOCK_PROFILER_AUTH_RP_ID="<private-hostname>"
export STOCK_PROFILER_API_SHARED_SECRET_FILE="/absolute/path/outside/repository/api_shared_secret"
export STOCK_PROFILER_AUTH_BOOTSTRAP_TOKEN_FILE="/absolute/path/outside/repository/auth_bootstrap_token"
export STOCK_PROFILER_AUTH_RECOVERY_TOKEN_FILE="/absolute/path/outside/repository/auth_recovery_token"
```

Replace every angle-bracket and absolute-path placeholder with deployment-local
values outside Git. Sensitive values are read only from `/run/secrets`; they
are not accepted as environment variables. The tracked files under
`deploy/secrets/` are immutable synthetic templates used only for public CI
Compose rendering and must never be used for a production deployment. The
current baseline declares authentication configuration but does not provide an
authentication route.

```bash
make compose-config
docker compose -f deploy/compose.yml up --build
```

Production startup fails closed when the source SHA, SQLite paths, HTTPS
origin/RP-ID relationship, or required secret files are missing, malformed,
synthetic, or incompatible. `GET /livez` reports process liveness; `GET
/readyz` also verifies the migrated application store and records the API
heartbeat. `stock-profiler doctor` exits nonzero if the API, scheduler, or
worker heartbeat is missing or stale.

## Safety Boundary

Only original synthetic fixtures may be committed. Never add credentials,
runtime databases, logs, backups, account information, market data, provider
responses, or transformed copies of those materials. See
[`SECURITY.md`](SECURITY.md) and [`CONTRIBUTING.md`](CONTRIBUTING.md).
