"""Токен агента: хеш в таблице workers.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("workers")}
    if "agent_token_hash" not in columns:
        op.add_column("workers", sa.Column("agent_token_hash", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("workers", "agent_token_hash")
