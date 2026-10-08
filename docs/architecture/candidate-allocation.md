# Saved candidate allocation

The `candidate-allocation.1.0.0` frozen decision-case capability consumes a
published candidate event and saved concentration, stress, liquidity and capital
protection reports. It uses the existing CLI/bootstrap, event ledger and scoped
report API. The host computes the allocation after successful business
validation and before committing the result. The report renderer displays this
saved result without recalculation.

Allocation is a separate result from candidate qualification. Every candidate
row retains the complete original calibrated member, including its research
identity, probability, thesis and entry window. Allocation cannot rewrite the
source report or its standard evaluation registrations. Missing or inaccessible
source reports fail closed without disclosing their members.
Before new allocation, shared retained-qualification checks bind the original
scope and version, trace current qualification history, and reject expiry or
withdrawal. Restoration cannot revive an invalidated release. A correction to
the candidate conclusion or its source research blocks current use. The result
retains the invalidating evidence identities and unchanged original members.

## Frozen inputs and gates

The command binds the candidate event, formation cutoff, risk-report identities,
explicit synthetic allocation policy, market-capacity facts, return matrix,
existing buy commitments and account-local purchase routes. The saved plan
additionally retains the candidate conclusion version, reconciled complete
portfolio snapshot and effective risk budget. Costs, confirmed price caps,
quantity rules and their evidence remain attached to each legal purchase leg.
Policy thresholds have no engine defaults.
Turnover facts explicitly identify the twenty-session median. The return matrix
declares daily adjusted returns and its immutable market-calendar version; its
dates must equal the latest complete trading-session window at the cutoff.

New exposure requires a successful business prerequisite, current complete
protection handoffs, the current effective authorization, normal capital state,
known stress capacity and no pending deterministic reduction. A newer protection
handoff blocks use of a superseded report. Current broker-final trading cash
already excludes unfinished-buy reservations; those reservations are not
deducted again. Broker-linked commitments must prove that the reservation covers their frozen
principal and full purchase cost; an inconsistent reservation blocks allocation.
Accepted commitments outside the broker snapshot deduct their
principal and full purchase cost. Every commitment also consumes issuer,
turnover, neighborhood and stress capacity.

Held and committed exposure reduces each issuer's entry target. Codes sharing
an issuer consume one shared gap. Account cash, full purchase costs, issuer
concentration, gross stress including disposal friction, turnover and every
direct positive-correlation neighborhood constrain that gap simultaneously.
Neighborhoods include the issuer itself and overlap independently; they do not
use transitive graph closure. Low or negative correlation supplies no additional
capacity. Unknown current evidence closes the affected capacity.
Each candidate's own direct neighborhood governs its allocation. An unrelated
existing neighborhood above its buying cap creates no portfolio-wide sell or
purchase gate. Missing history for an uncommitted peer closes that peer without
closing candidates whose own and committed-exposure history is complete.
Commitment issuer identities must agree with held and supplied security facts
and with every other commitment for that security. Contradictions block the
allocation rather than moving exposure into another issuer's target.

## Allocation and verification

Continuous allocation progressively equalizes final issuer exposure, freezing
issuers when their caps bind. Codes within one issuer divide its fixed allocation
without score or probability weights. The numerical proposal is truncated down
to six decimal places and checked against the original Decimal cash, cost,
stress and group-capacity inputs before publication.

One joint integer allocation then maximizes positive candidate coverage, the
ascending vector of completion ratios, and total principal, in that order.
Only complete ties use the ascending covered-probability vector. Purchase costs,
order count and stable security/account identities resolve remaining ties.
Each positive account quantity is its minimum buy plus an integer number of
increments. A candidate may exceed its continuous allocation by at most one
minimum legal unit of its selected account route, while total principal stays within the continuous total and
all original capacities. Exact Decimal validation checks quantities and every
monetary capacity again. There is no iterative leftover-cash purchase sweep.

The pinned SciPy/HiGHS optimizer must return an optimal result. Each phase is
bounded by 120 seconds, 512 solves, 2,000 variables and 5,000 constraints; each
individual solve has a 15-second limit and a 100,000-node limit. Invalid numerical
inputs, exhausted budgets, ambiguous progressive equality or failed exact
verification yield a blocked allocation with the original candidates retained.
They never publish an unverified feasible incumbent as the optimum.

Missing confirmation of an otherwise evidenced price cap permits only a
continuous `AWAITING_PRICE_CAP` result. It produces no purchase quantities.
Planned rows distinguish full, partial and zero allocation and retain structured
reasons. Legal completion uses the minimum unit of the selected, funded routes;
an unusable small-unit route cannot turn a complete allocation into a partial one.
The purchase sequence orders positive rows by entry-window expiry,
prior issuer exposure, prior neighborhood exposure, descending probability,
ascending purchase-cost rate and security identity.
Each row saves all applicable gate reasons and a primary reason selected by
fixed precedence: global protection and cash/stress, issuer and market capacity,
execution evidence and minimum units, discrete priority, then entry expiry.
Excluded account routes retain their complete frozen facts and all failure
reasons. A positive continuous allocation that cannot fit any legal minimum unit
uses `BELOW_MINIMUM_BUY_UNIT`. A legal but uncovered candidate uses
`UNALLOCATED_CAPACITY_PRIORITY` with every lexicographic comparison against the
best plan that requires its coverage. These counterfactuals are audit queries
over the same capacities, share the integer phase's solve budget and never apply
another allocation or replenish residual cash. The report records exact selected
objective vectors, alternative vectors, and the first losing comparison; later
layers are explicitly not reached.
Saved capacity checks identify each gate's affected securities and account,
committed margin, available buying capacity, and remaining continuous and planned
capacity. A pre-existing breach is retained as a negative committed margin with
zero buying capacity for its affected neighborhood.

The capability is a synthetic report-only contract. It does not confirm a plan,
create reservations, submit broker orders, reconcile future fills or activate
personal use. `actionable` is always false.
