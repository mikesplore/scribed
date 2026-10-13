"""Allow document drafts to exist without stored PDF files."""

from alembic import op
import sqlalchemy as sa


revision = "20261013_draft_pdf_on_demand"
down_revision = "20260928_service_scoped_invoices"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("contracts") as batch:
        batch.alter_column("pdf_path", existing_type=sa.Text(), nullable=True)
    with op.batch_alter_table("invoices") as batch:
        batch.alter_column("pdf_path", existing_type=sa.Text(), nullable=True)


def downgrade() -> None:
    # Drafts can have NULL pdf_path, so make their placeholder explicit before
    # restoring the old non-null constraint.
    op.execute("UPDATE contracts SET pdf_path = '' WHERE pdf_path IS NULL")
    op.execute("UPDATE invoices SET pdf_path = '' WHERE pdf_path IS NULL")
    with op.batch_alter_table("contracts") as batch:
        batch.alter_column("pdf_path", existing_type=sa.Text(), nullable=False)
    with op.batch_alter_table("invoices") as batch:
        batch.alter_column("pdf_path", existing_type=sa.Text(), nullable=False)
