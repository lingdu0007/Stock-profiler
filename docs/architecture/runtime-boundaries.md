# Runtime Boundaries

Stock Profiler is a modular monolith. HTTP, CLI, scheduler, worker, and
migration entrypoints load the same Python package and emit one version bundle.
The D0 synthetic seam exposes one replayable decision-case interface only.
Bootstrap composes adapters behind module-owned framework and ledger ports.
The decision-case module owns orchestration and domain outcomes; it does not
import adapters or SQLAlchemy. Only the persistence adapter interprets the
opaque transaction handle passed through the ledger port.

Application-owned runtime state uses one SQLite file through SQLAlchemy and
Alembic. M-Agent receives a distinct SQLite path through `SQLiteRunStore`.
There is no shared table and no cross-database transaction.

The host owns the frozen business identity, deterministic validation,
append-only decision event, stage-result ledger, correction lineage, and formal
report projection. M-Agent owns the
durable framework Run. A framework `SUCCEEDED` state is not publication: the
host validates typed output, durably commits the append-only event, then
publishes its report projection in a separate application transaction. A report
is readable only after it references that committed event; a report-projection
failure leaves the durable event without a readable report. An existing
nonterminal M-Agent Run is resumed under its original identity, while framework
states, host validation outcomes, business commits, publication, notifications,
and corrections retain their own append-only phase records.

Governed D0 cases record qualification in a separate `QUALIFICATION` stage.
Neither approval nor denial replaces the original business or lifecycle
outcome. Non-success business prerequisites cannot create authorization;
their saved negative outcomes remain readable. Evidence must have been
available by the original frozen knowledge cutoff, even when execution is
delayed. This also applies to inherited authorization evidence and every
retained alert. Invalid or timezone-less governed cutoffs are rejected and
audited before creating a Run. Reading a saved qualification outcome is
historical replay, not a new grant or activation.

Governed D0 commands supply an explicit `QualificationPolicy` inside their
capability version. Its contract version, policy version, original synthetic
provenance and clock parameters have no engine defaults. The complete policy
is frozen with the case and compared with the evidence version. A policy
version cannot be redefined within an existing scoped qualification history.
Accepted policy inputs retain a separate binding even when evidence or business
prerequisites deny qualification. That binding is provenance, not authorization;
a conflicting attempted redefinition does not replace or poison the original.
Missing, invalid or mismatched policy cannot grant authority or permit new
statistical use. This host accepts synthetic D0 policy only; it exposes no
personal policy authorization path.

New governance commands reject duplicate accounts, including their nested
evidence, before creating a business mapping or Run. Historical scope matching
uses account membership while retaining exact owner, visibility and every other
scope dimension. Stored account order and historical duplicate representations
are not normalized: their snapshots, fingerprints and original authorization
remain unchanged. Only a saved mapping or validated original-Run recovery can
reuse historical inputs; caller-supplied recovery markers are not new-request
authorization.

Policy-free historical snapshots remain deserializable without inserting a
policy field or changing their fingerprint. Saved reports remain historical
facts. If an original Run still needs host adjudication, absence of its original
policy closes the qualification gate with `QUALIFICATION_POLICY_REQUIRED`;
the host does not reconstruct a policy or substitute the demonstration fixture.
Explicit recovery must match the original durable Run, including the frozen
policy identity. Replaying a published business identity returns its original
report, even if the caller supplies another policy. Corrections likewise retain
the original policy and authorization snapshot, never re-adjudicating either.

Original frozen snapshots preserve build provenance across recovery. An
unmapped historical Run from another build closes the publication gate until
the original snapshot can be validated against that exact durable Run. The
pinned M-Agent adapter uses a read-only metadata inventory solely to veto
unsafe replacement creation; it neither adopts unknown Runs from SQL nor
reconstructs missing provenance. Normal recovery uses the public Run store.

The host pins the official M-Agent 0.5.1 wheel and its digest in the dependency
lock. New synthetic cases bind that identity. Healthy Runs created by the exact
previously pinned 0.5.0 wheel can be read or resumed with their original frozen
snapshot, Definition, input, checkpoint and Run identity. The explicit recovery
input is compared against the current template without rewriting the original;
unknown release identities and missing original Runs fail closed. Saved events,
qualification evidence and report fixtures keep their original runtime identity.

The upstream store performs its own atomic schema migration. Before opening a
real old store, stop every writer, take a consistent backup and upgrade every
process together. Mixed writers, downgrade of a migrated file, and reconstructing
damaged history are unsupported. Historical ownership collisions or unsupported
schemas raise `RunStoreIntegrityError`; they require an explicit recovery
decision using retained evidence, not a replacement Run. Rollback requires the
pre-upgrade backup and must not silently discard later Runs. The synthetic
compatibility tests install the exact official historical wheel in an isolated
environment to generate history; no neighboring runtime source is used.

Corrections append changed evidence, its source and reason, and independent
evidence clocks and cutoff. They reference the original event and evidence
without rewriting the original Run, report, outcome, or cutoff. Each report
version remains independently readable.
Scoped corrections retain the original projection contract and qualification
snapshot. They do not re-adjudicate authorization or add qualification history.

The HTTP transport returns Pydantic DTOs from `/api/v1`. Version diagnostics
identify the installed build, while the static safety-capabilities diagnostic
reports the official single-user, non-public-recommendation, and no-order-
writing invariants without offering an action. A read-only report route requires
an opaque Passkey session; browser enrollment requires a ten-minute
host-console bootstrap or recovery grant and has no public registration page.
The host console is a direct CLI operation, not a public HTTP endpoint. Caddy
terminates same-origin TLS for the configured private hostname before serving
the PWA and `/api/v1`.
The React client is generated from the OpenAPI 3.1 document and renders only
the committed report projection and its saved stage outcomes. The PWA precaches static assets only, has no
runtime API cache configuration, and private API responses use `no-store`.
