from pathlib import Path
from decimal import Decimal
import pytest
from fastapi import HTTPException

from app import db
from app.main import client_history, create_invoice, duplicate_invoice, get_invoice
from app.models import Invoice
from app.schemas import InvoiceRequest


def test_create_persists_number_and_pdf(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    db.init_db()
    session = db.SessionLocal()
    response = create_invoice(InvoiceRequest(client_name="Ada", project_name="Persistence Site", description="Build", amount=100), session)
    document = session.query(Invoice).order_by(Invoice.id.desc()).first()
    assert response.body.startswith(b"%PDF")
    assert document.invoice_number.startswith("MK-INV-")
    assert Path(document.pdf_path).is_file()
    fetched = get_invoice(document.id, session)
    assert fetched.body == response.body


def test_duplicate_invoice_and_client_history(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    db.init_db()
    session = db.SessionLocal()
    before = client_history("Ada", session)
    original = create_invoice(InvoiceRequest(client_name="Ada", project_name="Duplicate Site", description="Build", amount=100), session)
    source = session.query(Invoice).order_by(Invoice.id.desc()).first()
    with pytest.raises(HTTPException) as error:
        duplicate_invoice(source.id, session)
    history = client_history("Ada", session)
    assert error.value.status_code == 409
    assert len(history["invoices"]) == len(before["invoices"]) + 1
    assert Decimal(history["totals"]["billed"]) - Decimal(before["totals"]["billed"]) == Decimal("100.00")
