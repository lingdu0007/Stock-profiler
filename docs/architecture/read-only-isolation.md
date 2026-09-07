# Read-Only Isolation

The version 3 D0 case freezes a single result owner, all contributing account
identities, and USER or SHADOW visibility into its fingerprint, event, and
report projection. Definition 2 adds a deterministic Context Provider with
only that frozen input. Historical version 1 and 2 snapshots retain their
serialization and recovery identities; their owner is the sole application
user and their account is read from the saved synthetic event, never from a
new fixture or a request.

`ResultDelivery` is the user-facing storage boundary for reports and their
user-fact view. It requires an explicit trusted `AccessPrincipal`; absent
identity, wrong owner, missing account coverage, or missing permission returns
no result. HTTP derives the principal from a verified single-user passkey
session and startup-frozen host grants. Client-supplied identity fields confer
no authority. Every denied read appends an opaque request digest, reason,
surface, and observation time to an application-owned audit table. Audit
inspection is a host-console operation and has no public route.

Shadow events remain internal. Both orchestration and direct report
publication reject them. No summary, conversation, export, or notification
callback is registered. Probes of those routes are rejected without reflecting
their request content. All API responses prohibit caching; the PWA has no
runtime API cache. The internal `DecisionLedger`, its engine, and M-Agent's Run
store are trusted host persistence, not tools available to a browser or model.
This boundary does not claim to sandbox arbitrary Python execution or an
operator who already controls the database or source.

Governance-history reads require the frozen access scope. The host selects
original events with the same saved owner, accounts, and visibility before
adjudication, so a USER decision cannot inherit a SHADOW authorization,
evidence record, or state. The source scope remains in each original event.

Portfolio authorization history is a narrower trusted-host lineage lookup:
positive evidence selects only the same saved owner, visibility, portfolio
identity, and evidence available by the requesting case's frozen knowledge
cutoff. A separate full same-owner, same-visibility, same-portfolio lineage
read is permitted only inside the serialized host transaction as a negative
stale revision/use guard: it can reject a delayed forward authorization that
would fork a later lineage, or a new-exposure request against an effective
successor, but cannot positively authorize, preload, return, or project a
later authorization or its account data. Before a portfolio use
result can return a snapshot, preview, or unfinished obligation, the requesting
frozen scope must cover every account represented by that returned data. Browser
and model callers never receive either ledger lookup itself.

A same-owner, same-visibility cross-portfolio lookup is also host-only and
negative: it can reject an attempted new portfolio identity whose complete
account universe overlaps a retained authorization. It returns no predecessor,
account, obligation, or successor data, and cannot itself authorize a new
portfolio.

`GET` and `POST /api/v1/reports/{id}/facts` read and append independent VIEWED,
ACKNOWLEDGED, CONFIRMED, and EXECUTION_DECLARED facts. Mutation additionally
requires same-origin session/CSRF validation and `USER_FACT`. Confirmations
carry only ACCEPT, DECLINE, or DEFER. Execution declarations carry reported
states only, remain non-authoritative and pending reconciliation, and cannot
contain a security, direction, quantity, price, order identifier, or callback.
Same-key retries return the original fact; conflicting retries append a
denial. Reports and user facts are never overwritten.

The registered Tool allowlist is explicitly empty. The host admits only its
deterministic model adapter and frozen Context Provider, with no secondary
model adapters, custom executable policy, context stages, or compression
extension. No Session companion, live model, broker client, order credential,
notification provider, or order adapter is composed. Unregistered model Tool
calls are rejected by the pinned M-Agent contract before a Tool Step. The
host preserves an additional opaque capability-denial fact. CLI and HTTP
reject undeclared commands and routes before dispatch.

`tests/security`, the focused integration isolation tests, and the browser
shadow test exercise this credential-free D0 contract. The original
`read_only_security_cases.json` fixture fixes seed 4519, identities, closed
surfaces, and all five order operations. Every negative probe is mandatory;
there is no tolerated-failure threshold or skip marker. Route and command
inventory tests fail when the reachable surface changes. The normal full
verification also checks the immutable dependency lock, source role,
generated API contract, migrations, and application/browser builds.
