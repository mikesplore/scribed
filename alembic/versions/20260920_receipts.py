"""Add persisted invoice receipt fields."""
from alembic import op
import sqlalchemy as sa
revision = "20260920_receipts"
down_revision = "20260913_current_schema"
branch_labels = None
depends_on = None
def upgrade() -> None:
    op.add_column("invoices", sa.Column("receipt_path", sa.Text(), nullable=True))
    op.add_column("invoices", sa.Column("receipt_number", sa.String(50), nullable=True))
    op.create_unique_constraint("uq_invoices_receipt_number", "invoices", ["receipt_number"])
    op.create_table("payments",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("invoice_id", sa.Integer, nullable=False),
        sa.Column("provider", sa.String(30), nullable=False),
        sa.Column("provider_reference", sa.String(255), nullable=False, unique=True),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("paid_at", sa.DateTime, nullable=False),
        sa.Column("receipt_number", sa.String(60), nullable=False, unique=True),
        sa.Column("receipt_path", sa.Text, nullable=False),
        sa.Column("idempotency_key", sa.String(255), unique=True),
    )
def downgrade() -> None:
    op.drop_constraint("uq_invoices_receipt_number", "invoices", type_="unique")
    op.drop_column("invoices", "receipt_number")
    op.drop_column("invoices", "receipt_path")
    op.drop_table("payments")
