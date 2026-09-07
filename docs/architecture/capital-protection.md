# Synthetic capital protection

The `drawdown.1.0.0` frozen case, host and report contracts add a host-owned
capital-protection command to the existing D0 decision-case runner. They do not
add an HTTP mutation route, an order path or a live data adapter. M-Agent still
owns its Run; the host owns capital facts and the append-only formal report.
The generated HTTP client and web view read the committed result without
recalculating it.

## Inputs and accounting

`DrawdownCommand` names an approved portfolio authorization, an epoch, the
preceding committed capital decision and a reconciled position event. Evidence
must cover the same complete account scope and the command's frozen cutoff.
Liquidation costs and their source clocks are explicit. Missing material
valuation or flow evidence produces `UNKNOWN`, blocks new exposure and retains
the previously triggered protection. No market value is replaced with cost.
The observation cutoff advances independently of the last qualified accounting
cutoff, so a later complete proof can resolve intervening flows without losing
the original unit-accounting anchor.

The policy has explicit synthetic provenance, a version identity, exposure and
recovery ratios, session counts and an immutable synthetic calendar reference.
Risk escalation thresholds come from the committed authorized risk budget.
There are no personal policy defaults. The demonstration uses independently
chosen synthetic parameters and is not an investment policy.

Net liquidation equity divided by capital units gives unit NAV. Exact rational
units and the epoch high-water mark are saved alongside decimal report values.
External cash and asset flows issue or cancel units at the qualified pre-flow
NAV. Each flow must cover new committed transfer ledger entries exactly once.
Each pre-flow valuation must include all earlier flows in the interval.
Asset flows also require qualified contemporaneous asset prices. Internal
transfers must reconcile across accounts to zero value and do not change units.
Pre-flow qualified peaks and troughs participate in risk history.

## Protection lifecycle

Risk escalates immediately to the highest crossed state. `CAUTION` blocks new
exposure; `DEFENSIVE` adds an exposure ceiling; `PRESERVATION` latches a zero-stock
target. Direction and execution feasibility are distinct report facts. This
module does not allocate liquidation quantities across positions or execute
trades.

Recovery needs complete consecutive frozen market-close evidence, a clear
other-risk gate and, for defensive recovery, actual stock exposure within the
ceiling. A downgrade starts the next state's count afresh. Missing sessions and
unknown or adverse evidence reset the count. Recovery does not buy back stocks.
`synthetic-capital-calendar-v1` is a finite fictional sequence, not a real
exchange calendar; unknown session identities do not count.

Budget renewal or expiry does not reset units, the peak or the epoch. Expiry
blocks new exposure without stopping protection observations. Closing requires
zero stocks and reconciled orders, restrictions and cash. The closed epoch stays
protected throughout a complete zero-exposure cooling sequence. Reopening
requires a new approved capital authorization and an explicit confirmation
linked to the predecessor epoch. The successor records that predecessor; old
reports and their historical maximum drawdowns remain append-only.
An observation of an open epoch uses the effective authorized successor budget;
requesting a predecessor cannot bypass a confirmed tightening.
Refusing closure does not discard an independently qualified protection breach.
Cooling counts only complete session dates after closure. The retained stock
ledger identities detect intervening exposure, including round trips ending at
zero quantity; reopening cannot reuse a cooling proof invalidated by such trades.

## Scope

This is a credential-free synthetic capability. It makes no live qualification,
activation, provider, broker, notification or deployment claim. Conservative
valuation bounds, cross-engine target composition and executable liquidation
allocation are not provided by this command.

The integration tests exercise the public frozen-case runner and saved reports.
The browser tests cover accepted and unknown retained projections. Existing
report authentication, visibility and runtime ownership rules remain in force.
