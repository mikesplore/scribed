"""Store invoice input snapshots for safe duplication."""

from alembic import op
import sqlalchemy as sa

revision = "20260902_invoice_snapshot"
down_revision = "20260902_invoice_context"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("invoices")}
    if "terms_json" not in columns:
        op.add_column("invoices", sa.Column("terms_json", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("invoices", "terms_json")
