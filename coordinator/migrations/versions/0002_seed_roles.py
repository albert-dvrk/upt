"""seed roles

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

ROLES = ("viewer", "operator", "administrator")


def upgrade() -> None:
    roles = sa.table("roles", sa.column("name", sa.String))
    op.bulk_insert(roles, [{"name": name} for name in ROLES])


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM roles WHERE name IN ('viewer', 'operator', 'administrator')"))
