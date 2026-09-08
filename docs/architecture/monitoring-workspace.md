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
