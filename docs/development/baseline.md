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

The snapshot retains a content-addressed strategy and parameter artifact,
training members and label-availability watermark, diagnostics/environment
hashes, and complete signal observations. Strategy parameters freeze
signal semantics, industry/universe percentile populations, conservative
inapplicable states, curve knots and directions, low-rank interactions,
training-window/weighting rules, regularization, targets and selection-policy
hash. The host replays piecewise-linear additive curves and declared
interaction components. It rejects a claimed head score that does not match
that replay and retains raw values, states, transformed values and both
heads' contributions for the entire universe. Context signals may enter
only through declared interactions, never an independent main effect.
The training calendar must cover every month from the frozen start through
the potentially mature boundary. Calendar-month maturity is computed from
each original selection cutoff and the declared horizon; the artifact must
use the full expanding or trailing mature window, and no label may precede
its own maturity. Industry, adjustment, universe-policy, manifest and
per-field semantic contracts are checked against the content-addressed
strategy before inference.

Market fields and each signal have separate evidence-manifest entries.
Derived inputs use certified-delivery evidence; market evidence and any
authoritative substitute reconstruction must cover the completed close.
Missing inputs are data failures; unavailable or inconsistent model
artifacts are system failures. No fallback estimator is selected.

The policy supplies the required member count, industry and capitalization
limits, adjusted-return window length and inclusive positive-correlation
ceiling, without personal defaults. Equal head values share an average rank
on the zero-to-one-hundred endpoint scale; a singleton receives zero.
Composite comparisons use exact fractions, rounding only the displayed
values; ties use ascending security identity. Capitalization groups are
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
complete ranking, signal contributions, gate-scan reasons and population registration. It adds no
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

The `candidate-release.1.0.0` frozen-case contract accepts one exact committed
research event with ten independently risk-vetted members. The host fits the
versioned monotone Firth logistic calibrator from an initial window of at least
60 consecutive mature months, using the shortest consecutive suffix that
contains at least 500 records and 50 examples from each class. Its population
is the complete union of mature raw-score source rows, previously committed
calibration rows, and previously frozen candidate predictions whose six-month
outcomes have matured. Each eventual label row must retain its original frozen
candidate score, probability and five-session entry window; omitted or rewritten
outcomes fail closed, and an executable entry must fall within an actual saved
market session in that window. Members with no frozen probability remain visible
as availability failures and do not enter later calibration cohorts.
Rolling-window completeness uses all source labels mature and available by the
frozen knowledge cutoff, regardless of caller-supplied label or publication clocks.
After the first valid
calibration, every refit uses the fixed rolling latest 60 consecutive mature
months; a missed sample floor then produces a visible calibration failure.
The label watermark cannot exceed the research knowledge cutoff. It freezes
fitted parameters, historically frozen out-of-sample diagnostics,
per-security probabilities and the five-session window selected from the saved
market calendar. A candidate requires probability at least 0.80, complete
data, an accepted independent risk result and a scope-, version-, state- and
calendar-matched persisted qualification valid at the cutoff. Missing qualification yields
`RECOMMENDATION_ABSTAINED`; a valid batch with no passing member remains
`VALID_NO_CANDIDATES`. Calibration or data failures remain visible and do not
reuse older probabilities. A report published after the frozen window fails
without moving it. A post-commit diagnostic alert that only moves the matching
qualification from `VALID` to `AT_RISK` does not invalidate the already frozen
publication; revocation, suspension, expiry, or scope/version/calendar changes
still fail their publication checks. The authenticated report projection shows the original
research thesis, principal risks, evidence freshness, market qualification,
calibration and fixed validity dates. These synthetic D0 cases remain
non-actionable and expose no personal position information.

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
