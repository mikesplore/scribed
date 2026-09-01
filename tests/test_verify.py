from app import db
from app.main import create_invoice, verify_document
from app.schemas import InvoiceRequest


def test_verify_confirms_pdf_integrity(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    db.init_db()
    session = db.SessionLocal()
    response = create_invoice(InvoiceRequest(client_name="Ada", project_name="Site", description="Build", amount=100), session)
    document = session.query(__import__("app.models", fromlist=["Invoice"]).Invoice).order_by(__import__("app.models", fromlist=["Invoice"]).Invoice.id.desc()).first()
    result = verify_document(document.invoice_number, session)
    assert response.body.startswith(b"%PDF")
    assert len(result["hash"]) == 64
    assert result["hash_valid"] is True
