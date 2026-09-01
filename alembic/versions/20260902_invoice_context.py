"""Add invoice project context."""

from alembic import op
import sqlalchemy as sa

revision = "20260902_invoice_context"
down_revision = "20260902_invoice_project_link"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("invoices")}
    if "project_name" not in columns:
        op.add_column("invoices", sa.Column("project_name", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("invoices", "project_name")
