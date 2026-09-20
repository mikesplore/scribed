"""Store Gatekeeper ledger breakdowns and adjustment lines."""
from alembic import op
import sqlalchemy as sa

revision = "20260921_ledger_breakdown"
down_revision = "20260920_receipts"
branch_labels = None
depends_on = None

def upgrade() -> None:
    bind = op.get_bind()
    columns = {c["name"] for c in sa.inspect(bind).get_columns("invoices")}
    for name in ("base_amount", "total_adjustments", "total_payments", "outstanding_balance"):
        if name not in columns:
            op.add_column("invoices", sa.Column(name, sa.Numeric(14, 2), nullable=False, server_default="0"))
    op.execute("UPDATE invoices SET base_amount = amount WHERE base_amount = 0")
    if "payments" in sa.inspect(bind).get_table_names():
        op.execute("""
            UPDATE invoices AS i
            SET total_payments = COALESCE((SELECT SUM(p.amount) FROM payments AS p WHERE p.invoice_id = i.id), 0),
                outstanding_balance = GREATEST(0, i.amount - COALESCE((SELECT SUM(p.amount) FROM payments AS p WHERE p.invoice_id = i.id), 0))
            WHERE i.total_payments = 0 AND i.outstanding_balance = 0
        """)
    if "invoice_adjustments" not in sa.inspect(bind).get_table_names():
        op.create_table("invoice_adjustments",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("invoice_id", sa.Integer, nullable=False),
            sa.Column("gatekeeper_adjustment_id", sa.String(64), nullable=False),
            sa.Column("type", sa.String(20), nullable=False),
            sa.Column("amount", sa.Numeric(14, 2), nullable=False),
            sa.Column("reason", sa.Text, nullable=False),
            sa.Column("actor", sa.String(255), nullable=False),
            sa.Column("created_at", sa.DateTime, nullable=False),
            sa.CheckConstraint("type IN ('ADDITIONAL_CHARGE', 'DISCOUNT')"),
            sa.CheckConstraint("amount > 0"))
        op.create_index("uq_invoice_adjustments_gatekeeper_id", "invoice_adjustments", ["gatekeeper_adjustment_id"], unique=True)

def downgrade() -> None:
    op.drop_table("invoice_adjustments")
    for name in ("base_amount", "total_adjustments", "total_payments", "outstanding_balance"):
        op.drop_column("invoices", name)
