"""Preserve result-access denials independently of rotating operational logs."""

import sqlalchemy as sa
from alembic import op

revision = "0006_result_access_audit"
down_revision = "0005_persist_frozen_case_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "result_access_audit",
        sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("surface", sa.String(32), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.String(40), nullable=False),
    )
    for operation in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER result_access_audit_no_{operation.lower()} "
            f"BEFORE {operation} ON result_access_audit BEGIN "
            "SELECT RAISE(ABORT, 'access audit is append-only'); END"
        )
    op.create_table(
        "report_user_facts",
        sa.Column("sequence", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("fact_id", sa.String(96), nullable=False, unique=True),
        sa.Column("report_version_id", sa.String(96), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("fact_payload", sa.String(), nullable=False),
    )
    for operation in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER report_user_facts_no_{operation.lower()} "
            f"BEFORE {operation} ON report_user_facts BEGIN "
            "SELECT RAISE(ABORT, 'user facts are append-only'); END"
        )


def downgrade() -> None:
    connection = op.get_bind()
    for name in ("report_user_facts", "result_access_audit"):
        table = sa.table(name)
        if connection.execute(sa.select(sa.func.count()).select_from(table)).scalar_one():
            raise RuntimeError("append-only access history cannot be downgraded")
    for name in ("report_user_facts", "result_access_audit"):
        op.drop_table(name)
