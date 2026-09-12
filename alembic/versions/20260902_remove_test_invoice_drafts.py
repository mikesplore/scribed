"""Remove draft invoices created by the repository's test fixtures."""

from alembic import op
import sqlalchemy as sa

# Preserve the revision ID already recorded by existing databases.
revision = "20260902_remove_test_invoice_drafts"
down_revision = "20260902_invoice_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The persistence and verification fixtures use this exact invoice data.
    # Restrict the cleanup to drafts so real sent/paid records are untouched.
    op.execute(
        sa.text(
            """DELETE FROM invoices
               WHERE status = 'draft'
                 AND client_name = 'Ada'
                 AND project_name = 'Site'
                 AND amount = 100
                 AND terms_json LIKE '%\"description\": \"Build\"%'"""
        )
    )


def downgrade() -> None:
    # Deleted test data cannot be reconstructed safely.
    pass
