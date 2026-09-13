"""Current Scribed database schema.

This is the sole baseline migration. It creates the complete schema for a new
database and is intentionally non-destructive for an existing database.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260913_current_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    if "contracts" not in existing:
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
    if "invoices" not in existing:
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
    if "document_sequences" not in existing:
        op.create_table(
            "document_sequences",
            sa.Column("kind", sa.String(20), primary_key=True),
            sa.Column("value", sa.Integer, nullable=False, server_default="0"),
        )
    if "audit_log" not in existing:
        op.create_table(
            "audit_log",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("action", sa.String(40), nullable=False),
            sa.Column("document_type", sa.String(20), nullable=False),
            sa.Column("document_number", sa.String(50), nullable=False),
            sa.Column("detail", sa.Text),
            sa.Column("created_at", sa.DateTime, nullable=False),
        )

    for table, name, columns in (
        ("contracts", "ix_contracts_contract_number", ["contract_number"]),
        ("contracts", "ix_contracts_gatekeeper_project_id", ["gatekeeper_project_id"]),
        ("invoices", "ix_invoices_invoice_number", ["invoice_number"]),
        ("invoices", "ix_invoices_gatekeeper_project_id", ["gatekeeper_project_id"]),
    ):
        indexes = {index["name"] for index in sa.inspect(bind).get_indexes(table)}
        if name not in indexes:
            op.create_index(name, table, columns)


def downgrade() -> None:
    for table in ("audit_log", "document_sequences", "invoices", "contracts"):
        op.drop_table(table)
