# Frozen scoped coverage envelope

`candidate-allocation.2.0.0` adds an explicit, versioned synthetic coverage
command to the saved allocation journey. The earlier allocation contract keeps
its original shape. The new contract requires the `beta` input, and the frozen
case input must match its canonical serialization. There is no environment
switch, production activation endpoint, broker connection or order authority.
Every result remains `D0_SYNTHETIC_CONTRACT_ONLY` and `actionable: false`.

The command supplies its policy, complete portfolio scope, permission identity,
and exact retained qualification bindings. B0, B1, T0–T4 and G1–G5 must all
resolve to current valid scoped ledger records. Missing, ambiguous, suspended,
restricted, stale, terminated or unavailable evidence fails closed. Evidence
must match its qualification scope and version and be available by the frozen
cutoff. Current expiry and state are checked again immediately before commit.
The statistical-action binding uses the policy's explicit holding-age domain,
probability grid, source, target and purpose; another statistical profile cannot
stand in for it. Changing policy content under one version fails closed, and a
new policy version requires a new G5 qualification binding.
A qualification decision affects permission; it cannot change an original
candidate, probability, standard evaluation or record of a user declining to
trade.

The ordinary constrained allocation is solved first to disclose its feasible
remaining capacity. The envelope then adds one portfolio constraint before
progressive equalization, legal rounding and exact capacity verification.
With policy ratio `r`, current portfolio net liquidation equity `E`, and
retained coverage `A`, new capacity is `min(max(r × E − A, 0), C)`, where `C`
is the ordinary feasible remaining capacity after its existing commitments.
Retained coverage includes saved reservations, conservatively unmatched
commitments and acquired exposure traced from broker reconciliation to current
holdings. A partial fill converts the claim into acquired exposure. Later
batches or a different permission label cannot reset either part. Proven
terminal release does not grow another accepted row. Normal issuer, account,
cash, cost, stress, turnover and correlation constraints continue to apply.
The report displays normal feasible capacity, retained coverage, available
coverage and planned principal separately.

Expansion is an explicit new request. Its fictional observation input binds
the same portfolio, policy, permission and qualification identities. It models
an independently registered observation window, real-time provenance, cutoff
availability, saved monthly calendar closes, a consecutive position-day window,
closed plans, applicable broker reconciliation and explicit forward effect.
The `simulated_origin` label distinguishes fictional real-time eligibility
from replay, historical and PoC examples; none of these synthetic records
establish actual production evidence or permit real activation. Qualification
must already support the observation period. Insufficient observation leaves
the initial envelope and preserves the specific reason.

Operational audit keeps every elapsed planned month. Missing and duplicated
months cannot shorten the window. Batch, timeliness, applicable plan,
notification, report, confirmation and reconciliation obligations have distinct
denominators. A zero denominator has a null rate and `NOT_APPLICABLE`, rather
than a perfect score. Consecutive core failures and any safety failure close
new capacity. A later case cannot omit or relabel a saved failed obligation
within the supervised window. Original reports remain immutable. The policy
supplies completion and window parameters; safety failures have no percentage
allowance. Users do not need to accept a plan or manufacture a fill. A plan
with no accepted intent needs only its applicable reconciliation, while
expansion still requires the separate system-position observation window.

Confirmation and step review retain the saved envelope binding and replay it
with the rest of the plan. Changed policy, scope, qualification, quantities or
capacity requires a new plan. Confirmation and all reservations remain one
atomic ledger event. Withdrawal and broker reconciliation can still discharge
existing obligations after new permission is suspended. The authenticated
workspace rechecks current qualification for new actions while exposing the
same saved report and its original evidence; the Web component displays those
host results and performs no policy calculation.

Original synthetic integration cases cover accepted and rejected qualification,
initial and expanded envelopes, operational and calendar boundaries,
non-trading denominators, full/partial/zero allocation, confirmation faults,
concurrency, withdrawal, partial fills, unknown orders and retained capacity.
The existing delivery, identity, framework and order-boundary suites continue
to exercise the same saved-case host journey.
