import hashlib
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import db
from app.main import create_receipt, generate_receipt, get_receipt, verify_document
from app.models import Receipt
from app.schemas import ReceiptRequest


def request_data(**overrides):
    data = {"client_name": "Ada Lovelace", "client_email": "ada@example.com",
            "project_name": "Website", "description": "First project payment",
            "amount": "1200.00", "currency": "KES", "received_date": "2026-11-05",
            "payment_method": "Bank transfer", "payment_reference": "TX-123"}
    data.update(overrides)
    return ReceiptRequest(**data)


def test_receipt_preview_renders_pdf():
    response = generate_receipt(request_data())
    assert response.media_type == "application/pdf"
    assert response.body.startswith(b"%PDF")


def test_receipt_is_persisted_issued_immutable_and_retrievable(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    db.init_db()
    session = db.SessionLocal()
    response = create_receipt(request_data(), session, idempotency_key="receipt-1")
    receipt = session.query(Receipt).one()
    stored = Path(receipt.pdf_path).read_bytes()

    assert response.body == stored
    assert response.headers["x-document-id"] == str(receipt.id)
    assert response.headers["x-document-number"] == receipt.receipt_number
    assert response.headers["location"] == f"/receipts/{receipt.id}"
    assert stored.startswith(b"%PDF")
    assert receipt.receipt_number == "MK-REC-0001"
    assert receipt.status == "issued"
    assert receipt.pdf_hash == hashlib.sha256(stored).hexdigest()
    assert get_receipt(receipt.id, session).body == stored
    assert verify_document(receipt.receipt_number, session)["hash_valid"] is True

    replay = create_receipt(request_data(), session, idempotency_key="receipt-1")
    assert replay.body == stored
    assert session.query(Receipt).count() == 1
    with pytest.raises(HTTPException) as error:
        create_receipt(request_data(amount="1201"), session, idempotency_key="receipt-1")
    assert error.value.status_code == 409


def test_client_scoped_receipt_snapshots_registered_identity(monkeypatch, tmp_path):
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    db.init_db()
    session = db.SessionLocal()
    client = {"id": 42, "name": "Example Studio", "address": "1 Main Street",
              "contact_email": "studio@example.com", "phone": None}
    response = create_receipt(request_data(), session, idempotency_key="client-receipt-1", api_client=client)
    receipt = session.query(Receipt).one()
    assert response.body.startswith(b"%PDF")
    assert receipt.api_client_id == 42
    assert '"name": "Example Studio"' in receipt.terms_json
    assert get_receipt(receipt.id, session, client).body == response.body


def test_receipt_validation_rejects_nonpositive_or_overprecision_amounts():
    with pytest.raises(ValueError):
        request_data(amount="0")
    with pytest.raises(ValueError):
        request_data(amount="1.001")


def test_receipt_issuance_requires_idempotency_key():
    db.init_db()
    session = db.SessionLocal()
    with pytest.raises(HTTPException) as error:
        create_receipt(request_data(), session)
    assert error.value.status_code == 400
