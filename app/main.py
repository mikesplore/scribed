import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from decimal import Decimal

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from sqlalchemy import func
from sqlalchemy.orm import Session

load_dotenv()

from .db import SessionLocal, get_db, next_number
from .mailer import send_pdf
from .models import APIClient, AuditLog, Contract, Invoice, Receipt
from .render import render_pdf
from .schemas import APIClientRegistration, ContractRequest, InvoiceRequest, ReceiptRequest
from .storage import delete_pdf, read_pdf, upload_pdf

logger = logging.getLogger(__name__)
production = os.getenv("ENVIRONMENT", "development").lower() == "production"
public_url = os.getenv("SCRIBED_PUBLIC_URL", "https://scribed.mikesplore.me").strip().rstrip("/")
app = FastAPI(title="Scribed", description="mikesplore contract and invoice PDF generation",
              docs_url=None if production else "/docs", redoc_url=None if production else "/redoc",
              openapi_url=None if production else "/openapi.json")
cors_origins = [origin.strip() for origin in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",") if origin.strip()]
app.add_middleware(CORSMiddleware, allow_origins=cors_origins, allow_credentials=False,
                   allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
                   allow_headers=["Authorization", "Content-Type", "Idempotency-Key"])
MAX_BODY = 400 * 1024
_requests: dict[str, deque[float]] = defaultdict(deque)


def audit(db, action: str, kind: str, number: str, detail: str | None = None):
    db.add(AuditLog(action=action, document_type=kind, document_number=number, detail=detail))


def invoice_render_data(invoice: Invoice) -> dict:
    data = json.loads(invoice.terms_json or "{}")
    paid_amount = Decimal(str(data.get("paid_amount", 0)))
    raw_balance = data.get("balance_due")
    balance_due = Decimal(str(raw_balance)) if raw_balance is not None else max(invoice.amount - paid_amount, Decimal("0"))
    data.update(invoice_number=invoice.invoice_number, client_name=invoice.client_name,
                project_name=invoice.project_name, client_email=invoice.client_email,
                amount=invoice.amount, currency=invoice.currency,
                paid_amount=paid_amount,
                balance_due=balance_due,
                verification_url=f"{public_url}/verify/{invoice.invoice_number}")
    return data


def contract_render_data(contract: Contract) -> dict:
    data = json.loads(contract.terms_json or "{}")
    data["contract_number"] = contract.contract_number
    return data


def fingerprint(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def api_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def client_presentation(client: dict | None) -> dict | None:
    if not isinstance(client, dict):
        return None
    return {key: client.get(key) for key in ("name", "address", "contact_email", "phone")}


def request_fingerprint(request: ContractRequest | InvoiceRequest | ReceiptRequest,
                        client: dict | None) -> str:
    profile = client if isinstance(client, dict) else None
    return fingerprint({"document": request.model_dump(),
                        "api_client_id": profile.get("id") if profile else None})


def get_api_client(request: Request) -> dict | None:
    return getattr(request.state, "api_client", None)


def require_admin_token(request: Request) -> None:
    if not getattr(request.state, "is_admin_token", False):
        raise HTTPException(status_code=403, detail="Administrator credential required")


def scoped_idempotency_key(key: str | None, api_client: dict | None) -> str | None:
    if not key or not api_client:
        return key
    scoped = hashlib.sha256(f"{api_client['id']}:{key}".encode()).hexdigest()
    return f"client-{scoped}"


def prepared_document_data(request_model: ContractRequest | InvoiceRequest,
                           api_client: dict | None) -> dict:
    data = request_model.model_dump()
    profile = client_presentation(api_client)
    if profile:
        data["prepared_on_behalf_of"] = profile
    return data


def scoped_document_query(query, model, api_client: dict | None):
    if isinstance(api_client, dict):
        return query.filter(model.api_client_id == api_client["id"])
    return query


def pdf_filename(project_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (project_name or "").lower()).strip("-")
    return f"{slug[:80].rstrip('-') or 'mikesplore'}.pdf"


def document_storage_key(kind: str, number: str, content: bytes) -> str:
    digest = hashlib.sha256(content).hexdigest()
    return f"{kind}/{number}-{digest}.pdf"


def existing_project_document(db: Session, model, project_name: str, api_client_id: int | None):
    normalized_name = project_name.strip().lower()
    return db.query(model).filter(func.lower(func.trim(model.project_name)) == normalized_name,
                                   model.api_client_id == api_client_id).first()


@app.middleware("http")
async def require_api_token(request: Request, call_next):
    content_length = request.headers.get("content-length")
    try:
        too_large = content_length is not None and int(content_length) > MAX_BODY
    except ValueError:
        return Response(content='{"detail":"Invalid Content-Length"}', status_code=400,
                        media_type="application/json")
    if too_large:
        return Response(content='{"detail":"Payload Too Large"}', status_code=413,
                        media_type="application/json")
    if request.method in {"POST", "PUT", "PATCH"} and content_length is None:
        body = await request.body()
        if len(body) > MAX_BODY:
            return Response(content='{"detail":"Payload Too Large"}', status_code=413,
                            media_type="application/json")
        body_sent = False

        async def replay_body():
            nonlocal body_sent
            if body_sent:
                return {"type": "http.request", "body": b"", "more_body": False}
            body_sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        request = Request(request.scope, receive=replay_body)
    path = request.url.path
    expected = os.getenv("SCRIBED_API_TOKEN", "").strip()
    if path in {"/health", "/docs", "/redoc", "/openapi.json"} or path.startswith("/verify/"):
        return await call_next(request)
    authorization = request.headers.get("authorization", "")
    presented_token = authorization[7:] if authorization.startswith("Bearer ") else ""
    is_admin = bool(expected and hmac.compare_digest(presented_token.encode("utf-8"), expected.encode("utf-8")))
    api_client = None
    if presented_token and not is_admin:
        session = SessionLocal()
        try:
            record = session.query(APIClient).filter_by(token_hash=api_token_hash(presented_token),
                                                        is_active=True).first()
            if record:
                api_client = {"id": record.id, "name": record.name, "address": record.address,
                              "contact_email": record.contact_email, "phone": record.phone}
        finally:
            session.close()
    if not is_admin and api_client is None:
        return Response(content='{"detail":"Authentication required"}', status_code=401,
                        media_type="application/json")
    request.state.is_admin_token = is_admin
    request.state.api_client = api_client
    if api_client and not (
        request.method == "POST" and path in {"/generate/contract", "/generate/invoice", "/generate/receipt",
                                               "/contracts", "/invoices", "/receipts"}
        or request.method == "GET" and (
            re.fullmatch(r"/(contracts|invoices)/\d+", path)
            or re.fullmatch(r"/receipts/\d+", path)
            or re.fullmatch(r"/documents/[A-Za-z0-9_-]{1,50}", path)
        )
        or request.method == "GET" and path == "/api-clients/me"
        or request.method == "PATCH" and path == "/api-clients/me"
    ):
        return Response(content='{"detail":"API client is not authorized for this operation"}',
                        status_code=403, media_type="application/json")
    key = request.client.host if request.client else "unknown"
    now = time.monotonic()
    window = _requests[key]
    while window and now - window[0] > 60:
        window.popleft()
    try:
        rate_limit = max(1, int(os.getenv("RATE_LIMIT_PER_MINUTE", "60")))
    except ValueError:
        rate_limit = 60
    if len(window) >= rate_limit:
        return Response(content='{"detail":"Rate limit exceeded"}', status_code=429,
                        media_type="application/json")
    window.append(now)
    try:
        return await call_next(request)
    except Exception:
        logger.exception("Unhandled application error: %s %s", request.method, request.url.path)
        raise


@app.get("/health")
def health() -> dict[str, str]:
    from .health import check_dependencies
    check_dependencies()
    return {"status": "ok"}


@app.post("/api-clients", status_code=201)
def register_api_client(payload: APIClientRegistration, request: Request,
                         db: Session = Depends(get_db)) -> dict:
    require_admin_token(request)
    token = secrets.token_urlsafe(32)
    client = APIClient(name=payload.name, address=payload.address,
                       contact_email=payload.contact_email, phone=payload.phone,
                       token_hash=api_token_hash(token), is_active=True)
    db.add(client)
    db.commit()
    db.refresh(client)
    return {"id": client.id, "name": client.name, "address": client.address,
            "contact_email": client.contact_email, "phone": client.phone,
            "api_token": token, "created_at": client.created_at}


@app.get("/api-clients")
def list_api_clients(request: Request, db: Session = Depends(get_db)) -> list[dict]:
    require_admin_token(request)
    return [{"id": client.id, "name": client.name, "address": client.address,
             "contact_email": client.contact_email, "phone": client.phone,
             "is_active": client.is_active, "created_at": client.created_at}
            for client in db.query(APIClient).order_by(APIClient.created_at.asc()).all()]


@app.put("/api-clients/{client_id}")
def update_api_client(client_id: int, payload: APIClientRegistration, request: Request,
                      db: Session = Depends(get_db)) -> dict:
    require_admin_token(request)
    client = db.get(APIClient, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="API client not found")
    client.name = payload.name
    client.address = payload.address
    client.contact_email = payload.contact_email
    client.phone = payload.phone
    db.commit()
    return {"id": client.id, "name": client.name, "address": client.address,
            "contact_email": client.contact_email, "phone": client.phone,
            "is_active": client.is_active}


@app.get("/api-clients/me")
def get_own_api_client(request: Request) -> dict:
    client = get_api_client(request)
    if not client:
        raise HTTPException(status_code=403, detail="Scoped API client credential required")
    return client


@app.patch("/api-clients/me")
def update_own_api_client(payload: APIClientRegistration, request: Request,
                          db: Session = Depends(get_db)) -> dict:
    identity = get_api_client(request)
    if not identity:
        raise HTTPException(status_code=403, detail="Scoped API client credential required")
    client = db.get(APIClient, identity["id"])
    if not client or not client.is_active:
        raise HTTPException(status_code=404, detail="API client not found")
    client.name = payload.name
    client.address = payload.address
    client.contact_email = payload.contact_email
    client.phone = payload.phone
    db.commit()
    return {"id": client.id, "name": client.name, "address": client.address,
            "contact_email": client.contact_email, "phone": client.phone}


@app.delete("/api-clients/{client_id}")
def revoke_api_client(client_id: int, request: Request, db: Session = Depends(get_db)) -> dict:
    require_admin_token(request)
    client = db.get(APIClient, client_id)
    if not client:
        raise HTTPException(status_code=404, detail="API client not found")
    client.is_active = False
    db.commit()
    return {"id": client.id, "is_active": False}


@app.post("/generate/contract", response_class=Response)
def generate_contract(request: ContractRequest,
                      api_client: dict | None = Depends(get_api_client)) -> Response:
    data = prepared_document_data(request, api_client)
    pdf = render_pdf("contract.html", data)
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{pdf_filename(request.project_name)}"'})


@app.post("/contracts", response_class=Response)
def create_contract(request: ContractRequest, db: Session = Depends(get_db),
                    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                    api_client: dict | None = Depends(get_api_client)) -> Response:
    if not isinstance(idempotency_key, str):
        idempotency_key = None
    api_client = api_client if isinstance(api_client, dict) else None
    return _create_contract(request, db, scoped_idempotency_key(idempotency_key, api_client),
                            api_client=api_client)


def _create_contract(request: ContractRequest, db: Session, idempotency_key: str | None = None,
                     allow_duplicate: bool = False, api_client: dict | None = None) -> Response:
    if idempotency_key is not None and len(idempotency_key) > 128:
        raise HTTPException(status_code=422, detail="Idempotency-Key must be at most 128 characters")
    if idempotency_key:
        existing = db.query(Contract).filter_by(idempotency_key=idempotency_key).first()
        if existing:
            if existing.request_fingerprint != request_fingerprint(request, api_client):
                raise HTTPException(status_code=409, detail="Idempotency-Key was already used with different data")
            return _document_pdf_response(db, existing, "contract")
    client_id = api_client.get("id") if api_client else None
    if not allow_duplicate and existing_project_document(db, Contract, request.project_name, client_id):
        raise HTTPException(status_code=409, detail="A contract already exists for this project")
    number = next_number(db, "contract", "MK-CON")
    data = prepared_document_data(request, api_client)
    data["contract_number"] = number
    document = Contract(contract_number=number, client_name=request.client_name,
                        project_name=request.project_name,
                        api_client_id=api_client.get("id") if api_client else None,
                        terms_json=json.dumps(data, default=str),
                        idempotency_key=idempotency_key,
                        request_fingerprint=request_fingerprint(request, api_client))
    try:
        db.add(document)
        audit(db, "created", "contract", number)
        db.flush()
        db.commit()
    except Exception:
        db.rollback()
        raise
    pdf = render_pdf("contract.html", data)
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{pdf_filename(request.project_name)}"'})


@app.post("/generate/invoice", response_class=Response)
def generate_invoice(request: InvoiceRequest,
                     api_client: dict | None = Depends(get_api_client)) -> Response:
    pdf = render_pdf("invoice.html", prepared_document_data(request, api_client))
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{pdf_filename(request.project_name)}"'})


@app.post("/generate/receipt", response_class=Response)
def generate_receipt(request: ReceiptRequest,
                     api_client: dict | None = Depends(get_api_client)) -> Response:
    data = prepared_document_data(request, api_client)
    data["receipt_number"] = "DRAFT-RECEIPT"
    pdf = render_pdf("receipt.html", data)
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{pdf_filename(request.project_name)}-receipt.pdf"'})


def _receipt_render_data(receipt: Receipt) -> dict:
    data = json.loads(receipt.terms_json or "{}")
    data.update(receipt_number=receipt.receipt_number, client_name=receipt.client_name,
                project_name=receipt.project_name, amount=receipt.amount, currency=receipt.currency)
    return data


def _receipt_pdf_response(receipt: Receipt) -> Response:
    if receipt.pdf_path:
        response = _file_response(receipt.pdf_path, receipt.receipt_number, receipt.project_name)
    else:
        pdf = render_pdf("receipt.html", _receipt_render_data(receipt))
        response = Response(content=pdf, media_type="application/pdf",
                            headers={"Content-Disposition": f'inline; filename="{pdf_filename(receipt.project_name)}-receipt.pdf"'})
    response.headers["X-Document-Id"] = str(receipt.id)
    response.headers["X-Document-Number"] = receipt.receipt_number
    response.headers["Location"] = f"/receipts/{receipt.id}"
    return response


def _issue_receipt(receipt: Receipt, db: Session) -> Response:
    """Render and store outside DB transactions; pending rows are safe to retry."""
    pdf = render_pdf("receipt.html", _receipt_render_data(receipt))
    location = upload_pdf(document_storage_key("receipts", receipt.receipt_number, pdf), pdf)
    receipt.pdf_path = location
    receipt.pdf_hash = hashlib.sha256(pdf).hexdigest()
    receipt.status = "issued"
    audit(db, "issued", "receipt", receipt.receipt_number)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    return _receipt_pdf_response(receipt)


@app.post("/receipts", response_class=Response)
def create_receipt(request: ReceiptRequest, db: Session = Depends(get_db),
                   idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                   api_client: dict | None = Depends(get_api_client)) -> Response:
    if not isinstance(idempotency_key, str):
        idempotency_key = None
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key is required for receipt issuance")
    if idempotency_key is not None and len(idempotency_key) > 128:
        raise HTTPException(status_code=422, detail="Idempotency-Key must be at most 128 characters")
    api_client = api_client if isinstance(api_client, dict) else None
    idempotency_key = scoped_idempotency_key(idempotency_key, api_client)
    fingerprint_value = request_fingerprint(request, api_client)
    if idempotency_key:
        existing = db.query(Receipt).filter_by(idempotency_key=idempotency_key).first()
        if existing:
            if existing.request_fingerprint != fingerprint_value:
                raise HTTPException(status_code=409, detail="Idempotency-Key was already used with different data")
            if existing.pdf_path:
                return _receipt_pdf_response(existing)
            return _issue_receipt(existing, db)

    data = prepared_document_data(request, api_client)
    number = next_number(db, "receipt", "MK-REC")
    data["receipt_number"] = number
    receipt = Receipt(receipt_number=number, client_name=request.client_name,
                      project_name=request.project_name, api_client_id=api_client.get("id") if api_client else None,
                      terms_json=json.dumps(data, default=str), amount=request.amount,
                      currency=request.currency, status="pending", idempotency_key=idempotency_key,
                      request_fingerprint=fingerprint_value)
    try:
        db.add(receipt)
        audit(db, "created", "receipt", number)
        db.commit()
        db.refresh(receipt)
    except Exception:
        db.rollback()
        raise
    return _issue_receipt(receipt, db)


@app.get("/receipts/{document_id}", response_class=Response)
def get_receipt(document_id: int, db: Session = Depends(get_db),
                api_client: dict | None = Depends(get_api_client)) -> Response:
    receipt = scoped_document_query(db.query(Receipt), Receipt, api_client).filter_by(id=document_id).first()
    if not receipt:
        raise HTTPException(status_code=404, detail="Receipt not found")
    return _receipt_pdf_response(receipt)


@app.post("/invoices", response_class=Response)
def create_invoice(request: InvoiceRequest, db: Session = Depends(get_db),
                   idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                   api_client: dict | None = Depends(get_api_client)) -> Response:
    if not isinstance(idempotency_key, str):
        idempotency_key = None
    api_client = api_client if isinstance(api_client, dict) else None
    return _create_invoice(request, db, scoped_idempotency_key(idempotency_key, api_client),
                            api_client=api_client)


def _create_invoice(request: InvoiceRequest, db: Session, idempotency_key: str | None = None,
                    allow_duplicate: bool = False, api_client: dict | None = None) -> Response:
    if idempotency_key is not None and len(idempotency_key) > 128:
        raise HTTPException(status_code=422, detail="Idempotency-Key must be at most 128 characters")
    if idempotency_key:
        existing = db.query(Invoice).filter_by(idempotency_key=idempotency_key).first()
        if existing:
            if existing.request_fingerprint != request_fingerprint(request, api_client):
                raise HTTPException(status_code=409, detail="Idempotency-Key was already used with different data")
            return _document_pdf_response(db, existing, "invoice")
    client_id = api_client.get("id") if api_client else None
    if not allow_duplicate and existing_project_document(db, Invoice, request.project_name, client_id):
        raise HTTPException(status_code=409, detail="An invoice already exists for this project")
    number = next_number(db, "invoice", "MK-INV")
    data = prepared_document_data(request, api_client)
    data["invoice_number"] = number
    data["verification_url"] = f"{public_url}/verify/{number}"
    document = Invoice(invoice_number=number, client_name=request.client_name,
                       project_name=request.project_name, amount=request.amount,
                       client_email=request.client_email, currency=request.currency,
                       api_client_id=api_client.get("id") if api_client else None,
                       terms_json=json.dumps(data, default=str),
                       idempotency_key=idempotency_key,
                       request_fingerprint=request_fingerprint(request, api_client))
    try:
        db.add(document)
        audit(db, "created", "invoice", number)
        db.flush()
        db.commit()
    except Exception:
        db.rollback()
        raise
    pdf = render_pdf("invoice.html", data)
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{pdf_filename(request.project_name)}"'})


@app.post("/contracts/{document_id}/duplicate", response_class=Response)
def duplicate_contract(document_id: int, db: Session = Depends(get_db)) -> Response:
    source = db.get(Contract, document_id)
    if not source:
        raise HTTPException(status_code=404, detail="Contract not found")
    data = json.loads(source.terms_json)
    data.pop("contract_number", None)
    origin = ({"id": source.api_client_id, **data["prepared_on_behalf_of"]}
              if source.api_client_id and data.get("prepared_on_behalf_of") else None)
    data.pop("prepared_on_behalf_of", None)
    return _create_contract(ContractRequest(**data), db, allow_duplicate=True, api_client=origin)


@app.post("/invoices/{document_id}/duplicate", response_class=Response)
def duplicate_invoice(document_id: int, db: Session = Depends(get_db)) -> Response:
    source = db.get(Invoice, document_id)
    if not source:
        raise HTTPException(status_code=404, detail="Invoice not found")
    data = json.loads(source.terms_json)
    data.pop("invoice_number", None)
    data.pop("verification_url", None)
    origin = ({"id": source.api_client_id, **data["prepared_on_behalf_of"]}
              if source.api_client_id and data.get("prepared_on_behalf_of") else None)
    data.pop("prepared_on_behalf_of", None)
    return _create_invoice(InvoiceRequest(**data), db, allow_duplicate=True, api_client=origin)


def _file_response(path: str, filename: str, project_name: str | None = None) -> Response:
    try:
        content = read_pdf(path)
    except (FileNotFoundError, OSError):
        raise HTTPException(status_code=404, detail="PDF file not found")
    download_name = pdf_filename(project_name) if project_name else f"{filename}.pdf"
    return Response(content=content, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{download_name}"'})


def _document_pdf_response(db: Session, document: Contract | Invoice, kind: str) -> Response:
    number = document.contract_number if kind == "contract" else document.invoice_number
    if document.pdf_path:
        return _file_response(document.pdf_path, number, document.project_name)
    data = contract_render_data(document) if kind == "contract" else invoice_render_data(document)
    pdf = render_pdf(f"{kind}.html", data)
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{pdf_filename(document.project_name)}"'})


@app.get("/contracts/{document_id}", response_class=Response)
def get_contract(document_id: int, db: Session = Depends(get_db),
                 api_client: dict | None = Depends(get_api_client)) -> Response:
    document = scoped_document_query(db.query(Contract), Contract, api_client).filter_by(id=document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Contract not found")
    return _document_pdf_response(db, document, "contract")


@app.get("/invoices/{document_id}", response_class=Response)
def get_invoice(document_id: int, db: Session = Depends(get_db),
                api_client: dict | None = Depends(get_api_client)) -> Response:
    document = scoped_document_query(db.query(Invoice), Invoice, api_client).filter_by(id=document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return _document_pdf_response(db, document, "invoice")


@app.get("/documents/{number}")
def get_document_status(number: str, db: Session = Depends(get_db),
                        api_client: dict | None = Depends(get_api_client)) -> dict:
    contract = scoped_document_query(db.query(Contract), Contract, api_client).filter_by(contract_number=number).first()
    if contract:
        return {"type": "contract", "id": contract.id, "number": number,
                "client_name": contract.client_name, "project_name": contract.project_name,
                "status": contract.status, "created_at": contract.created_at}
    invoice = scoped_document_query(db.query(Invoice), Invoice, api_client).filter_by(invoice_number=number).first()
    if invoice:
        return {"type": "invoice", "id": invoice.id, "number": number,
                "client_name": invoice.client_name, "project_name": invoice.project_name,
                "status": invoice.status, "amount": str(invoice.amount),
                "currency": invoice.currency, "created_at": invoice.created_at}
    receipt = scoped_document_query(db.query(Receipt), Receipt, api_client).filter_by(receipt_number=number).first()
    if receipt:
        return {"type": "receipt", "id": receipt.id, "number": number,
                "client_name": receipt.client_name, "project_name": receipt.project_name,
                "status": receipt.status, "amount": str(receipt.amount), "currency": receipt.currency,
                "created_at": receipt.created_at}
    raise HTTPException(status_code=404, detail="Document not found")


@app.get("/contracts")
def list_contracts(db: Session = Depends(get_db)) -> list[dict]:
    return [{"id": item.id, "number": item.contract_number, "client_name": item.client_name,
             "project_name": item.project_name, "status": item.status, "created_at": item.created_at}
            for item in db.query(Contract).filter_by(archived_at=None).order_by(Contract.created_at.desc()).all()]


@app.get("/invoices")
def list_invoices(db: Session = Depends(get_db)) -> list[dict]:
    return [{"id": item.id, "number": item.invoice_number, "client_name": item.client_name,
             "project_name": item.project_name, "status": item.status,
             "amount": str(item.amount), "currency": item.currency, "created_at": item.created_at}
            for item in db.query(Invoice).filter_by(archived_at=None).order_by(Invoice.created_at.desc()).all()]


@app.delete("/contracts/{document_id}")
def delete_contract(document_id: int, db: Session = Depends(get_db)) -> dict:
    document = db.get(Contract, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Contract not found")
    if document.archived_at:
        raise HTTPException(status_code=409, detail="Contract is already archived")
    document.archived_at = datetime.now(timezone.utc)
    audit(db, "archived", "contract", document.contract_number)
    db.commit()
    return {"archived": True, "number": document.contract_number}


@app.delete("/contracts/{document_id}/permanent")
def permanently_delete_contract(document_id: int, db: Session = Depends(get_db)) -> dict:
    document = db.get(Contract, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Contract not found")
    if not document.archived_at:
        raise HTTPException(status_code=409, detail="Archive the contract before permanent deletion")
    if document.pdf_path:
        delete_pdf(document.pdf_path)
    number = document.contract_number
    audit(db, "permanently_deleted", "contract", number)
    db.delete(document)
    db.commit()
    return {"deleted": True, "number": number}


@app.get("/audit/{number}")
def document_audit(number: str, db: Session = Depends(get_db)) -> list[dict]:
    return [{"action": item.action, "type": item.document_type, "number": item.document_number,
             "detail": item.detail, "created_at": item.created_at}
            for item in db.query(AuditLog).filter_by(document_number=number).order_by(AuditLog.created_at.asc()).all()]


@app.get("/clients/{client_name}")
def client_history(client_name: str, db: Session = Depends(get_db)) -> dict:
    contracts = db.query(Contract).filter(Contract.client_name.ilike(client_name), Contract.archived_at.is_(None)).all()
    invoices = db.query(Invoice).filter(Invoice.client_name.ilike(client_name), Invoice.archived_at.is_(None)).all()
    return {"client_name": client_name,
            "contracts": [{"id": x.id, "number": x.contract_number, "project_name": x.project_name,
                           "status": x.status} for x in contracts],
            "invoices": [{"id": x.id, "number": x.invoice_number, "project_name": x.project_name,
                          "amount": str(x.amount), "currency": x.currency, "status": x.status} for x in invoices]}


@app.get("/verify/{number}")
def verify_document(number: str, db: Session = Depends(get_db)) -> dict:
    document = db.query(Contract).filter_by(contract_number=number).first()
    if not document:
        document = db.query(Invoice).filter_by(invoice_number=number).first()
    if not document:
        document = db.query(Receipt).filter_by(receipt_number=number).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    try:
        actual_hash = hashlib.sha256(read_pdf(document.pdf_path)).hexdigest() if document.pdf_path else None
    except (FileNotFoundError, OSError):
        actual_hash = None
    return {"number": number, "status": document.status, "issued": bool(document.pdf_path),
            "hash": document.pdf_hash,
            "hash_valid": (bool(actual_hash and document.pdf_hash and actual_hash == document.pdf_hash)
                           if document.pdf_path else None)}


@app.post("/contracts/{document_id}/send")
def send_contract(document_id: int, db: Session = Depends(get_db)) -> dict:
    document = db.get(Contract, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Contract not found")
    if document.archived_at or document.status != "draft":
        raise HTTPException(status_code=409, detail="Only active draft contracts can be sent")
    terms = json.loads(document.terms_json)
    if not terms.get("client_email"):
        raise HTTPException(status_code=400, detail="Contract has no client email")
    pdf = render_pdf("contract.html", contract_render_data(document))
    location = upload_pdf(document_storage_key("contracts", document.contract_number, pdf), pdf)
    document.pdf_path = location
    document.pdf_hash = hashlib.sha256(pdf).hexdigest()
    delivered = False
    try:
        send_pdf(terms["client_email"], f"Contract {document.contract_number}",
                 f"{document.contract_number}.pdf", location)
        delivered = True
        document.status = "sent"
        audit(db, "sent", "contract", document.contract_number)
        db.commit()
    except Exception:
        db.rollback()
        if not delivered:
            delete_pdf(location)
        raise
    return {"number": document.contract_number, "status": document.status}


@app.post("/invoices/{document_id}/send")
def send_invoice(document_id: int, db: Session = Depends(get_db)) -> dict:
    document = db.get(Invoice, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if document.archived_at or document.status != "draft":
        raise HTTPException(status_code=409, detail="Only active draft invoices can be sent")
    if not document.client_email:
        raise HTTPException(status_code=400, detail="Invoice has no client email")
    pdf = render_pdf("invoice.html", invoice_render_data(document))
    location = upload_pdf(document_storage_key("invoices", document.invoice_number, pdf), pdf)
    document.pdf_path = location
    document.pdf_hash = hashlib.sha256(pdf).hexdigest()
    delivered = False
    try:
        send_pdf(document.client_email, f"Invoice {document.invoice_number}",
                 f"{document.invoice_number}.pdf", location)
        delivered = True
        document.status = "sent"
        audit(db, "sent", "invoice", document.invoice_number)
        db.commit()
    except Exception:
        db.rollback()
        if not delivered:
            delete_pdf(location)
        raise
    return {"number": document.invoice_number, "status": document.status}


@app.post("/contracts/{document_id}/mark-accepted")
def mark_contract_accepted(document_id: int, db: Session = Depends(get_db)) -> dict:
    document = db.get(Contract, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Contract not found")
    if document.archived_at or document.status != "sent":
        raise HTTPException(status_code=409, detail="Only active sent contracts can be accepted")
    document.status = "accepted"
    document.accepted_at = datetime.now(timezone.utc)
    audit(db, "accepted", "contract", document.contract_number)
    db.commit()
    return {"number": document.contract_number, "status": document.status,
            "accepted_at": document.accepted_at}
