# Synthetic standard outcome reports

`standard-outcomes.1.0.0` extends the existing frozen decision-case journey. The
host resolves a same-owner, same-account committed selection event and retains
separate identities registered in the source commit for selection members, each frozen calibrated probability,
and each candidate. Candidate attempts at the selection cutoff retain their
monthly delivery facts. An empty or failed month can reference its committed
candidate event directly without inventing selection members.

The generator and seed identify fictional inputs. This contract does not use
provider credentials, personal trades, account costs, orders or external
notification delivery. It does not grant statistical qualification or activation.

## Entry and terminal wealth

The immutable synthetic calendar supplies the first five market sessions whose
opens follow the recommendation cutoff. Entry evidence must cover the ordered
prefix of that window, including explicit reasons for every preceding nonbuyable
session. The first buyable session supplies turnover / volume (VWAP), plus
commission, fees, taxes and slippage divided by the fixed standard quantity.
Incomplete entry evidence stays saved. If all five sessions are nonbuyable, the
member remains registered and becomes not achieved at its uniform six-month
maturity from the final window session.

For an executable entry, the terminal date is six natural months after entry,
clamping the day at month end and using the last saved session on or before the
target date. Terminal evidence must cover that exact close and corporate actions
through that close. Terminal wealth is quantity × cumulative share multiplier ×
terminal price + total cash distributions − commission − fees − taxes − slippage.
The denominator includes the applicable entry costs. Success compares exact
rational wealth against 1.20 × entry wealth; display rounding cannot promote a
below-threshold result. An intermediate high price does not form a success label.

## Saved versions and evidence

New source commits also save the standard quantity (100 synthetic units), calendar
version and versioned admission rule before any outcome evidence. Legacy saved
source facts remain readable without acquiring new fields on read.

The command contains a frozen cutoff, calendar version, standard quantity,
observations, and the latest retained evaluation event as `previous_event_id`.
The first version uses a null predecessor. Later versions must preserve the
calendar, quantity and all existing evaluation identities. Available evidence is
bounded by publication, acquisition and validation clocks. A reused evidence ID
cannot change payload. A replacement must use a new ID, name the prior evidence,
and include the authoritative correction reason. No user fact changes these
standard observations.

Each report saves registered, due, evaluable, due missing and immature counts for
all three populations, plus achieved / not achieved counts. Any due missing
outcome makes the affected population `INDETERMINATE`. `EVIDENCE_COMPLETE` means
only that due outcome evidence is complete; it is not an acceptance of a strategy
or a qualification decision. Evidence arriving later creates a linked new event
and report version. Prior report IDs remain independently readable behind the
existing authentication and account permission gates.

Candidate delivery snapshots retain month, disposition, original cutoff,
generation, commit, publication, saved window, expiry at the evaluation cutoff,
reminder observations, correction and supersession event identities. Cutoff-late
publication, reminders and corrections cannot appear in an earlier snapshot.

The generated OpenAPI/client and formal report view carry these saved fields.
The browser displays each population independently and links retained reports;
it does not derive new labels or membership.

## Verification

Run `uv run pytest tests/integration/test_standard_outcomes.py --no-cov` for the
frozen-case, durable-ledger and authenticated-read journey. The original shared
fixture is `tests/fixtures/synthetic/standard_outcomes.json`. Browser coverage is
`web/src/StandardOutcomeEvidence.test.tsx`; desktop and mobile coverage is
`web/e2e/standard-outcomes.spec.ts`. Full repository validation remains `make
verify` and the protected CI workflow, whose existing test sharder covers the
same complete Python collection with separate coverage files.
