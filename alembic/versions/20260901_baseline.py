"""baseline schema"""
from alembic import op
import sqlalchemy as sa

revision = "20260901_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())
    if "contracts" not in existing:
        op.create_table("contracts", sa.Column("id", sa.Integer, primary_key=True), sa.Column("contract_number", sa.String(40), nullable=False), sa.Column("client_name", sa.String(255), nullable=False), sa.Column("project_name", sa.String(255), nullable=False), sa.Column("gatekeeper_project_id", sa.String(64)), sa.Column("terms_json", sa.Text, nullable=False), sa.Column("status", sa.String(20), nullable=False), sa.Column("created_at", sa.DateTime, nullable=False), sa.Column("pdf_path", sa.Text, nullable=False), sa.Column("pdf_hash", sa.String(64)), sa.Column("accepted_at", sa.DateTime), sa.Column("archived_at", sa.DateTime), sa.UniqueConstraint("contract_number"))
    if "invoices" not in existing:
        op.create_table("invoices", sa.Column("id", sa.Integer, primary_key=True), sa.Column("invoice_number", sa.String(40), nullable=False), sa.Column("client_name", sa.String(255), nullable=False), sa.Column("client_email", sa.String(255)), sa.Column("amount", sa.Numeric(14, 2), nullable=False), sa.Column("currency", sa.String(3), nullable=False), sa.Column("status", sa.String(20), nullable=False), sa.Column("created_at", sa.DateTime, nullable=False), sa.Column("pdf_path", sa.Text, nullable=False), sa.Column("pdf_hash", sa.String(64)), sa.Column("paid_at", sa.DateTime), sa.Column("archived_at", sa.DateTime), sa.UniqueConstraint("invoice_number"))
    if "document_sequences" not in existing:
        op.create_table("document_sequences", sa.Column("kind", sa.String(20), primary_key=True), sa.Column("value", sa.Integer, nullable=False))
    if "audit_log" not in existing:
        op.create_table("audit_log", sa.Column("id", sa.Integer, primary_key=True), sa.Column("action", sa.String(40), nullable=False), sa.Column("document_type", sa.String(20), nullable=False), sa.Column("document_number", sa.String(50), nullable=False), sa.Column("detail", sa.Text), sa.Column("created_at", sa.DateTime, nullable=False))

    # Bring databases created by the pre-Alembic create_all() implementation
    # up to the current schema as part of the baseline migration.
    for table, columns in {
        "contracts": [("gatekeeper_project_id", sa.String(64)), ("pdf_hash", sa.String(64)), ("accepted_at", sa.DateTime), ("archived_at", sa.DateTime), ("idempotency_key", sa.String(128)), ("request_fingerprint", sa.String(64))],
        "invoices": [("client_email", sa.String(255)), ("pdf_hash", sa.String(64)), ("paid_at", sa.DateTime), ("archived_at", sa.DateTime), ("idempotency_key", sa.String(128)), ("request_fingerprint", sa.String(64))],
    }.items():
        present = {column["name"] for column in sa.inspect(bind).get_columns(table)}
        for name, type_ in columns:
            if name not in present:
                op.add_column(table, sa.Column(name, type_))
    # Idempotency is deliberately enforced by the database, not only in the
    # request handler, so concurrent retries cannot create duplicates.
    for table in ("contracts", "invoices"):
        columns = {column["name"] for column in sa.inspect(bind).get_columns(table)}
        if "idempotency_key" in columns:
            op.create_index(f"uq_{table}_idempotency_key", table, ["idempotency_key"], unique=True)


def downgrade() -> None:
    for table in ("audit_log", "document_sequences", "invoices", "contracts"):
        op.drop_table(table)
