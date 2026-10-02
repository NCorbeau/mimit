"""Initial foundation schema, frozen independently from application metadata.

Revision ID: 0001_foundation
Revises:
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_foundation"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "household",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(trim(name)) > 0", name=op.f("ck_household_name_nonempty")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_household")),
    )
    op.create_table(
        "notification_outbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(state = 'sending' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (state <> 'sending' AND lease_owner IS NULL AND lease_expires_at IS NULL)",
            name=op.f("ck_notification_outbox_lease_state"),
        ),
        sa.CheckConstraint(
            "(state = 'sent' AND delivered_at IS NOT NULL) "
            "OR (state <> 'sent' AND delivered_at IS NULL)",
            name=op.f("ck_notification_outbox_delivery_state"),
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'sending', 'sent', 'failed')",
            name=op.f("ck_notification_outbox_state"),
        ),
        sa.CheckConstraint(
            "attempts >= 0", name=op.f("ck_notification_outbox_attempts_nonnegative")
        ),
        sa.CheckConstraint(
            "length(trim(dedupe_key)) > 0", name=op.f("ck_notification_outbox_dedupe_key_nonempty")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_outbox")),
        sa.UniqueConstraint("dedupe_key", name=op.f("uq_notification_outbox_dedupe_key")),
    )
    op.create_index(
        "ix_notification_outbox_lease_expires_at",
        "notification_outbox",
        ["lease_expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_notification_outbox_state_run_at",
        "notification_outbox",
        ["state", "run_at"],
        unique=False,
    )
    op.create_table(
        "scheduled_job",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("job_type", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(state = 'running' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (state <> 'running' AND lease_owner IS NULL AND lease_expires_at IS NULL)",
            name=op.f("ck_scheduled_job_lease_state"),
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')",
            name=op.f("ck_scheduled_job_state"),
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_scheduled_job_attempts_nonnegative")),
        sa.CheckConstraint(
            "length(trim(dedupe_key)) > 0", name=op.f("ck_scheduled_job_dedupe_key_nonempty")
        ),
        sa.CheckConstraint(
            "length(trim(job_type)) > 0", name=op.f("ck_scheduled_job_job_type_nonempty")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scheduled_job")),
        sa.UniqueConstraint("dedupe_key", name=op.f("uq_scheduled_job_dedupe_key")),
    )
    op.create_index(
        "ix_scheduled_job_lease_expires_at", "scheduled_job", ["lease_expires_at"], unique=False
    )
    op.create_index(
        "ix_scheduled_job_state_run_at", "scheduled_job", ["state", "run_at"], unique=False
    )
    op.create_table(
        "telegram_update_receipt",
        sa.Column("update_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "update_id >= 0", name=op.f("ck_telegram_update_receipt_update_id_nonnegative")
        ),
        sa.PrimaryKeyConstraint("update_id", name=op.f("pk_telegram_update_receipt")),
    )
    op.create_table(
        "consumable",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("household_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("stock_quantity", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("canonical_unit", sa.Text(), nullable=False),
        sa.Column("daily_consumption", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("reserve_days", sa.Integer(), nullable=False),
        sa.Column("stock_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(trim(canonical_unit)) > 0", name=op.f("ck_consumable_canonical_unit_nonempty")
        ),
        sa.CheckConstraint(
            "stock_quantity >= 0 AND stock_quantity <> 'NaN'::numeric",
            name=op.f("ck_consumable_stock_nonnegative"),
        ),
        sa.CheckConstraint(
            "daily_consumption > 0 AND daily_consumption <> 'NaN'::numeric",
            name=op.f("ck_consumable_daily_consumption_positive"),
        ),
        sa.CheckConstraint(
            "reserve_days >= 0", name=op.f("ck_consumable_reserve_days_nonnegative")
        ),
        sa.CheckConstraint("length(trim(name)) > 0", name=op.f("ck_consumable_name_nonempty")),
        sa.ForeignKeyConstraint(
            ["household_id"], ["household.id"], name=op.f("fk_consumable_household_id_household")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_consumable")),
    )
    op.create_index(
        op.f("ix_consumable_household_id"), "consumable", ["household_id"], unique=False
    )
    op.create_table(
        "offer_source",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("consumable_id", sa.Uuid(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("variant", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(trim(url)) > 0", name=op.f("ck_offer_source_url_nonempty")),
        sa.ForeignKeyConstraint(
            ["consumable_id"],
            ["consumable.id"],
            name=op.f("fk_offer_source_consumable_id_consumable"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_offer_source")),
        sa.UniqueConstraint("consumable_id", name=op.f("uq_offer_source_consumable_id")),
    )
    op.create_table(
        "purchase",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("consumable_id", sa.Uuid(), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("purchased_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("price", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("source", sa.Text(), nullable=True),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name=op.f("ck_purchase_currency_format")),
        sa.CheckConstraint(
            "(price IS NULL AND currency IS NULL) OR (price IS NOT NULL AND currency IS NOT NULL)",
            name=op.f("ck_purchase_price_currency"),
        ),
        sa.CheckConstraint(
            "price IS NULL OR (price >= 0 AND price <> 'NaN'::numeric)",
            name=op.f("ck_purchase_price_nonnegative"),
        ),
        sa.CheckConstraint(
            "quantity > 0 AND quantity <> 'NaN'::numeric",
            name=op.f("ck_purchase_quantity_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["consumable_id"], ["consumable.id"], name=op.f("fk_purchase_consumable_id_consumable")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_purchase")),
    )
    op.create_index(
        "ix_purchase_consumable_purchased",
        "purchase",
        ["consumable_id", "purchased_at"],
        unique=False,
    )
    op.create_table(
        "price_observation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("offer_source_id", sa.Uuid(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("price", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("unit_price", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("unit", sa.Text(), nullable=True),
        sa.Column("availability", sa.String(length=16), nullable=False),
        sa.CheckConstraint(
            "availability IN ('available', 'unavailable', 'unknown')",
            name=op.f("ck_price_observation_availability"),
        ),
        sa.CheckConstraint(
            "currency ~ '^[A-Z]{3}$'", name=op.f("ck_price_observation_currency_format")
        ),
        sa.CheckConstraint(
            "price IS NULL OR currency IS NOT NULL",
            name=op.f("ck_price_observation_price_currency"),
        ),
        sa.CheckConstraint(
            "price IS NULL OR (price >= 0 AND price <> 'NaN'::numeric)",
            name=op.f("ck_price_observation_price_nonnegative"),
        ),
        sa.CheckConstraint(
            "unit_price IS NULL OR (currency IS NOT NULL AND unit IS NOT NULL "
            "AND length(trim(unit)) > 0)",
            name=op.f("ck_price_observation_unit_price_unit"),
        ),
        sa.CheckConstraint(
            "unit_price IS NULL OR (unit_price >= 0 AND unit_price <> 'NaN'::numeric)",
            name=op.f("ck_price_observation_unit_price_nonnegative"),
        ),
        sa.ForeignKeyConstraint(
            ["offer_source_id"],
            ["offer_source.id"],
            name=op.f("fk_price_observation_offer_source_id_offer_source"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_price_observation")),
    )
    op.create_index(
        "ix_price_observation_source_observed",
        "price_observation",
        ["offer_source_id", "observed_at"],
        unique=False,
    )

    op.execute("""
        CREATE FUNCTION reject_price_observation_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'price_observation is append-only'
                USING ERRCODE = '23514';
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER price_observation_append_only
        BEFORE UPDATE OR DELETE ON price_observation
        FOR EACH ROW EXECUTE FUNCTION reject_price_observation_mutation()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER price_observation_append_only ON price_observation")
    op.execute("DROP FUNCTION reject_price_observation_mutation()")
    op.drop_index("ix_price_observation_source_observed", table_name="price_observation")
    op.drop_table("price_observation")
    op.drop_index("ix_purchase_consumable_purchased", table_name="purchase")
    op.drop_table("purchase")
    op.drop_table("offer_source")
    op.drop_index(op.f("ix_consumable_household_id"), table_name="consumable")
    op.drop_table("consumable")
    op.drop_table("telegram_update_receipt")
    op.drop_index("ix_scheduled_job_state_run_at", table_name="scheduled_job")
    op.drop_index("ix_scheduled_job_lease_expires_at", table_name="scheduled_job")
    op.drop_table("scheduled_job")
    op.drop_index("ix_notification_outbox_state_run_at", table_name="notification_outbox")
    op.drop_index("ix_notification_outbox_lease_expires_at", table_name="notification_outbox")
    op.drop_table("notification_outbox")
    op.drop_table("household")
