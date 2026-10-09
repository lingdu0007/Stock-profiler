# Saved batch confirmation

The `candidate-confirmation.1.0.0` frozen-case capability submits a complete
choice vector for a saved allocation plan. It uses the existing synthetic
CLI/bootstrap and application ledger. This contract remains non-actionable and
does not grant personal qualification or provide broker order authority.

Choices can be edited locally before submission. A draft has no ledger identity
or capacity effect. A submission binds the single user, complete portfolio
scope, candidate batch, exact plan event and plan identity, previously seen
confirmation event, and idempotency key. Only positive planned rows with legal
account routes and confirmed price caps belong to the actionable vector.
Duplicate, incomplete or foreign row identities fail closed. Declining or
deferring a row preserves its original candidate conclusion and evaluation
registrations.

The host replays the allocation policy against saved risk handoffs, effective
authorization, retained qualification, current evidence and outstanding
reservations. Structured quantities, accounts, purchase sequence, price caps,
route rules, reasons and every capacity remainder must match the saved plan.
Changing those outputs requires a new allocation plan. The original market
window is checked again immediately before the application event commits.

The complete confirmation and all accepted purchase legs are one immutable
application event payload. SQLite serializes revalidation and the single event
write. There is no separately committed reservation row. A failed write leaves
neither fact; an uncertain acknowledgement is reconciled against that exact
event identity. Until reconciliation succeeds, another key cannot submit new
confirmation or allocation for the same portfolio. Report publication is a
separate recoverable projection and cannot discard a committed reservation.
Reusing a key with different choices or scope is a payload conflict. Another
device with an old seen version cannot replace the latest confirmed vector.

Allocation reads the latest confirmed event for each plan directly from the
ledger and includes its retained reservations in cash, stress, issuer, turnover
and correlation capacity. Callers cannot omit or replace a reservation. Its
original provenance remains historical evidence; later snapshots do not expire
that capacity claim. Reservations remain after the entry window closes.

A revision appends a successor event and keeps the identities and quantities of
accepted legs that remain accepted. Withdrawn legs are named in the same event's
release list. Release requires a same-scope, uncorrected, fully reconciled
position event covering the current cutoff after the preceding confirmation.
Unfinished or unknown orders prevent release. Holdings requiring execution
attribution also retain their reservation until reconciliation is available.
An explicit `WITHDRAW` vector contains no accepts and can release a still
unexecuted reservation after the original entry window closes. Releasing one
leg never increases another leg or rewrites the old plan.

Generic report user-fact endpoints permit only viewing of candidate,
allocation and confirmation reports. A single-row confirmation, execution
claim, notification, copy action or chat cannot create a batch fact. The
capability exposes no broker order, order preview or order mutation operation.
The authenticated allocation workspace is a separate delivery surface over
these saved host facts.

Integration tests exercise the complete saved-plan journey, choice and policy
conflicts, concurrency, write and acknowledgement faults, original-key replay,
append-only revision, order-gated withdrawal and generic-action isolation.
The calibrated candidate journey also checks confirmation without changing its
original candidate report or evaluation registrations. All inputs and policies
are independently generated synthetic fixtures.
