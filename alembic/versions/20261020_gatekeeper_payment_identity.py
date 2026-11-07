"""Add stable Gatekeeper payment identity for service reconciliation."""
from alembic import op
import sqlalchemy as sa

revision = "20261020_payment_identity"
down_revision = "20261013_draft_pdf_on_demand"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("payments")}
    if "gatekeeper_payment_id" not in columns:
        op.add_column("payments", sa.Column("gatekeeper_payment_id", sa.String(64), nullable=True))
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("payments")}
    if "ix_payments_gatekeeper_payment_id" not in indexes:
        op.create_index("ix_payments_gatekeeper_payment_id", "payments", ["gatekeeper_payment_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_payments_gatekeeper_payment_id", table_name="payments")
    op.drop_column("payments", "gatekeeper_payment_id")
