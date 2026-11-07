"""Register credentialed API clients and attribute their documents."""
from alembic import op
import sqlalchemy as sa


revision = "20261106_registered_api_clients"
down_revision = "20261105_document_only_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "api_clients" not in tables:
        op.create_table(
            "api_clients",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("address", sa.String(1000)),
            sa.Column("contact_email", sa.String(254)),
            sa.Column("phone", sa.String(40)),
            sa.Column("token_hash", sa.String(64), nullable=False),
            sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime, nullable=False),
        )
        op.create_index("ix_api_clients_token_hash", "api_clients", ["token_hash"], unique=True)

    for table in ("contracts", "invoices"):
        columns = {item["name"] for item in sa.inspect(bind).get_columns(table)}
        if "api_client_id" not in columns:
            with op.batch_alter_table(table) as batch:
                batch.add_column(sa.Column("api_client_id", sa.Integer, nullable=True))
            op.create_index(f"ix_{table}_api_client_id", table, ["api_client_id"])


def downgrade() -> None:
    for table in ("contracts", "invoices"):
        indexes = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes(table)}
        if f"ix_{table}_api_client_id" in indexes:
            op.drop_index(f"ix_{table}_api_client_id", table_name=table)
        columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}
        if "api_client_id" in columns:
            with op.batch_alter_table(table) as batch:
                batch.drop_column("api_client_id")
    op.drop_index("ix_api_clients_token_hash", table_name="api_clients")
    op.drop_table("api_clients")
