"""Persist recommendation transitions for MAC-33 / MAC-55 / MAC-56 / MAC-57.

Revision ID: 0004_recommendations
Revises: 0003_product_observations
"""

import sqlalchemy as sa
from alembic import op

revision = "0004_recommendations"
down_revision = "0003_product_observations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_notification_outbox_state"), "notification_outbox", type_="check")
    op.create_check_constraint(
        op.f("ck_notification_outbox_state"),
        "notification_outbox",
        "state IN ('pending', 'sending', 'sent', 'failed', 'cancelled')",
    )
    op.create_table(
        "recommendation_state",
        sa.Column("consumable_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('OK', 'BUY SOON', 'BUY NOW')", name=op.f("ck_recommendation_state_state")
        ),
        sa.CheckConstraint(
            "generation >= 0", name=op.f("ck_recommendation_state_generation_nonnegative")
        ),
        sa.CheckConstraint(
            "length(trim(reason)) > 0", name=op.f("ck_recommendation_state_reason_nonempty")
        ),
        sa.ForeignKeyConstraint(
            ["consumable_id"],
            ["consumable.id"],
            name="fk_recommendation_state_consumable_id_consumable",
        ),
        sa.PrimaryKeyConstraint("consumable_id", name="pk_recommendation_state"),
    )


def downgrade() -> None:
    op.drop_table("recommendation_state")
    op.execute("UPDATE notification_outbox SET state = 'failed' WHERE state = 'cancelled'")
    op.drop_constraint(op.f("ck_notification_outbox_state"), "notification_outbox", type_="check")
    op.create_check_constraint(
        op.f("ck_notification_outbox_state"),
        "notification_outbox",
        "state IN ('pending', 'sending', 'sent', 'failed')",
    )
