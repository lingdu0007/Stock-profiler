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

## Frozen Case Acceptance

`stock-profiler decision-case-run --case /absolute/path/synthetic-case.json`
accepts one complete versioned synthetic frozen case through the same host
acceptance seam as the packaged demonstration case. The local input must pass
the frozen contract and exact host-provenance checks for the executing
application version and source SHA before it can create a Run or report; the
CLI forwards its parsed JSON object unchanged to those host checks, and invalid
input is rejected and audited. `--case` is available only for
`decision-case-run`. These synthetic case files are local runtime inputs, not
files to add to Git.

## Frozen Recovery

The `selection.1.0.0` frozen-case contract consumes an original committed
monthly universe and an evidence-bound dual-head screening snapshot. The
host, not the framework, computes complete cross-sectional midrank
percentiles, their explicitly weighted composite, and the diversification
scan. Scores are uncalibrated research inputs, never success probabilities.
This D0 interface does not fit statistical models or certify their outputs.

The policy supplies the required member count, industry and capitalization
limits, adjusted-return window length and inclusive positive-correlation
ceiling, without personal defaults. Equal head values share an average rank
on the zero-to-one-hundred endpoint scale; a singleton receives zero.
Composite ties use ascending security identity. Capitalization groups are
the three contiguous rank partitions of the entire eligible universe,
ordered by float capitalization and then security identity; group index is
`floor(3 * zero_based_rank / universe_size)`. The complete ranking survives
even when only its prefix is scanned. Conflicts retain higher-ranked
members; the scan does not backtrack or relax constraints.

Insufficient capacity produces `ABSTAINED` with no frozen members. The
scan's provisional inclusions remain audit facts, not a partial cohort.
Abstention remains a registered valid month and a coverage/pass denominator
member; it is not a zero-drawdown success. Incomplete universe membership,
missing or stale inputs, invalid or constant adjusted-return series and
unavailable evidence produce a separate data failure. System prerequisites
retain their own failed outcome. All planned attempts retain their stable
month identity; changed retry keys, policies or later evidence cannot
replace the original snapshot, including after a failure.

The existing authenticated report route shows ordered frozen members,
complete ranking, gate-scan reasons and population registration. It adds no
trading, allocation or personal authorization capability.

The `universe.1.0.0` frozen-case contract accepts an explicit synthetic monthly
universe command. Its policy, calendar, account scope, security inventory,
dated market window, legal acquisition cost and source manifest are input
evidence, not environment defaults. The host computes membership and exclusion
reasons; framework output cannot supply them. Required evidence failures remain
visible as a whole-month data failure.

Evidence retains original content and its hash, independent availability
clocks, license purpose and validity, and any preregistered substitution proof.
Extended-board permissions use saved, cutoff-visible scoped qualifications.
These are D0 contract checks, not provider certification or live authorization.
All universe reports remain non-actionable, including simulated real-candidate
purpose cases. The first committed scope/purpose/month snapshot is immutable;
changed retry identities or later evidence do not replace it.

`stock-profiler decision-case-replay --business-identity <identity>` reuses the
saved business mapping and frozen snapshot. For an older unmapped Run whose
build provenance differs, add `--recovery-case /absolute/path/original-case.json`
with its original complete frozen case snapshot. The adapter validates the
derived original Run ID, frozen input, definition snapshot, and supported
version bundle before any host write. Missing or inconsistent proof fails
closed without creating a replacement Run. Recovery snapshots are local
runtime inputs, not files to add to Git.

`stock-profiler decision-case-correct --business-identity <identity>` appends
the packaged D0 correction evidence to a published original. Repeated calls
return the same correction event and report. Its evidence cutoff belongs to
the correction only; it does not extend or rewrite the original cutoff.

## Production Contract

Compose uses the same backend image for API, scheduler, worker, and migration.
Before rendering it, set a complete Git SHA, its commit epoch,
`STOCK_PROFILER_AUTH_ORIGIN` to a complete HTTPS origin, and
`STOCK_PROFILER_AUTH_RP_ID` to the matching relying-party ID. Set
`STOCK_PROFILER_GATEWAY_HOSTNAME` to that stable private hostname and
`STOCK_PROFILER_GATEWAY_BIND_ADDRESS` to its private IPv4 VPN address.
The gateway permits only loopback, RFC1918, and CGNAT bind ranges and verifies
the mounted certificate's validity, hostname, and key pair before serving.
Production
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
