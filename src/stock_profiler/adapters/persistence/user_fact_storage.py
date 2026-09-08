"""The existing append-only user-fact table shared by delivery and audit projections."""

from sqlalchemy import Column, Integer, MetaData, String, Table

USER_FACTS = Table(
    "report_user_facts",
    MetaData(),
    Column("sequence", Integer, primary_key=True, autoincrement=True),
    Column("fact_id", String(96), nullable=False, unique=True),
    Column("report_version_id", String(96), nullable=False),
    Column("request_digest", String(64), nullable=False),
    Column("fact_payload", String, nullable=False),
)
