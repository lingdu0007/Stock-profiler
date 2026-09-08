# Deterministic protection contracts

The offline contract matrix exercises the frozen decision-case interface with
original synthetic inputs. It is engineering evidence only: it does not grant
qualification, advance a data stage, activate a version, authorize statistical
actions, or prove investment performance or operational reliability.

## Execution

Start from a clean, committed source tree with the locked development environment:

```sh
uv sync --locked --extra dev --python 3.11
uv run --locked --python 3.11 python scripts/protection_matrix.py --catalog
make protection-contracts CONTRACT_OUTPUT=/tmp/protection-contract-result.json
```

The output must be a new file outside the source repository. Keep generated
results outside Git; do not commit test logs, databases, or execution artifacts.
An existing result is never overwritten. A failed run either leaves no artifact
(preflight failure) or writes a `FAILED` artifact. Only a zero exit status and
`PASSED` verdict together represent a completed run.

The runner binds the artifact to the full source commit, Git archive digest and
dependency lock digest. Two fresh source archives run the complete catalog in
separate processes and temporary stores. Missing coverage, duplicate identities,
skips, unexpected failures, collection errors and mismatched repeated inventories
all fail the run. The journey cases additionally compare complete saved report
projections across two independently reconstructed application/framework stores,
including replay, monitoring, independent user facts, notifications and audit.

## Coverage

The catalog includes the complete integration suites for:

| Surface | Required behaviors |
| --- | --- |
| Account scope | Cash-account admission, unknown or unsupported types, incomplete selection, authorization expiry |
| Position facts | Reconciliation, conflicting quantities, sellability, incomplete or corrected evidence |
| Concentration | Target/hard boundaries, retained obligations, fills, unresolved execution |
| Stress | Normal/buffer/hard states, exact thresholds, residual restoration, evidence failure |
| Liquidity | Cash bands, dated obligations, funding capacity, settlement, retained restoration |
| Drawdown | Escalation, exact boundaries, consecutive-close recovery, epoch retention, expiry |
| Execution | Strict conjunction, waterfall credit, account costs, legal lots, full-sale rounding, infeasibility |
| Monitoring | P0/P1, quiet windows, fallback, lifecycle, user facts, report history and corrections |
| Publication | Durable commit, uncertain acknowledgement, replay, saved projection, correction recovery |
| Isolation | User/account grants, shadow content, unregistered orders, append-only audit |
| Governance | Scoped engineering evidence and independent qualification/activation gates |
| Journey | Complete saved results rebuilt in independent stores, without assuming execution |

Passing counts are a coverage check, not a substitute for the assertions within
these contracts. The catalog is versioned source, not a claim about real accounts
or a statistical measurement.

## Gate-removal self-checks

After both baselines pass, the runner mutates actual source guards in separate
temporary archives. Each mutation must match exactly one AST condition in its
named function; a changed or missing guard makes the run fail closed. The mutated
test inventory must match the corresponding passing baseline inventory.

Removing concentration, stress, cash restoration, capital preservation, user
scope, account scope or notification fallback must cause the designated contract
assertion to fail. A surviving mutation is a failed artifact. A syntax error,
fixture error, missing import, skipped test or unrelated failure does not count
as detecting a removed protection. The original source and installed framework
are never modified by these checks.

These are D0 synthetic contract checks that can support a separately controlled
D1 engineering assessment. They do not establish D2/D3/D4 investment or
operational qualification. In particular, synthetic notification attempts do not
prove delivery, user declarations do not prove execution, and a generated plan
does not prove risk restoration.
