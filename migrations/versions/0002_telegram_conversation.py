"""Persist the single active onboarding conversation per household."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_telegram_conversation"
down_revision = "0001_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "telegram_conversation",
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column("step", sa.String(16), nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "step IN ('name', 'stock', 'unit', 'daily', 'reserve', 'confirm')",
            name=op.f("ck_telegram_conversation_step"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(data) = 'object'", name=op.f("ck_telegram_conversation_data_object")
        ),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_telegram_conversation_household_id_household"),
        ),
        sa.PrimaryKeyConstraint("household_id", name=op.f("pk_telegram_conversation")),
    )


def downgrade() -> None:
    op.drop_table("telegram_conversation")
