"""Create M1 service heartbeats.

Revision ID: 0001_service_heartbeats
Revises:
"""
import sqlalchemy as sa
from alembic import op

revision = "0001_service_heartbeats"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "service_heartbeats",
        sa.Column("service_name", sa.String(length=64), primary_key=True),
        sa.Column(
            "last_seen",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("inference_ready", sa.Boolean(), nullable=False),
        sa.Column("message", sa.String(length=128), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("service_heartbeats")
