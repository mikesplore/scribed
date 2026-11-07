"""Add client-scoped, idempotent receipt documents."""
from alembic import op
import sqlalchemy as sa


revision = "20261107_receipts"
down_revision = "20261106_registered_api_clients"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "receipts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("receipt_number", sa.String(40), nullable=False, unique=True),
        sa.Column("client_name", sa.String(255), nullable=False),
        sa.Column("project_name", sa.String(255), nullable=False),
        sa.Column("api_client_id", sa.Integer, nullable=True),
        sa.Column("terms_json", sa.Text, nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("pdf_path", sa.Text, nullable=True),
        sa.Column("pdf_hash", sa.String(64), nullable=True),
        sa.Column("archived_at", sa.DateTime, nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=True, unique=True),
        sa.Column("request_fingerprint", sa.String(64), nullable=True),
    )
    op.create_index("ix_receipts_receipt_number", "receipts", ["receipt_number"], unique=True)
    op.create_index("ix_receipts_api_client_id", "receipts", ["api_client_id"])


def downgrade() -> None:
    op.drop_index("ix_receipts_api_client_id", table_name="receipts")
    op.drop_index("ix_receipts_receipt_number", table_name="receipts")
    op.drop_table("receipts")
