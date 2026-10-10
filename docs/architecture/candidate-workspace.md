# Candidate Workspace

The authenticated `/candidates` workspace derives B1 release and security-detail
views from the shared decision ledger. Reports own release identities; each
security detail binds its report version and research identity. The workspace
owns neither a decision nor a mutable recommendation record. It uses the same
passkey session, account grants, denial audit and no-store read boundary as the
monitoring workspace and formal reports.

## Current and historical views

Current monthly pointers follow saved correction lineage within the original
owner and account scope. Original reports remain addressable after correction,
expiry or invalidation. Old deep links show the original release, the current
eligibility reasons and an explicit replacement link; they never redirect to a
new recommendation silently. The original cutoff, generation, commit,
publication, qualification snapshot, research evidence clocks and five-session
window are preserved. Reading or refreshing changes only the observation time.

Eligibility is checked against the saved governance history at read time. A
missing, ambiguous, scope-mismatched, expired, suspended or revoked qualification
cannot make a saved candidate current. The current qualification status is
separate from the publication snapshot. Once a saved qualification transition
invalidates a release, later restoration does not revive it; same-value reviews
retain the original invalidating evidence anchor. A correction to its source research also
invalidates current use. Expiry uses the original saved market sessions and
immutable calendar version. Detail views keep the frozen probability, independent
risk gates, thesis, risks and freshness beside current eligibility. Rejected
members retain their own reasons and do not become candidates through projection.

B1 exposes release and detail reading, plus an optional viewing fact. The shared
user-fact boundary rejects candidate acknowledgement, confirmation and execution
declarations. Personalized allocation, confirmation and execution reports require
the independent `CANDIDATE_ALLOCATION` host grant, including generic report deep
links and viewing facts. B1 reading alone does not expose a personal plan.

## Personal allocation and execution

The same authenticated view projects each saved allocation beside its original
candidate batch. Allocation, policy review, complete batch choice, confirmation,
reservation and execution histories retain their own event and report identities.
Account routes, legal quantities, confirmed price caps, full/partial/zero reasons,
capacity margins and evidence clocks come from the saved plan. There is no combined
status that overwrites candidate qualification or broker facts.

`POST /api/v1/candidates/commands` requires the independent `CANDIDATE_COMMAND`
grant, the existing account scope, exact Origin, CSRF token and recent passkey
verification. Its narrow intent contract references an original plan report, a
saved input report, the seen confirmation and execution versions, and an
idempotency key. The host resolves inputs from its own ledger. The browser cannot
submit portfolio snapshots, risk handoffs, price caps, broker facts or reservation
amounts. The normal frozen-case runner validates provenance, serializes decisions,
and atomically commits outcomes and reservations, followed by separately
recoverable report publication. Framework Runs remain execution
mechanics without business authority.

A read-only policy review produces a saved `REVALIDATED` or blocked report without
forming choices or reservations. Confirmation and each next execution-step review
replay the complete allocation policy. A structured plan change permanently stops
new actions on the old plan; presenting its old inputs again cannot revive it.
Replanning records the original plan event and explicit trigger, keeps the original
candidate window, and forms no inherited confirmation. A successful replacement
stops the old plan while preserving its reports and outstanding commitments.

The browser keeps a complete choice draft separate from saved facts and displays
policy review before enabling confirmation. A response without a report or a lost
response keeps the exact original request in memory for reconciliation. It blocks
new submissions until that request resolves; host commit recovery and uncertainty
guards remain authoritative across sessions and devices. No command payload is
persisted in browser storage. An application-level memory owner retains unresolved
requests across failed refreshes and route navigation. Failed refreshes withdraw current presentation, and
the local deadline disables new entry actions at the original saved expiry.
An original-key confirmation or replanning retry whose business event has not committed rechecks the latest
saved allocation inputs at adjudication and immediately before commit, including
when a successful phase result is cached. Changed prices or policy inputs produce
an `ALLOCATION_INPUT_VERSION_CONFLICT` report without reservations. An event that
already committed continues to replay its original report exactly. Rejected
derived replanning attempts remain in report history and cannot replace the latest
accepted allocation inputs. Replanning lineage is compared separately from the
price, policy and evidence inputs it references.
New replacement plans also require current retained candidate qualification,
checked at adjudication and immediately before commit. A suspension or revocation
after the saved input cutoff stops replanning. Historical initial allocations
retain their original cutoff semantics, and committed replacement reports remain
available for exact replay. The browser stops replanning for current candidate or
execution stops while allowing changed inputs to request a replacement within the
original window.

User execution declarations are explicitly pending reconciliation. Read-only
execution reports separately display authoritative orders, fills, attribution,
deviations, unknown identities, residual commitments, causal releases and terminal
outcomes. Further pending declarations remain available after the entry window
closes or while broker reconciliation is outstanding; they never release capacity.
Withdrawal references a saved authoritative order-exclusion report;
unclosed execution facts and unknown orders retain capacity. Only host broker
reconciliation can establish execution or release facts. Released capacity never
expands a different candidate automatically. This surface provides no order
generation, broker prefill or broker write capability.

The browser rechecks eligibility on candidate-view navigation and window focus,
and refreshes an open view every minute. A local absolute-expiry guard withdraws
current presentation at the saved deadline even while a response is pending.
Population and research counts come from the original committed research facts;
unavailable provenance remains explicit rather than reconstructing a population.

## Synthetic reminder observations

`ResultDelivery.candidate_reminder` is a host-only, credential-free D0 routing
seam for reliably saved, published, authorized results. It appends typed reminder
observations to the existing stage ledger; the workspace exposes their history.
There is no browser notification callback or live provider connection. Requests
record their synthetic generator and seed and are revalidated at the host
boundary. Provider acceptance, rejection, timeout and unknown delivery remain
separate from completion of the routing attempt and from any user action.

Each user and original plan month has at most one initial intent and one final
expiry intent. B1 rejects plan-ready reminders because it has no personal plan.
Final reminders require remaining candidates and occur before the fifth session
starts. A material withdrawal can create one correction intent bound to the
original report and invalidating evidence. That identity survives natural expiry,
so expiry neither adds another withdrawal intent nor blocks an unsuccessful
withdrawal retry. Ordinary reminders respect quiet
periods and the user's ordinary-reminder preference. Deferred or unsuccessful
attempts explicitly reference their predecessor and reuse the original intent
and body; retries cannot extend the market window.

A necessary correction requires a prior external attempt that could have reached
the user. Only a quiet delay that could allow continued reliance within the
original window bypasses quiet and attempts both primary and persistent roles.
Its minimal result type explicitly says the original has been withdrawn and must
not be relied on. Other external bodies contain only result type, candidate
count, original window end and an authenticated read-only release entry. They
contain no securities, probabilities, thesis, accounts or personal quantities.
Shadow, uncommitted, unsaved, expired or superseded releases cannot produce new
current-result reminders. Historic withdrawal notices remain distinct from a
current recommendation.

## Verification

The candidate workspace integration tests exercise saved report identity,
authentication, correction history, qualification changes, action isolation,
monthly budgets and append-only routing. Unit tests check window and reminder
eligibility, idempotency, quiet deferral, channel fallback and withdrawal routing.
Browser and Playwright tests cover release/detail/history navigation at desktop
and mobile widths with independent synthetic responses, plus complete batch
intent, policy review, immutable allocation reports and original-key recovery.
Host journeys exercise scope grants, policy changes, original-window replanning,
pending declarations, authoritative withdrawal and interrupted atomic commits.
These contracts do not
establish real qualification, personal activation or notification-provider health.
