FROM ghcr.io/astral-sh/uv:0.11.6@sha256:b1e699368d24c57cda93c338a57a8c5a119009ba809305cc8e86986d4a006754 AS uv

FROM python:3.11.15-slim@sha256:90744cff8f32887f075c47d747a173ff333e9e98801667af93c357fa9f5e28ff AS runtime

COPY --from=uv /uv /uvx /usr/local/bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=0 \
    UV_LINK_MODE=copy \
    VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:${PATH}"

RUN groupadd --gid 10001 stock-profiler \
    && useradd --uid 10001 --gid 10001 --create-home stock-profiler

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations
RUN uv sync --frozen --no-dev

ARG SOURCE_SHA=0000000000000000000000000000000000000000
ARG SOURCE_DATE_EPOCH=0
ENV STOCK_PROFILER_ENVIRONMENT=production \
    STOCK_PROFILER_SOURCE_SHA=${SOURCE_SHA} \
    SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH} \
    STOCK_PROFILER_APP_DATABASE_URL=sqlite:////var/lib/stock-profiler/application.sqlite3 \
    STOCK_PROFILER_M_AGENT_RUN_STORE_PATH=/var/lib/stock-profiler/m-agent-runs.sqlite3

RUN mkdir -p /var/lib/stock-profiler \
    && find /app /var/lib/stock-profiler -exec touch -h --date="@${SOURCE_DATE_EPOCH}" {} + \
    && chown -R stock-profiler:stock-profiler /app /var/lib/stock-profiler

USER stock-profiler

CMD ["uvicorn", "stock_profiler.entrypoints.http.app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
