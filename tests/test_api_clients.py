import asyncio
import json

from starlette.requests import Request

from app import db
from app.main import (api_token_hash, create_invoice, generate_invoice, get_invoice,
                      get_own_api_client, register_api_client, require_api_token,
                      revoke_api_client, update_own_api_client)
from app.models import APIClient, Invoice
from app.schemas import APIClientRegistration, InvoiceRequest
from starlette.responses import JSONResponse


def make_request(path: str, method: str = "GET", token: str = "",
                 admin: bool = False) -> Request:
    headers = [(b"authorization", f"Bearer {token}".encode())] if token else []
    state = {"is_admin_token": True} if admin else {}
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request({
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
        "query_string": b"", "headers": headers, "client": ("127.0.0.1", 1234),
        "server": ("localhost", 8000), "state": state,
    }, receive)


def create_registered_client(session, name: str, address: str | None = None) -> dict:
    registered = register_api_client(
        APIClientRegistration(name=name, address=address),
        make_request("/api-clients", "POST", admin=True), session)
    return {"id": registered["id"], "name": registered["name"],
            "address": registered["address"], "contact_email": registered["contact_email"],
            "phone": registered["phone"], "api_token": registered["api_token"]}


def test_registered_identity_is_derived_from_token_and_added_to_pdf(monkeypatch):
    db.init_db()
    session = db.SessionLocal()
    registered = create_registered_client(session, "Example Studio", "1 Main Street")
    record = session.get(APIClient, registered["id"])
    assert record.token_hash == api_token_hash(registered["api_token"])
    assert registered["api_token"] not in record.token_hash

    async def route(request):
        assert request.state.api_client["id"] == registered["id"]
        return JSONResponse({"authorized": True})

    async def authenticate():
        response = await require_api_token(
            make_request("/generate/invoice", "POST", registered["api_token"]), route)
        assert response.status_code == 200
        forbidden = await require_api_token(
            make_request("/api-clients", token=registered["api_token"]), route)
        assert forbidden.status_code == 403
        allowed = await require_api_token(
            make_request("/api-clients/me", token=registered["api_token"]), route)
        assert allowed.status_code == 200

    asyncio.run(authenticate())

    rendered = {}
    monkeypatch.setattr("app.main.render_pdf", lambda _template, fields: rendered.update(fields) or b"%PDF-test")
    response = generate_invoice(InvoiceRequest(client_name="Ada", project_name="Website",
                                               description="Build", amount=1000), registered)
    assert response.body == b"%PDF-test"
    assert rendered["prepared_on_behalf_of"] == {
        "name": "Example Studio", "address": "1 Main Street",
        "contact_email": None, "phone": None,
    }


def test_persisted_documents_are_scoped_and_revocation_disables_credentials(monkeypatch):
    db.init_db()
    session = db.SessionLocal()
    first = create_registered_client(session, "First Studio")
    second = create_registered_client(session, "Second Studio")
    monkeypatch.setattr("app.main.render_pdf", lambda *_args: b"%PDF-test")

    create_invoice(InvoiceRequest(client_name="Ada", project_name="Private Website",
                                  description="Build", amount=1000), session,
                   api_client=first)
    invoice = session.query(Invoice).one()
    assert invoice.api_client_id == first["id"]
    assert json.loads(invoice.terms_json)["prepared_on_behalf_of"]["name"] == "First Studio"
    assert get_invoice(invoice.id, session, first).body == b"%PDF-test"
    try:
        get_invoice(invoice.id, session, second)
    except Exception as error:
        assert getattr(error, "status_code", None) == 404
    else:
        raise AssertionError("A different API client retrieved another client's invoice")

    revoked = revoke_api_client(first["id"], make_request("/api-clients/1", "DELETE", admin=True), session)
    assert revoked["is_active"] is False

    async def authenticate_revoked():
        response = await require_api_token(
            make_request(f"/invoices/{invoice.id}", token=first["api_token"]),
            lambda _request: JSONResponse({"unexpected": True}))
        assert response.status_code == 401

    asyncio.run(authenticate_revoked())


def test_scoped_client_can_read_and_update_only_its_own_profile():
    db.init_db()
    session = db.SessionLocal()
    registered = create_registered_client(session, "Gatekeeperd")
    request = make_request("/api-clients/me", token=registered["api_token"])
    request.state.api_client = {key: registered[key] for key in ("id", "name", "address", "contact_email", "phone")}
    assert get_own_api_client(request)["name"] == "Gatekeeperd"

    updated = update_own_api_client(
        APIClientRegistration(name="Gatekeeperd", address="Nairobi", contact_email="billing@example.com"),
        request, session)
    assert updated == {
        "id": registered["id"], "name": "Gatekeeperd", "address": "Nairobi",
        "contact_email": "billing@example.com", "phone": None,
    }
    assert session.get(APIClient, registered["id"]).address == "Nairobi"


def test_admin_token_cannot_read_or_edit_a_scoped_client_profile():
    request = make_request("/api-clients/me", token="admin-token", admin=True)
    try:
        get_own_api_client(request)
    except Exception as error:
        assert getattr(error, "status_code", None) == 403
    else:
        raise AssertionError("Admin tokens must not be treated as a scoped client identity")

