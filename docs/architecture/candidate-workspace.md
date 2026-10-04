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
declarations. No candidate UI control creates a choice, capacity reservation,
account route, personal quantity or order capability.

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
and mobile widths with independent synthetic responses. These contracts do not
establish real qualification, personal activation or notification-provider health.
