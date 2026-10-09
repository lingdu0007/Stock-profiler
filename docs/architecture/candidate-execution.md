# Saved broker execution reconciliation

The `candidate-execution.1.0.0` frozen-case capability records synthetic user
declarations and reconciles read-only broker facts through the existing host,
M-Agent Run, application event and formal report journey. Each request binds a
saved allocation, its latest confirmed vector, complete owner/account scope,
cutoff, previously seen execution event and idempotency key. All inputs remain
original D0 synthetic contracts. This capability provides no order submission,
preview, modification or cancellation operation.

Declarations retain their own reported state. They cannot create a fill, replace
an order, release capacity or clear an earlier reconciliation gap. A later
statement inherits the saved broker result and reservations. Reusing a key with
a different payload fails; replay recovers the same immutable event and Run.

Reconciliation requires the latest same-scope reconciled position event and
complete broker order evidence at the same cutoff. Broker fill and fee identities
must cover the authoritative ledger since confirmation. Duplicate identities,
missing facts, contradictory quantities or frozen cash, unknown order states,
foreign accounts and unavailable evidence fail closed. Account plus broker
identity identifies each order and fill. Previously attributed identities cannot
move to another intent. An explicit broker external-origin record can prove an
autonomous purchase even when it uses a candidate security.

The saved report distinguishes planned execution, provable deviations, external
purchases and unknown attribution. Causal evidence alone does not establish
planned execution: account, quantity, price, market window and saved step review
also matter. A step needs a separate saved atomic confirmation that replayed the
full allocation policy; the original batch acceptance alone does not establish
step review. Earlier order commitments consume the accepted quantity, and later
steps must follow the saved purchase sequence with review evidence covering
intervening fills. Fill quantities beyond the accepted intent are separately
retained as external quantities. Signed correction quantities remain linked to their
original broker entry and order; the broker ledger's additive deltas reconstruct
the current quantity and cash while every original entry and report survives.
Fees belong to one fill and cannot be counted twice or omitted.

Only the matched filled quantity converts the plan reservation into actual
holdings and cash use. The next allocation consumes those actual holdings from
fresh risk handoffs and replaces the original reservation with the saved remaining
claim. A linked open order and the unfilled plan share the stricter principal and
quantity commitment, including multiple broker orders. A proven account deviation
retains the original accepted account and qualifies each linked broker order by
its actual account. Every linked order contributes its estimated acquisition cost,
including each minimum commission; missing cost curves block new allocation.
Cash already frozen by
the broker is deducted only once; any larger plan cash requirement or held
reconciliation difference is additionally reserved. Unassociated open orders
reserve independently. Missing external-order cost curves block new allocation.
Prices improving on the accepted cap never expand quantities or reassign a plan:
the saved difference remains a cash hold until complete funds evidence arrives.

An explicit withdrawal is remembered by successor execution events. Unknown
attribution or unfinished related orders prevent release. A complete no-order
proof, broker final state, reconciled full fill, or partial fill with a withdrawn,
expired or invalidated remainder can close the corresponding commitment. A cash
hold remains until reconciliation completes. Expiry alone cannot release unknown
or open orders. A known unfinished order disappearing from a later snapshot
requires explicit broker terminal evidence. Future requests cannot revive an
explicitly withdrawn intent.

A plan terminal requires all actionable choices, accepted intents, broker orders,
fills, remaining claims and funds reconciliation to be closed. Outcomes include
no trade, all declined, deferred expiry, partial fill and full fill. Pending user
statements are displayed separately from broker execution and capacity evidence.
The report and HTTP surface remain read-only; generic user-fact endpoints allow
only viewing. Publication can recover independently of the single atomic event.

Integration journeys cover declarations, attribution, partial fills, conservative
capacity reuse, funds holds, broker conflicts, additive corrections, expiry,
terminal choices, immutable replay, commit faults and CLI recovery. All fixture
accounts, securities, amounts, policies and broker sources are invented.
