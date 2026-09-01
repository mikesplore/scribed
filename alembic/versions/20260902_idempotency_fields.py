"""Add idempotency fields used by document creation endpoints.

This is intentionally separate from the baseline migration because the
baseline may already be recorded as applied in deployed databases.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260902_idempotency_fields"
down_revision = "20260901_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    for table in ("contracts", "invoices"):
        columns = {column["name"] for column in inspector.get_columns(table)}
        if "idempotency_key" not in columns:
            op.add_column(table, sa.Column("idempotency_key", sa.String(128), nullable=True))
        if "request_fingerprint" not in columns:
            op.add_column(table, sa.Column("request_fingerprint", sa.String(64), nullable=True))

        indexes = {index["name"] for index in inspector.get_indexes(table)}
        index_name = f"uq_{table}_idempotency_key"
        if index_name not in indexes:
            op.create_index(index_name, table, ["idempotency_key"], unique=True)


def downgrade() -> None:
    for table in ("contracts", "invoices"):
        op.drop_index(f"uq_{table}_idempotency_key", table_name=table)
        op.drop_column(table, "request_fingerprint")
        op.drop_column(table, "idempotency_key")
