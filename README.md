# Stock Profiler

Stock Profiler is an Apache-2.0 licensed engineering baseline for a personal,
single-user investment decision-support host application. This repository
contains no market data, account data, credentials, personal policy values, or
order-writing capability.

The current `0.1.0.dev0` baseline provides:

- a Python 3.11 host with a pinned M-Agent `0.5.1` release wheel;
- one frozen, original D0 synthetic decision case with immutable clocks,
  version bundle, independent business/Run/event/report identities, durable
  Run recovery, append-only stage outcomes, and deterministic replay;
- a CLI acceptance path and authenticated, read-only FastAPI report projection;
- startup-frozen configuration, privacy-safe structured logging, health
  diagnostics, and separate SQLite ownership for application state and
  M-Agent run state;
- a React/PWA report viewer generated from the OpenAPI contract, with no
  runtime API cache;
- reproducible dependency locks, container build inputs, and public synthetic
  verification only.

It does not implement real data access, investment conclusions, real
qualification, provider or broker integrations, external notification delivery,
order writing, or account actions.

## Quick Start

```bash
make setup
make verify
make dev
```

The CLI calls host application modules directly and makes no HTTP request:

```bash
uv run stock-profiler version
uv run stock-profiler doctor
uv run stock-profiler decision-case-run
uv run stock-profiler decision-case-replay \
  --business-identity synthetic:decision:orbital-mosaic:001
uv run stock-profiler decision-case-correct \
  --business-identity synthetic:decision:orbital-mosaic:001
```

The D0 case is explicitly synthetic and cannot be read as a recommendation,
historical result, qualification, or order path. The report endpoint is
`GET /api/v1/reports/{report_version_id}` and requires a valid Passkey session.
Authentication alone grants no account access. Configure the host-local JSON
array `STOCK_PROFILER_REPORT_ACCOUNT_IDS` outside Git; its default is empty.
For the fictional D0 fixture only, the account grant is
`["synthetic-account-4017"]`. `STOCK_PROFILER_REPORT_PERMISSIONS` defaults to
`["REPORT_READ"]`; appending synthetic user facts additionally requires
`USER_FACT`. Request headers, model responses, and browser sessions cannot
expand these startup-frozen grants.
Passkey registration has no public browser entry: it begins from a host-console
bootstrap or recovery grant that expires after ten minutes. A directly operated
host console creates those grants without an HTTP endpoint:

```bash
docker compose -f deploy/compose.yml run --rm api \
  stock-profiler host-console-grant --purpose bootstrap
```

Open the emitted `enrollment_path` at the configured private HTTPS origin
before the grant expires. The opaque grant is carried only in the URL fragment,
so it is not included in the browser's HTTP request.

## Controlled Compose

The Compose topology has one Caddy gateway plus API, scheduler, worker, and
migration processes. It requires an immutable source identity, a deterministic
build epoch, and these non-secret production values:

```bash
export STOCK_PROFILER_SOURCE_SHA="$(git rev-parse HEAD)"
export SOURCE_DATE_EPOCH="$(git log -1 --format=%ct)"
export STOCK_PROFILER_AUTH_ORIGIN="https://<private-hostname>"
export STOCK_PROFILER_AUTH_RP_ID="<private-hostname>"
export STOCK_PROFILER_GATEWAY_HOSTNAME="<private-hostname>"
export STOCK_PROFILER_GATEWAY_BIND_ADDRESS="<private-vpn-address>"
export STOCK_PROFILER_API_SHARED_SECRET_FILE="/absolute/path/outside/repository/api_shared_secret"
export STOCK_PROFILER_AUTH_BOOTSTRAP_TOKEN_FILE="/absolute/path/outside/repository/auth_bootstrap_token"
export STOCK_PROFILER_AUTH_RECOVERY_TOKEN_FILE="/absolute/path/outside/repository/auth_recovery_token"
export STOCK_PROFILER_GATEWAY_TLS_CERTIFICATE_FILE="/absolute/path/outside/repository/gateway_tls_certificate"
export STOCK_PROFILER_GATEWAY_TLS_PRIVATE_KEY_FILE="/absolute/path/outside/repository/gateway_tls_private_key"
```

Replace every angle-bracket and absolute-path placeholder with deployment-local
values outside Git. Sensitive values are read only from `/run/secrets`; they
are not accepted as environment variables. The tracked files under
`deploy/secrets/` are immutable synthetic templates used only for public CI
Compose rendering and must never be used for a production deployment.
The gateway accepts only loopback, RFC1918, or CGNAT IPv4 bind addresses,
terminates TLS for the stable private hostname with a certificate and key
provisioned outside Git, and proxies the same HTTPS origin to `/api/v1`. It
fails before serving traffic unless the certificate is currently valid, matches
the configured hostname, and matches the mounted private key.

The API accepts only the configured Origin for authentication-state changes.
Its opaque `__Host-stock_profiler_session` cookie is `Secure`, `HttpOnly`, and
`SameSite=Lax`; sessions use a five-minute challenge, seven-day idle expiry,
thirty-day absolute expiry, and ten-minute recent-reauthentication contract.
Private authentication and report responses send `Cache-Control: no-store`.

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

The single-user, non-public-recommendation, and no-order-writing constraints
are official-project safety invariants. They define the maintained project's
scope and do not add restrictions to Apache-2.0 forks; a fork is not an
officially maintained or endorsed deployment.

The credential-free isolation contract and its precise trusted-host boundary
are documented in [`Read-only isolation`](docs/architecture/read-only-isolation.md).
