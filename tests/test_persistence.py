from pathlib import Path
from decimal import Decimal
from app import db
from app.main import (client_history, create_contract, create_invoice, duplicate_invoice,
                      get_contract, get_invoice, send_contract, send_invoice, verify_document)
from app.models import Invoice
from app.schemas import ContractRequest, InvoiceRequest


def test_create_persists_details_and_renders_pdf_without_storing_draft(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    db.init_db()
    session = db.SessionLocal()
    response = create_invoice(InvoiceRequest(client_name="Ada", project_name="Persistence Site", description="Build", amount=100), session)
    document = session.query(Invoice).order_by(Invoice.id.desc()).first()
    assert response.body.startswith(b"%PDF")
    assert document.invoice_number.startswith("MK-INV-")
    assert document.pdf_path is None
    assert document.pdf_hash is None
    assert list(tmp_path.rglob("*.pdf")) == []
    fetched = get_invoice(document.id, session)
    assert fetched.body.startswith(b"%PDF")
    assert list(tmp_path.rglob("*.pdf")) == []


def test_duplicate_invoice_and_client_history(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    db.init_db()
    session = db.SessionLocal()
    before = client_history("Ada", session)
    original = create_invoice(InvoiceRequest(client_name="Ada", project_name="Duplicate Site", description="Build", amount=100), session)
    source = session.query(Invoice).order_by(Invoice.id.desc()).first()
    duplicate = duplicate_invoice(source.id, session)
    history = client_history("Ada", session)
    documents = session.query(Invoice).order_by(Invoice.id.asc()).all()
    assert duplicate.body.startswith(b"%PDF")
    assert len(history["invoices"]) == len(before["invoices"]) + 2
    assert documents[0].pdf_path is None
    assert documents[1].pdf_path is None
    assert Decimal(history["totals"]["billed"]) - Decimal(before["totals"]["billed"]) == Decimal("200.00")


def test_draft_invoice_creation_does_not_store_pdf(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    db.init_db()
    session = db.SessionLocal()
    create_invoice(InvoiceRequest(client_name="Ada", project_name="A/B", description="First", amount=100), session)
    create_invoice(InvoiceRequest(client_name="Ada", project_name="A B", description="Second", amount=200), session)
    documents = session.query(Invoice).order_by(Invoice.id.asc()).all()
    assert all(document.pdf_path is None for document in documents)
    assert list(tmp_path.rglob("*.pdf")) == []


def test_draft_creation_does_not_upload_pdf(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    db.init_db()
    session = db.SessionLocal()

    create_invoice(InvoiceRequest(client_name="Ada", project_name="No draft upload", description="Build", amount=100,
                                  client_email="ada@example.com"), session)
    invoice = session.query(Invoice).one()
    monkeypatch.setattr("app.main.send_pdf", lambda *_args: None)
    send_invoice(invoice.id, session)
    session.refresh(invoice)
    assert invoice.pdf_path and Path(invoice.pdf_path).is_file()
    assert Path(invoice.pdf_path).read_bytes().startswith(b"%PDF")
    assert verify_document(invoice.invoice_number, session)["hash_valid"] is True
    assert get_invoice(invoice.id, session).body == Path(invoice.pdf_path).read_bytes()


def test_contract_pdf_is_saved_only_when_sent_and_matches_emailed_file(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    db.init_db()
    session = db.SessionLocal()
    request = ContractRequest(client_name="Ada", client_email="ada@example.com", project_name="Issued contract",
                              scope="Build a site", deliverables=["Site"], timeline="4 weeks", amount=100,
                              payment_schedule="Due on delivery")
    preview = create_contract(request, session)
    from app.models import Contract
    contract = session.query(Contract).one()
    assert preview.body.startswith(b"%PDF")
    assert contract.pdf_path is None

    sent_files = []
    monkeypatch.setattr("app.main.send_pdf", lambda _to, _subject, _name, path: sent_files.append(Path(path).read_bytes()))
    send_contract(contract.id, session)
    session.refresh(contract)
    artifact = Path(contract.pdf_path).read_bytes()
    assert sent_files == [artifact]
    assert get_contract(contract.id, session).body == artifact
    assert verify_document(contract.contract_number, session)["hash_valid"] is True
