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
def downgrade() -> None:
    op.drop_constraint("uq_invoices_receipt_number", "invoices", type_="unique")
    op.drop_column("invoices", "receipt_number")
    op.drop_column("invoices", "receipt_path")
