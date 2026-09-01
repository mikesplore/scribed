"""Link invoices to Gatekeeper projects."""

from alembic import op
import sqlalchemy as sa

revision = "20260902_invoice_project_link"
down_revision = "20260902_idempotency_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("invoices")}
    if "gatekeeper_project_id" not in columns:
        op.add_column("invoices", sa.Column("gatekeeper_project_id", sa.String(64), nullable=True))
    indexes = {index["name"] for index in inspector.get_indexes("invoices")}
    if "ix_invoices_gatekeeper_project_id" not in indexes:
        op.create_index("ix_invoices_gatekeeper_project_id", "invoices", ["gatekeeper_project_id"])


def downgrade() -> None:
    op.drop_index("ix_invoices_gatekeeper_project_id", table_name="invoices")
    op.drop_column("invoices", "gatekeeper_project_id")
