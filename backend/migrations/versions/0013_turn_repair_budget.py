"""Persist the single structured repair budget for each durable turn request."""
import sqlalchemy as sa
from alembic import op

revision = "0013_turn_repair_budget"
down_revision = "0012_initialization_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "turn_requests",
        sa.Column("repair_used", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    repair_was_used = op.get_bind().exec_driver_sql(
        "SELECT EXISTS (SELECT 1 FROM turn_requests WHERE repair_used)"
    ).scalar_one()
    if repair_was_used:
        raise RuntimeError("cannot drop a durable turn repair budget after it has been used")
    op.drop_column("turn_requests", "repair_used")
