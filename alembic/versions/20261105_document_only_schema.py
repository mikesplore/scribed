"""Remove legacy payment and external project-link schema."""
import json
from alembic import op
import sqlalchemy as sa


revision = "20261105_document_only_schema"
down_revision = "20261020_payment_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    obsolete_terms = {
        "gatekeeper_project_id", "gatekeeper_service_id", "original_amount", "amount_paid",
        "payments", "adjustments", "payment_portal_url", "total_paid", "outstanding_balance",
        "total_adjustments", "_ledger_version", "_ledger_fingerprint", "_ensure_fingerprint",
    }
    for table in ("contracts", "invoices"):
        if table not in tables:
            continue
        rows = bind.execute(sa.text(f"SELECT id, terms_json FROM {table}")).all()
        for row_id, raw_terms in rows:
            try:
                terms = json.loads(raw_terms or "{}")
            except (TypeError, ValueError):
                continue
            changed = False
            for key in obsolete_terms:
                if key in terms:
                    terms.pop(key)
                    changed = True
            if changed:
                bind.execute(sa.text(f"UPDATE {table} SET terms_json = :terms WHERE id = :id"),
                             {"terms": json.dumps(terms), "id": row_id})
    if "invoices" in tables:
        bind.execute(sa.text("UPDATE invoices SET status = 'sent' WHERE status IN ('paid', 'partially_paid')"))
    if "payments" in tables:
        op.drop_table("payments")
    if "invoice_adjustments" in tables:
        op.drop_table("invoice_adjustments")

    for table, columns in {
        "contracts": ["gatekeeper_project_id"],
        "invoices": ["gatekeeper_project_id", "gatekeeper_service_id", "base_amount",
                     "total_adjustments", "total_payments", "outstanding_balance",
                     "paid_at", "receipt_path", "receipt_number"],
    }.items():
        if table not in tables:
            continue
        present = {column["name"] for column in sa.inspect(bind).get_columns(table)}
        if table == "contracts" and "gatekeeper_project_id" in present:
            indexes = {item["name"] for item in sa.inspect(bind).get_indexes(table)}
            if "ix_contracts_gatekeeper_project_id" in indexes:
                op.drop_index("ix_contracts_gatekeeper_project_id", table_name=table)
        if table == "invoices":
            indexes = {item["name"] for item in sa.inspect(bind).get_indexes(table)}
            for name in ("ix_invoices_gatekeeper_project_id", "ix_invoices_gatekeeper_service_id",
                         "uq_invoices_receipt_number"):
                if name in indexes:
                    op.drop_index(name, table_name=table)
        dropping = [name for name in columns if name in present]
        if dropping:
            with op.batch_alter_table(table) as batch:
                for name in dropping:
                    batch.drop_column(name)


def downgrade() -> None:
    # Removed ledger and receipt records are intentionally not reconstructed.
    raise RuntimeError("The document-only schema migration is irreversible")
