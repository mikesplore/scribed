from decimal import Decimal

import pytest
from fastapi import HTTPException

from app import db
from app.main import (create_invoice, ensure_gatekeeper_invoice, get_payment_receipt,
                      record_gatekeeper_payment, verify_document)
from app.models import Invoice, InvoiceAdjustment, Payment
from app.schemas import InvoiceRequest


def test_payment_event_creates_receipt_and_supports_partial_payment(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    monkeypatch.setenv("SCRIBED_INTEGRATION_SECRET", "secret")
    db.init_db(); session = db.SessionLocal()
    create_invoice(InvoiceRequest(client_name="Ada", project_name="Payments", description="Build", amount=100, gatekeeper_project_id="p1"), session)
    invoice = session.query(Invoice).first()
    first = record_gatekeeper_payment({"project_id": "p1", "provider": "mpesa", "provider_reference": "r1", "amount": "40", "currency": "KES"}, session, "secret")
    assert first["status"] == "partially_paid"
    second = record_gatekeeper_payment({"project_id": "p1", "provider": "mpesa", "provider_reference": "r2", "amount": "60", "currency": "KES"}, session, "secret")
    assert second["status"] == "paid"
    payment = session.query(Payment).filter_by(provider_reference="r2").one()
    assert get_payment_receipt(payment.id, session).body.startswith(b"%PDF")
    invoice = session.query(Invoice).filter_by(gatekeeper_project_id="p1").one()
    assert invoice.outstanding_balance == Decimal("0.00")
    assert verify_document(invoice.invoice_number, session)["hash_valid"] is True


def test_project_invoice_prefill_tracks_total_paid_and_remaining(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    monkeypatch.setenv("SCRIBED_INTEGRATION_SECRET", "secret")
    db.init_db()
    session = db.SessionLocal()
    create_invoice(InvoiceRequest(client_name="Ada", project_name="Partial project", description="Build",
                                  amount=60, original_amount=100, amount_paid=40,
                                  gatekeeper_project_id="partial-project"), session)
    invoice = session.query(Invoice).filter_by(gatekeeper_project_id="partial-project").one()
    assert invoice.amount == Decimal("100.00")
    assert invoice.total_payments == Decimal("40.00")
    assert invoice.outstanding_balance == Decimal("60.00")
    result = record_gatekeeper_payment({"project_id": "partial-project", "provider_reference": "remainder",
                                        "amount": "60", "currency": "KES"}, session, "secret")
    assert result["status"] == "paid"
    assert invoice.outstanding_balance == Decimal("0.00")


def test_payment_event_rejects_currency_and_overpayment(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path)); monkeypatch.setenv("SCRIBED_INTEGRATION_SECRET", "secret")
    db.init_db(); session = db.SessionLocal()
    create_invoice(InvoiceRequest(client_name="Ada", project_name="Payments", description="Build", amount=100, gatekeeper_project_id="p2"), session)
    with pytest.raises(HTTPException) as currency:
        record_gatekeeper_payment({"project_id": "p2", "provider_reference": "r3", "amount": "10", "currency": "USD"}, session, "secret")
    assert currency.value.status_code == 409
    with pytest.raises(HTTPException) as amount:
        record_gatekeeper_payment({"project_id": "p2", "provider_reference": "r4", "amount": "101", "currency": "KES"}, session, "secret")
    assert amount.value.status_code == 409


def test_invoice_ensure_is_idempotent_and_preserves_payments_and_adjustments(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    monkeypatch.setenv("SCRIBED_INTEGRATION_SECRET", "secret")
    db.init_db()
    session = db.SessionLocal()
    create_invoice(InvoiceRequest(client_name="Ada", project_name="Synced project", description="Build",
                                  amount=100, gatekeeper_project_id="synced-project"), session)
    invoice = session.query(Invoice).one()
    record_gatekeeper_payment(
        {"project_id": "synced-project", "provider": "mpesa", "provider_reference": "sync-payment",
         "amount": "40", "currency": "KES"},
        session, "secret",
    )
    session.add(InvoiceAdjustment(invoice_id=invoice.id, gatekeeper_adjustment_id="sync-adjustment",
                                  type="ADDITIONAL_CHARGE", amount=10, reason="Extra scope", actor="admin"))
    session.commit()
    request = InvoiceRequest(client_name="Ada", project_name="Synced project", description="Build",
                             amount=100, original_amount=100, amount_paid=40,
                             gatekeeper_project_id="synced-project")

    first = ensure_gatekeeper_invoice(request, session, "secret")
    second = ensure_gatekeeper_invoice(request, session, "secret")

    session.expire_all()
    invoice = session.query(Invoice).one()
    assert first["invoice"]["id"] == second["invoice"]["id"] == invoice.id
    assert second["idempotent"] is True
    assert session.query(Payment).filter_by(invoice_id=invoice.id).count() == 1
    assert session.query(InvoiceAdjustment).filter_by(invoice_id=invoice.id).count() == 1
    assert invoice.base_amount == Decimal("100.00")
    assert invoice.total_adjustments == Decimal("10.00")
    assert invoice.amount == Decimal("110.00")
    assert invoice.total_payments == Decimal("40.00")
    assert invoice.outstanding_balance == Decimal("70.00")
