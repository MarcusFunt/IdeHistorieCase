"""Allow a committed turn to record several validated scenario effects."""
import sqlalchemy as sa
from alembic import op

revision = "0004_multiple_events_per_turn"
down_revision = "0003_scenario_definitions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("game_events_committed_turn_id_key", "game_events", type_="unique")
    op.add_column(
        "game_events",
        sa.Column("event_index", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.create_check_constraint(
        "ck_game_events_event_index_nonnegative",
        "game_events",
        "event_index >= 0",
    )
    op.create_unique_constraint(
        "uq_game_events_turn_index",
        "game_events",
        ["committed_turn_id", "event_index"],
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM game_events
                GROUP BY committed_turn_id
                HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade: one or more committed turns have multiple game events';
            END IF;
        END
        $$;
        """
    )
    op.drop_constraint("uq_game_events_turn_index", "game_events", type_="unique")
    op.drop_constraint("ck_game_events_event_index_nonnegative", "game_events", type_="check")
    op.drop_column("game_events", "event_index")
    op.create_unique_constraint(
        "game_events_committed_turn_id_key",
        "game_events",
        ["committed_turn_id"],
    )
