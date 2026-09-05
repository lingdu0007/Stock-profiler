# Development Baseline

Use Python 3.11 and uv for the backend. Use Node 24 with Corepack and pnpm for
the Web client.

```bash
make setup
make verify
```

`make api-client-check` proves that `web/openapi.json` and the generated
frontend type declaration match the FastAPI OpenAPI document. `make artifacts`
creates local wheel, sdist, Web archive, checksum, version-bundle, and SPDX
SBOM inputs. Local artifact output is ignored by Git.

## Production Contract

Compose uses the same backend image for API, scheduler, worker, and migration.
Before rendering it, set a complete Git SHA, its commit epoch,
`STOCK_PROFILER_AUTH_ORIGIN` to a complete HTTPS origin, and
`STOCK_PROFILER_AUTH_RP_ID` to the matching relying-party ID. Set
`STOCK_PROFILER_GATEWAY_HOSTNAME` to that stable private hostname and
`STOCK_PROFILER_GATEWAY_BIND_ADDRESS` to its private VPN address. Production
secrets, including the gateway TLS certificate and private key, are
deployment-local files mounted at `/run/secrets`; set the corresponding
`STOCK_PROFILER_*_FILE` variables to absolute paths outside the repository.
Only the API mounts authentication secrets; scheduler, worker, and migration
processes receive no authentication secret files. Secret environment variables
are rejected. The tracked `deploy/secrets/*.example` files remain exact
synthetic placeholders for public CI interpolation only.

Startup fails closed when configuration is incomplete, synthetic, malformed,
version-incompatible, or gives the application and M-Agent the same SQLite
file. The API readiness endpoint checks the migrated application database.
`stock-profiler doctor` reports API, scheduler, and worker heartbeat status
and exits nonzero when any required long-running process is missing or stale.
Compose health checks use `stock-profiler process-health` for scheduler and
worker without scheduling business work.
