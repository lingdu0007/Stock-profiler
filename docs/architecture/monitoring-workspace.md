# Monitoring Workspace

The `monitoring.1.0.0` frozen host contract projects committed decisions into
the existing authenticated report surface. It has no order capability and
does not connect to a broker or notification provider.

## Ownership

The decision ledger owns assessment and report identities. Monitoring reads
published execution-plan and reconciliation reports; the browser cannot supply
a verdict or close a risk obligation. Overview pointers and inbox entries are
derived from authorized reports, not a separate case database.

Daily assessment identity binds the owner, normalized account scope, portfolio,
local market date and contract. Retries recover the original frozen snapshot.
Event identities bind the validated event set. Reports for rejected historical
requests remain in the archive without displacing forward current pointers.

Four user-fact kinds and authoritative reconciliation retain separate meanings.
User execution declarations remain pending, non-authoritative observations.
Confirmation of a monitoring plan fails closed when newer owner facts require
a new plan or the report is superseded, corrected or outside its window.

## Frozen Evidence

Market/security, company events, broker accounts, costs/rules and
qualification/version evidence carry individual provenance and clocks.
Incomplete evidence cannot manufacture an exact quantity. Protective events
can preserve an exit target while quantity verification is unavailable.
Minimum-evidence risk events require an existing REDUCE or EXIT obligation;
missing evidence cannot turn a normal HOLD target into risk remediation.
Concurrent events preserve each affected target and its own priority. Cases
retain all contributing saved plan identities when targets span multiple sources.
Protective priority comes from saved target provenance or capital-preservation
state, never merely from a zero quantity. Opening a complete assessment version
automatically records viewing without acknowledging or confirming it.

Publication timestamps are read from durable business-commit and publication
stage records. Reading or refreshing a page never advances those clocks.

## Synthetic Notifications

Channel observations are explicit D0 inputs. Provider acceptance is not proof
of delivery, reading or execution. P0 attempts both routes; P1 conditionally
falls back. External bodies contain only priority, a generic reason, window
context and an authenticated workspace entry.

A quiet-period deferral retains its due instant. A subsequent frozen attempt
references the previous run, retains the notification intent and case, and
must be due. Replaying an attempt does not repeat it.

Operations reports bind twenty explicitly supplied planned trading dates.
Missing daily records stay visible. Monthly snapshots have a month identity,
and audit reports do not recursively count earlier audit projections.

## Verification

`tests/integration/test_monitoring_workspace.py` exercises the frozen ledger
and authentication boundaries. Browser tests cover independent facts and
overview/inbox/archive navigation. The Playwright monitoring workflow checks
desktop and mobile layouts with independent synthetic responses.

## Versioned Statistical Conclusions

The additive `monitoring.1.1.0` contract can consume a published
`position-action.1.0.0` report. It preserves the saved action and independent
deterministic execution plan; monitoring never recalculates a probability,
quantity, reduction anchor or cost anchor. Effective statistical REDUCE and EXIT
conclusions receive a P2 case binding the position lifecycle, exact conclusion
report, qualification, direction, target and next executable window. HOLD,
ABSTAIN and data observations remain in the ordinary assessment view.

The frozen calendar must match the security assessment's version. The supplied
next window must follow the original cutoff and fit the original probability
validity. A conclusion can establish that window once. A subsequent daily or
validated event assessment needs a newly committed position-action version;
it cannot rewrap the old conclusion to renew its window. Original reports stay
immutable. Independent P0/P1 targets retain their own provenance and survive a
missing or expired statistical window.

The authorized workspace and confirmation boundary revalidate the saved source,
current qualification and original evidence. At the window's end, P2 leaves the
inbox and loses confirmation eligibility. The browser advances only its display
clock, periodically refreshes authorized pointers, and preserves cutoff,
assessment, publication and user-fact timestamps. A reported execution remains
pending authoritative reconciliation and cannot renew or discharge a conclusion.

P2 respects quiet hours and uses one immediate route. Its stable message identity
binds the case and route; individual retries have distinct attempt identities.
A retry keeps the original case, conclusion and window. Re-keying the same
conclusion cannot open another reminder chain. A quiet period ending at or after
expiry leaves no deferred statistical reminder. Lifecycle, notification and
operations reports retain the exact original conclusion reference. Shadow
results remain closed at the shared ledger publication and authorization boundary.

`tests/integration/test_statistical_monitoring.py` verifies this delivery seam
against saved host/M-Agent reports, including mixed deterministic risks,
confirmation, expiry, quiet hours, supersession and shadow isolation. All channel
outcomes and inputs remain synthetic; no provider or order capability is added.
