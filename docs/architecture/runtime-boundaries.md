# Runtime Boundaries

Stock Profiler is a modular monolith. HTTP, CLI, scheduler, worker, and
migration entrypoints load the same Python package and emit one version bundle.
The baseline exposes no business decision interfaces.

Application-owned runtime state uses one SQLite file through SQLAlchemy and
Alembic. M-Agent receives a distinct SQLite path through `SQLiteRunStore`.
There is no shared table and no cross-database transaction.

The HTTP transport returns Pydantic DTOs from `/api/v1`. The React client is
generated from the OpenAPI 3.1 document and only displays the diagnostic
version bundle. The PWA precaches static assets only; it has no runtime API
cache configuration.
