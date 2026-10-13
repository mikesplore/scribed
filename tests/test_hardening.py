import asyncio

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app import db
from app.main import (MAX_BODY, mark_contract_accepted, require_api_token, send_contract,
                      sync_gatekeeper_ledger, telegram_webhook)
from app.models import Contract, Invoice
from app.schemas import ContractRequest, InvoiceRequest, LedgerSyncRequest


def make_request(body: bytes = b"", headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": "/telegram/webhook",
        "raw_path": b"/telegram/webhook", "query_string": b"", "headers": headers or [],
        "client": ("127.0.0.1", 1234), "server": ("localhost", 8000),
    }

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def test_webhook_requires_configured_secret(monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, "telegram_application", object())
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)

    async def run():
        with pytest.raises(HTTPException) as error:
            await telegram_webhook(make_request())
        assert error.value.status_code == 503

    asyncio.run(run())


def test_chunked_request_body_is_limited_without_content_length():
    body = b"x" * (MAX_BODY + 1)

    async def run():
        response = await require_api_token(make_request(body), lambda _request: None)
        assert response.status_code == 413

    asyncio.run(run())


def test_chunked_request_body_remains_available_to_the_route():
    body = b'{"update_id":1}'

    async def route(request):
        assert await request.body() == body
        from starlette.responses import JSONResponse
        return JSONResponse({"ok": True})

    async def run():
        response = await require_api_token(make_request(body), route)
        assert response.status_code == 200

    asyncio.run(run())


def test_failed_email_delivery_does_not_mark_contract_sent(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    db.init_db()
    session = db.SessionLocal()
    from app.main import create_contract
    request = ContractRequest(client_name="Ada", client_email="ada@example.com", project_name="Email fail",
                              scope="Build a site", deliverables=["Site"], timeline="4 weeks", amount=100,
                              payment_schedule="Due on delivery")
    create_contract(request, session)
    contract = session.query(Contract).one()

    def fail_send(*_args):
        raise OSError("mail service unavailable")

    monkeypatch.setattr("app.main.send_pdf", fail_send)
    with pytest.raises(OSError):
        send_contract(contract.id, session)
    session.refresh(contract)
    assert contract.status == "draft"
    assert contract.pdf_path is None
    assert contract.pdf_hash is None


def test_contract_cannot_be_accepted_before_send(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    db.init_db()
    session = db.SessionLocal()
    from app.main import create_contract
    create_contract(ContractRequest(client_name="Ada", project_name="Not sent", scope="Build",
                                    deliverables=["Site"], timeline="4 weeks", amount=100,
                                    payment_schedule="Due on delivery"), session)
    contract = session.query(Contract).one()
    with pytest.raises(HTTPException) as error:
        mark_contract_accepted(contract.id, session)
    assert error.value.status_code == 409


def test_ledger_rejects_stale_version_and_accepts_same_version_replay(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    monkeypatch.setenv("SCRIBED_INTEGRATION_SECRET", "secret")
    db.init_db()
    session = db.SessionLocal()
    from app.main import create_invoice
    create_invoice(InvoiceRequest(client_name="Ada", project_name="Ledger", description="Build", amount=100,
                                  gatekeeper_project_id="ledger-project"), session)
    payload = LedgerSyncRequest(project_id="ledger-project", ledger_version="2", base_amount=100,
                                outstanding_balance=100, currency="KES")
    result = sync_gatekeeper_ledger(payload, session, "secret")
    assert result["idempotent"] is False
    assert sync_gatekeeper_ledger(payload, session, "secret")["idempotent"] is True
    stale = LedgerSyncRequest(project_id="ledger-project", ledger_version="1", base_amount=100,
                              successful_payments=10, outstanding_balance=90, currency="KES")
    with pytest.raises(HTTPException) as error:
        sync_gatekeeper_ledger(stale, session, "secret")
    assert error.value.status_code == 409
    invoice = session.query(Invoice).one()
    assert invoice.outstanding_balance == 100
