# Runtime Boundaries

Stock Profiler is a modular monolith. HTTP, CLI, scheduler, worker, and
migration entrypoints load the same Python package and emit one version bundle.
The D0 synthetic seam exposes one replayable decision-case interface only.

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
