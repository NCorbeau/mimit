"""Record explicit outcomes and bounded product snapshots in append-only history."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_product_observations"
down_revision = "0002_telegram_conversation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Defaults backfill old observations without UPDATE, preserving the history trigger.
    op.add_column(
        "price_observation",
        sa.Column("outcome", sa.String(16), nullable=False, server_default="success"),
    )
    op.add_column("price_observation", sa.Column("error_code", sa.String(32), nullable=True))
    op.add_column("price_observation", sa.Column("product_name", sa.Text(), nullable=True))
    op.add_column("price_observation", sa.Column("variant_snapshot", sa.Text(), nullable=True))
    op.add_column(
        "price_observation",
        sa.Column("extraction_metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
    )
    constraints = {
        "outcome": "outcome IN ('success', 'failed')",
        "outcome_shape": (
            "(outcome = 'success' AND error_code IS NULL) OR "
            "(outcome = 'failed' AND error_code IS NOT NULL AND "
            "price IS NULL AND currency IS NULL AND unit_price IS NULL AND unit IS NULL "
            "AND product_name IS NULL AND availability = 'unknown')"
        ),
        "error_code": (
            "error_code IN ('unsafe_url', 'unsafe_address', 'redirect_limit', 'timeout', "
            "'http_error', 'body_too_large', 'unsupported_content', 'transport_error', "
            "'rate_limited', 'invalid_encoding', 'unsupported_source', 'invalid_jsonld', "
            "'ambiguous_product', 'ambiguous_offer', 'invalid_price', 'identity_mismatch', "
            "'invalid_product', 'no_product')"
        ),
        "product_name": (
            "product_name IS NULL OR (length(trim(product_name)) > 0 "
            "AND length(product_name) <= 512)"
        ),
        "variant_snapshot": "variant_snapshot IS NULL OR length(variant_snapshot) <= 1024",
        "extraction_metadata": (
            "jsonb_typeof(extraction_metadata) = 'object' "
            "AND octet_length(extraction_metadata::text) <= 4096"
        ),
    }
    for name, expression in constraints.items():
        op.create_check_constraint(
            op.f(f"ck_price_observation_{name}"), "price_observation", expression
        )


def downgrade() -> None:
    for name in [
        "outcome",
        "outcome_shape",
        "error_code",
        "product_name",
        "variant_snapshot",
        "extraction_metadata",
    ]:
        op.drop_constraint(op.f(f"ck_price_observation_{name}"), "price_observation", type_="check")
    for name in [
        "outcome",
        "error_code",
        "product_name",
        "variant_snapshot",
        "extraction_metadata",
    ]:
        op.drop_column("price_observation", name)
