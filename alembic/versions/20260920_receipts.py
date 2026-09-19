"""Add persisted invoice receipt fields."""
from alembic import op
import sqlalchemy as sa
revision = "20260920_receipts"
down_revision = "20260913_current_schema"
branch_labels = None
depends_on = None
def upgrade() -> None:
    bind = op.get_bind(); inspector = sa.inspect(bind)
    columns = {c["name"] for c in inspector.get_columns("invoices")}
    if "receipt_path" not in columns: op.add_column("invoices", sa.Column("receipt_path", sa.Text(), nullable=True))
    if "receipt_number" not in columns: op.add_column("invoices", sa.Column("receipt_number", sa.String(50), nullable=True))
    if "uq_invoices_receipt_number" not in {i["name"] for i in sa.inspect(bind).get_indexes("invoices") if i["name"]}:
        op.create_index("uq_invoices_receipt_number", "invoices", ["receipt_number"], unique=True)
    if "payments" not in inspector.get_table_names():
        op.create_table("payments",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("invoice_id", sa.Integer, nullable=False),
        sa.Column("provider", sa.String(30), nullable=False),
        sa.Column("provider_reference", sa.String(255), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("paid_at", sa.DateTime, nullable=False),
        sa.Column("receipt_number", sa.String(60), nullable=False),
        sa.Column("receipt_path", sa.Text, nullable=False),
            sa.Column("idempotency_key", sa.String(255)),
        )
        op.create_index("ix_payments_provider_reference", "payments", ["provider_reference"], unique=True)
        op.create_index("uq_payments_receipt_number", "payments", ["receipt_number"], unique=True)
        op.create_index("uq_payments_idempotency_key", "payments", ["idempotency_key"], unique=True)
def downgrade() -> None:
    op.drop_index("uq_invoices_receipt_number", table_name="invoices")
    op.drop_column("invoices", "receipt_number")
    op.drop_column("invoices", "receipt_path")
    op.drop_table("payments")
