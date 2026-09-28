"""Add optional Gatekeeper service ownership to invoices."""
from alembic import op
import sqlalchemy as sa

revision = "20260928_service_scoped_invoices"
down_revision = "20260921_ledger_breakdown"
branch_labels = None
depends_on = None

def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("invoices")}
    if "gatekeeper_service_id" not in columns:
        op.add_column("invoices", sa.Column("gatekeeper_service_id", sa.String(64), nullable=True))
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("invoices")}
    if "ix_invoices_gatekeeper_service_id" not in indexes:
        op.create_index("ix_invoices_gatekeeper_service_id", "invoices", ["gatekeeper_service_id"])

def downgrade() -> None:
    op.drop_index("ix_invoices_gatekeeper_service_id", table_name="invoices")
    op.drop_column("invoices", "gatekeeper_service_id")
