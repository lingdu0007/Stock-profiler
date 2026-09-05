# Runtime Boundaries

Stock Profiler is a modular monolith. HTTP, CLI, scheduler, worker, and
migration entrypoints load the same Python package and emit one version bundle.
The D0 synthetic seam exposes one replayable decision-case interface only.

Application-owned runtime state uses one SQLite file through SQLAlchemy and
Alembic. M-Agent receives a distinct SQLite path through `SQLiteRunStore`.
There is no shared table and no cross-database transaction.

The host owns the frozen business identity, deterministic validation,
append-only decision event, and formal report projection. M-Agent owns the
durable framework Run. A framework `SUCCEEDED` state is not publication: the
host validates typed output, commits the event and report atomically in its
application database, then makes the report readable by its independent report
version ID.

The HTTP transport returns Pydantic DTOs from `/api/v1`. Version diagnostics
identify the installed build, while the static safety-capabilities diagnostic
reports the official single-user, non-public-recommendation, and no-order-
writing invariants without offering an action. A read-only report route requires
an opaque Passkey session; browser enrollment requires a ten-minute
host-console bootstrap or recovery grant and has no public registration page.
The React client is generated from the OpenAPI 3.1 document and renders only
the committed report projection. The PWA precaches static assets only, has no
runtime API cache configuration, and private API responses use `no-store`.
