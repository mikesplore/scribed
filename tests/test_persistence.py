from pathlib import Path

from app import db
from app.main import create_invoice, get_invoice
from app.models import Invoice
from app.schemas import InvoiceRequest


def test_create_persists_number_and_pdf(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    db.init_db()
    session = db.SessionLocal()
    response = create_invoice(InvoiceRequest(client_name="Ada", project_name="Site", description="Build", amount=100), session)
    document = session.query(Invoice).order_by(Invoice.id.desc()).first()
    assert response.body.startswith(b"%PDF")
    assert document.invoice_number.startswith("MK-INV-")
    assert Path(document.pdf_path).is_file()
    fetched = get_invoice(document.id, session)
    assert fetched.body == response.body
