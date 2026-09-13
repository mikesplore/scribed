"""Rebuild the application schema from a clean slate.

This service has no supported data-preserving migration path yet.  Keeping a
single migration makes a new deployment deterministic and prevents old test
data or abandoned schema revisions from surviving a deploy.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260913_clean_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop in dependency order.  IF EXISTS is expressed through inspection so
    # this works for both a fresh database and an existing deployment.
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    for table in ("audit_log", "document_sequences", "invoices", "contracts"):
        if table in tables:
            op.drop_table(table)

    op.create_table(
        "contracts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("contract_number", sa.String(40), nullable=False),
        sa.Column("client_name", sa.String(255), nullable=False),
        sa.Column("project_name", sa.String(255), nullable=False),
        sa.Column("gatekeeper_project_id", sa.String(64)),
        sa.Column("terms_json", sa.Text, nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("pdf_path", sa.Text, nullable=False),
        sa.Column("pdf_hash", sa.String(64)),
        sa.Column("accepted_at", sa.DateTime),
        sa.Column("archived_at", sa.DateTime),
        sa.Column("idempotency_key", sa.String(128), unique=True),
        sa.Column("request_fingerprint", sa.String(64)),
        sa.UniqueConstraint("contract_number"),
    )
    op.create_index("ix_contracts_contract_number", "contracts", ["contract_number"], unique=False)
    op.create_index("ix_contracts_gatekeeper_project_id", "contracts", ["gatekeeper_project_id"], unique=False)

    op.create_table(
        "invoices",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("invoice_number", sa.String(40), nullable=False),
        sa.Column("client_name", sa.String(255), nullable=False),
        sa.Column("project_name", sa.String(255), nullable=False),
        sa.Column("client_email", sa.String(255)),
        sa.Column("gatekeeper_project_id", sa.String(64)),
        sa.Column("terms_json", sa.Text, nullable=False, server_default="{}"),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("pdf_path", sa.Text, nullable=False),
        sa.Column("pdf_hash", sa.String(64)),
        sa.Column("paid_at", sa.DateTime),
        sa.Column("archived_at", sa.DateTime),
        sa.Column("idempotency_key", sa.String(128), unique=True),
        sa.Column("request_fingerprint", sa.String(64)),
        sa.UniqueConstraint("invoice_number"),
    )
    op.create_index("ix_invoices_invoice_number", "invoices", ["invoice_number"], unique=False)
    op.create_index("ix_invoices_gatekeeper_project_id", "invoices", ["gatekeeper_project_id"], unique=False)

    op.create_table(
        "document_sequences",
        sa.Column("kind", sa.String(20), primary_key=True),
        sa.Column("value", sa.Integer, nullable=False, server_default="0"),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("document_type", sa.String(20), nullable=False),
        sa.Column("document_number", sa.String(50), nullable=False),
        sa.Column("detail", sa.Text),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )


def downgrade() -> None:
    for table in ("audit_log", "document_sequences", "invoices", "contracts"):
        op.drop_table(table)
