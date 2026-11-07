import asyncio

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app import db
from app.main import MAX_BODY, mark_contract_accepted, require_api_token, send_contract
from app.models import Contract
from app.schemas import ContractRequest


def make_request(body: bytes = b"", headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": "/test",
        "raw_path": b"/test", "query_string": b"", "headers": headers or [],
        "client": ("127.0.0.1", 1234), "server": ("localhost", 8000),
    }

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def test_chunked_request_body_is_limited_without_content_length():
    body = b"x" * (MAX_BODY + 1)

    async def run():
        response = await require_api_token(make_request(body), lambda _request: None)
        assert response.status_code == 413

    asyncio.run(run())


def test_chunked_request_body_remains_available_to_the_route(monkeypatch):
    body = b'{"update_id":1}'
    monkeypatch.setenv("SCRIBED_API_TOKEN", "test-token")

    async def route(request):
        assert await request.body() == body
        from starlette.responses import JSONResponse
        return JSONResponse({"ok": True})

    async def run():
        request = make_request(body, [(b"authorization", b"Bearer test-token")])
        response = await require_api_token(request, route)
        assert response.status_code == 200

    asyncio.run(run())


def test_non_ascii_bearer_token_is_rejected_without_internal_error(monkeypatch):
    monkeypatch.setenv("SCRIBED_API_TOKEN", "admin-token")

    class Query:
        def filter_by(self, **_kwargs):
            return self

        def first(self):
            return None

    class Session:
        def query(self, _model):
            return Query()

        def close(self):
            pass

    monkeypatch.setattr("app.main.SessionLocal", Session)

    async def route(_request):
        raise AssertionError("Unauthorized request must not reach the route")

    async def run():
        request = make_request(headers=[("authorization", "Bearer admín-token".encode("utf-8"))])
        response = await require_api_token(request, route)
        assert response.status_code == 401

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


