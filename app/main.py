import json
import hashlib
import logging
import os
import re
import time
from decimal import Decimal, InvalidOperation
from contextlib import asynccontextmanager
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from dotenv import load_dotenv

load_dotenv()

from .db import get_db, next_number
from .models import AuditLog, Contract, Invoice, Payment, InvoiceAdjustment
from .mailer import send_pdf
from .storage import delete_pdf, read_pdf, upload_pdf

from .render import render_pdf
from .schemas import ContractRequest, InvoiceRequest, LedgerSyncRequest, ServiceLedgerSyncRequest

logger = logging.getLogger(__name__)
production = os.getenv("ENVIRONMENT", "development").lower() == "production"
telegram_application = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global telegram_application
    webhook_url = os.getenv("TELEGRAM_WEBHOOK_URL", "").rstrip("/")
    if webhook_url and os.getenv("TELEGRAM_BOT_TOKEN"):
        if not os.getenv("TELEGRAM_WEBHOOK_SECRET"):
            raise RuntimeError("TELEGRAM_WEBHOOK_SECRET is required when the Telegram webhook is enabled")
        from bot import build_application
        from telegram import Update

        webhook_path = os.getenv("TELEGRAM_WEBHOOK_PATH", "telegram/webhook").strip("/")
        if webhook_path != "telegram/webhook":
            raise RuntimeError("TELEGRAM_WEBHOOK_PATH must be telegram/webhook when sharing the API port")
        telegram_application = build_application()
        await telegram_application.initialize()
        await telegram_application.start()
        await telegram_application.bot.set_webhook(
            url=f"{webhook_url}/{webhook_path}",
            secret_token=os.getenv("TELEGRAM_WEBHOOK_SECRET"),
            drop_pending_updates=True,
        )
    yield
    if telegram_application:
        await telegram_application.stop()
        await telegram_application.shutdown()


app = FastAPI(title="Scribed", description="mikesplore contract and invoice PDF generation",
              docs_url=None if production else "/docs", redoc_url=None if production else "/redoc",
              openapi_url=None if production else "/openapi.json", lifespan=lifespan)
cors_origins = [origin.strip() for origin in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",") if origin.strip()]
app.add_middleware(CORSMiddleware, allow_origins=cors_origins, allow_credentials=False,
                   allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
                   allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Gatekeeper-Secret"])
MAX_BODY = 400 * 1024
_requests: dict[str, deque[float]] = defaultdict(deque)

def audit(db, action: str, kind: str, number: str, detail: str | None = None):
    db.add(AuditLog(action=action, document_type=kind, document_number=number, detail=detail))

def invoice_render_data(db, invoice: Invoice) -> dict:
    adjustments = db.query(InvoiceAdjustment).filter_by(invoice_id=invoice.id).order_by(InvoiceAdjustment.created_at.asc()).all()
    payments = db.query(Payment).filter_by(invoice_id=invoice.id).order_by(Payment.paid_at.asc()).all()
    data = json.loads(invoice.terms_json or "{}")
    data.update(invoice_number=invoice.invoice_number, client_name=invoice.client_name, project_name=invoice.project_name,
                client_email=invoice.client_email, amount=invoice.amount, currency=invoice.currency, status=invoice.status,
                base_amount=invoice.base_amount, total_adjustments=invoice.total_adjustments,
                total_paid=invoice.total_payments, amount_paid=invoice.total_payments,
                outstanding_balance=invoice.outstanding_balance,
                adjustments=[{"label": "Additional charge" if item.type == "ADDITIONAL_CHARGE" else "Discount", "reason": item.reason, "display_amount": f"{'+' if item.type == 'ADDITIONAL_CHARGE' else '-'}{float(item.amount):,.2f}"} for item in adjustments],
                payments=[{"amount": item.amount, "status": "success", "paidAt": item.paid_at} for item in payments])
    return data


def contract_render_data(contract: Contract) -> dict:
    data = json.loads(contract.terms_json or "{}")
    data["contract_number"] = contract.contract_number
    data["amount"] = Decimal(str(data["amount"]))
    return data


def refresh_invoice_pdf(db, invoice: Invoice) -> None:
    """Persist the first issued invoice rendition; drafts and issued files stay immutable."""
    if invoice.status == "draft" or invoice.pdf_path:
        return
    pdf = render_pdf("invoice.html", invoice_render_data(db, invoice))
    location = document_storage_key("invoices", invoice.invoice_number, pdf)
    invoice.pdf_path = upload_pdf(location, pdf)
    invoice.pdf_hash = hashlib.sha256(pdf).hexdigest()

def fingerprint(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def numeric_version(value: str) -> tuple[int, ...] | None:
    normalized = value.lower().removeprefix("v")
    if not re.fullmatch(r"\d+(?:\.\d+)*", normalized):
        return None
    parts = [int(part) for part in normalized.split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)

def pdf_filename(project_id: str | None, project_name: str, kind: str) -> str:
    """Build a safe, readable filename from the project title.

    Project IDs are useful internally but are not meaningful to a client,
    so downloads consistently use names such as ``website.pdf``.
    """
    del project_id
    slug = re.sub(r"[^a-z0-9]+", "-", (project_name or "").lower()).strip("-")
    slug = slug[:80].rstrip("-") or "mikesplore"
    del kind
    return f"{slug}.pdf"


def document_storage_key(kind: str, number: str, content: bytes) -> str:
    """Use an immutable per-version object key to prevent cross-document overwrites."""
    digest = hashlib.sha256(content).hexdigest()
    return f"{kind}/{number}-{digest}.pdf"


def existing_project_document(db: Session, model, project_name: str):
    """Return an existing document for this project, including archived rows."""
    normalized_name = project_name.strip().lower()
    return db.query(model).filter(
        func.lower(func.trim(model.project_name)) == normalized_name
    ).first()

@app.middleware("http")
async def require_api_token(request: Request, call_next):
    content_length = request.headers.get("content-length")
    try:
        too_large = content_length is not None and int(content_length) > MAX_BODY
    except ValueError:
        return Response(content='{"detail":"Invalid Content-Length"}', status_code=400, media_type="application/json")
    if too_large:
        return Response(content='{"detail":"Payload Too Large"}', status_code=413, media_type="application/json")
    if request.method in {"POST", "PUT", "PATCH"} and content_length is None:
        body = await request.body()
        if len(body) > MAX_BODY:
            return Response(content='{"detail":"Payload Too Large"}', status_code=413, media_type="application/json")
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
    if path in {"/health", "/docs", "/redoc", "/openapi.json", "/telegram/webhook"} or path.startswith("/verify/"):
        return await call_next(request)
    if not expected or request.headers.get("authorization") != f"Bearer {expected}":
        return Response(content='{"detail":"Authentication required"}', status_code=401, media_type="application/json")
    key = request.client.host if request.client else "unknown"
    now = time.monotonic(); window = _requests[key]
    while window and now - window[0] > 60: window.popleft()
    try:
        rate_limit = max(1, int(os.getenv("RATE_LIMIT_PER_MINUTE", "60")))
    except ValueError:
        rate_limit = 60
    if len(window) >= rate_limit:
        return Response(content='{"detail":"Rate limit exceeded"}', status_code=429, media_type="application/json")
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


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request) -> dict[str, bool]:
    if telegram_application is None:
        raise HTTPException(status_code=503, detail="Telegram webhook is not configured")
    expected = os.getenv("TELEGRAM_WEBHOOK_SECRET")
    if not expected:
        raise HTTPException(status_code=503, detail="Telegram webhook secret is not configured")
    if request.headers.get("x-telegram-bot-api-secret-token") != expected:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")
    from telegram import Update
    update = Update.de_json(await request.json(), telegram_application.bot)
    await telegram_application.process_update(update)
    return {"ok": True}


@app.post("/generate/contract", response_class=Response)
def generate_contract(request: ContractRequest) -> Response:
    pdf = render_pdf("contract.html", request.model_dump())
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{pdf_filename(request.gatekeeper_project_id, request.project_name, "contract")}"'})


@app.post("/contracts", response_class=Response)
def create_contract(request: ContractRequest, db: Session = Depends(get_db), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> Response:
    if not isinstance(idempotency_key, str):
        idempotency_key = None
    return _create_contract(request, db, idempotency_key)


def _create_contract(request: ContractRequest, db: Session, idempotency_key: str | None = None,
                     allow_duplicate: bool = False) -> Response:
    if idempotency_key is not None and len(idempotency_key) > 128:
        raise HTTPException(status_code=422, detail="Idempotency-Key must be at most 128 characters")
    if idempotency_key:
        existing = db.query(Contract).filter_by(idempotency_key=idempotency_key).first()
        if existing:
            if existing.request_fingerprint != fingerprint(request.model_dump()):
                raise HTTPException(status_code=409, detail="Idempotency-Key was already used with different data")
            return _document_pdf_response(db, existing, "contract")
    existing = None if allow_duplicate else existing_project_document(db, Contract, request.project_name)
    if existing:
        raise HTTPException(status_code=409, detail="A contract already exists for this project")
    number = next_number(db, "contract", "MK-CON")
    request_data = request.model_dump()
    request_data["contract_number"] = number
    pdf = render_pdf("contract.html", request_data)
    document = Contract(contract_number=number, client_name=request.client_name, project_name=request.project_name,
                        gatekeeper_project_id=request.gatekeeper_project_id,
                        terms_json=json.dumps(request_data, default=str), pdf_path=None,
                        pdf_hash=None, idempotency_key=idempotency_key,
                        request_fingerprint=fingerprint(request.model_dump()))
    try:
        db.add(document)
        audit(db, "created", "contract", number)
        db.flush()
        db.commit()
    except Exception:
        db.rollback()
        raise
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{pdf_filename(request.gatekeeper_project_id, request.project_name, "contract")}"'},
    )


@app.post("/generate/invoice", response_class=Response)
def generate_invoice(request: InvoiceRequest) -> Response:
    pdf = render_pdf("invoice.html", request.model_dump())
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{pdf_filename(request.gatekeeper_project_id, request.project_name, "invoice")}"'},
    )


@app.post("/invoices", response_class=Response)
def create_invoice(request: InvoiceRequest, db: Session = Depends(get_db), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> Response:
    if not isinstance(idempotency_key, str):
        idempotency_key = None
    return _create_invoice(request, db, idempotency_key)


def _create_invoice(request: InvoiceRequest, db: Session, idempotency_key: str | None = None,
                    allow_duplicate: bool = False) -> Response:
    if idempotency_key is not None and len(idempotency_key) > 128:
        raise HTTPException(status_code=422, detail="Idempotency-Key must be at most 128 characters")
    if idempotency_key:
        existing = db.query(Invoice).filter_by(idempotency_key=idempotency_key).first()
        if existing:
            if existing.request_fingerprint != fingerprint(request.model_dump()):
                raise HTTPException(status_code=409, detail="Idempotency-Key was already used with different data")
            return _document_pdf_response(db, existing, "invoice")
    existing = None if allow_duplicate or request.gatekeeper_service_id else existing_project_document(db, Invoice, request.project_name)
    if existing:
        raise HTTPException(status_code=409, detail="An invoice already exists for this project")
    number = next_number(db, "invoice", "MK-INV")
    request_data = request.model_dump()
    invoice_total = request.original_amount or request.amount
    total_paid = request.amount_paid or Decimal("0")
    if total_paid > invoice_total:
        raise HTTPException(status_code=422, detail="amount_paid cannot exceed the invoice total")
    balance = invoice_total - total_paid
    request_data.update(amount=invoice_total, original_amount=invoice_total, amount_paid=total_paid)
    render_data = {**request_data, "total_paid": total_paid, "outstanding_balance": balance,
                   "invoice_number": number}
    pdf = render_pdf("invoice.html", render_data)
    document = Invoice(invoice_number=number, client_name=request.client_name, project_name=request.project_name, amount=invoice_total,
                       base_amount=invoice_total, total_payments=total_paid, outstanding_balance=balance,
                       client_email=request.client_email, gatekeeper_project_id=request.gatekeeper_project_id,
                       gatekeeper_service_id=request.gatekeeper_service_id, currency=request.currency,
                       status="draft",
                       terms_json=json.dumps(request_data, default=str), pdf_path=None,
                       pdf_hash=None, idempotency_key=idempotency_key,
                       request_fingerprint=fingerprint(request.model_dump()))
    try:
        db.add(document)
        audit(db, "created", "invoice", number)
        db.flush()
        db.commit()
    except Exception:
        db.rollback()
        raise
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{pdf_filename(request.gatekeeper_project_id, request.project_name, "invoice")}"'})


@app.post("/contracts/{document_id}/duplicate", response_class=Response)
def duplicate_contract(document_id: int, db: Session = Depends(get_db)) -> Response:
    source = db.get(Contract, document_id)
    if not source: raise HTTPException(status_code=404, detail="Contract not found")
    data = json.loads(source.terms_json)
    data.pop("contract_number", None)
    data["gatekeeper_project_id"] = source.gatekeeper_project_id
    return _create_contract(ContractRequest(**data), db, allow_duplicate=True)


@app.post("/invoices/{document_id}/duplicate", response_class=Response)
def duplicate_invoice(document_id: int, db: Session = Depends(get_db)) -> Response:
    source = db.get(Invoice, document_id)
    if not source: raise HTTPException(status_code=404, detail="Invoice not found")
    data = json.loads(source.terms_json)
    data.pop("invoice_number", None)
    data["gatekeeper_project_id"] = source.gatekeeper_project_id
    data["gatekeeper_service_id"] = source.gatekeeper_service_id
    data.pop("total_paid", None)
    data.pop("outstanding_balance", None)
    data.pop("_ledger_version", None)
    data.pop("_ledger_fingerprint", None)
    data["amount_paid"] = 0
    data["original_amount"] = data["amount"]
    data.pop("payments", None)
    return _create_invoice(InvoiceRequest(**data), db, allow_duplicate=True)


def _file_response(path: str, filename: str, project_id: str | None = None,
                   project_name: str | None = None, kind: str | None = None) -> Response:
    try:
        content = read_pdf(path)
    except (FileNotFoundError, OSError):
        raise HTTPException(status_code=404, detail="PDF file not found")
    download_name = (pdf_filename(project_id, project_name, kind)
                     if project_name and kind else f"{filename}.pdf")
    return Response(content=content, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{download_name}"'})


def _document_pdf_response(db: Session, document: Contract | Invoice, kind: str) -> Response:
    project_id = document.gatekeeper_project_id
    if document.pdf_path:
        return _file_response(document.pdf_path, document.contract_number if kind == "contract" else document.invoice_number,
                              project_id, document.project_name, kind)
    if kind == "contract":
        pdf = render_pdf("contract.html", contract_render_data(document))
    else:
        data = invoice_render_data(db, document)
        pdf = render_pdf("invoice.html", data)
    download_name = pdf_filename(project_id, document.project_name, kind)
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{download_name}"'})


@app.get("/contracts/{document_id}", response_class=Response)
def get_contract(document_id: int, db: Session = Depends(get_db)) -> Response:
    document = db.get(Contract, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Contract not found")
    return _document_pdf_response(db, document, "contract")


@app.get("/invoices/{document_id}", response_class=Response)
def get_invoice(document_id: int, db: Session = Depends(get_db)) -> Response:
    document = db.get(Invoice, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return _document_pdf_response(db, document, "invoice")


@app.get("/documents/{number}")
def get_document_status(number: str, db: Session = Depends(get_db)) -> dict:
    contract = db.query(Contract).filter_by(contract_number=number).first()
    if contract:
        return {"type": "contract", "id": contract.id, "number": number,
                "client_name": contract.client_name, "project_name": contract.project_name,
                "status": contract.status, "created_at": contract.created_at}
    invoice = db.query(Invoice).filter_by(invoice_number=number).first()
    if invoice:
        return {"type": "invoice", "id": invoice.id, "number": number,
                "client_name": invoice.client_name, "status": invoice.status,
                "project_name": invoice.project_name,
                "amount": str(invoice.amount), "currency": invoice.currency,
                "gatekeeper_project_id": invoice.gatekeeper_project_id,
                "created_at": invoice.created_at}
    raise HTTPException(status_code=404, detail="Document not found")


@app.get("/contracts")
def list_contracts(db: Session = Depends(get_db)) -> list[dict]:
    return [{"id": item.id, "number": item.contract_number, "client_name": item.client_name,
             "project_name": item.project_name, "status": item.status, "created_at": item.created_at}
            for item in db.query(Contract).filter_by(archived_at=None).order_by(Contract.created_at.desc()).all()]


@app.delete("/contracts/{document_id}")
def delete_contract(document_id: int, db: Session = Depends(get_db)) -> dict:
    document = db.get(Contract, document_id)
    if not document: raise HTTPException(status_code=404, detail="Contract not found")
    if document.archived_at: raise HTTPException(status_code=409, detail="Contract is already archived")
    document.archived_at = datetime.now(timezone.utc); audit(db, "archived", "contract", document.contract_number); db.commit()
    return {"archived": True, "number": document.contract_number}


@app.delete("/contracts/{document_id}/permanent")
def permanently_delete_contract(document_id: int, db: Session = Depends(get_db)) -> dict:
    document = db.get(Contract, document_id)
    if not document: raise HTTPException(status_code=404, detail="Contract not found")
    if not document.archived_at: raise HTTPException(status_code=409, detail="Archive the contract before permanent deletion")
    if document.pdf_path:
        delete_pdf(document.pdf_path)
    number = document.contract_number; audit(db, "permanently_deleted", "contract", number); db.delete(document); db.commit()
    return {"deleted": True, "number": number}


@app.get("/invoices")
def list_invoices(db: Session = Depends(get_db)) -> list[dict]:
    return [{"id": item.id, "number": item.invoice_number, "client_name": item.client_name,
             "project_name": item.project_name,
             "status": item.status, "amount": str(item.amount), "currency": item.currency,
             "gatekeeper_project_id": item.gatekeeper_project_id, "created_at": item.created_at}
            for item in db.query(Invoice).filter_by(archived_at=None).order_by(Invoice.created_at.desc()).all()]


@app.post("/integrations/gatekeeper/invoices/ensure")
def ensure_gatekeeper_invoice(request: InvoiceRequest, db: Session = Depends(get_db),
                              x_gatekeeper_secret: str | None = Header(default=None),
                              idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict:
    expected = os.getenv("SCRIBED_INTEGRATION_SECRET")
    if not expected or x_gatekeeper_secret != expected:
        raise HTTPException(status_code=401, detail="Invalid integration secret")
    project_id = request.gatekeeper_project_id
    if not project_id:
        raise HTTPException(status_code=422, detail="gatekeeper_project_id is required")

    invoice = db.query(Invoice).filter_by(gatekeeper_project_id=project_id, archived_at=None).order_by(Invoice.created_at.desc()).first()
    if not invoice:
        invoice = db.query(Invoice).filter(
            func.lower(func.trim(Invoice.project_name)) == request.project_name.strip().lower(),
            Invoice.archived_at.is_(None),
        ).order_by(Invoice.created_at.desc()).first()
        if invoice and invoice.gatekeeper_project_id not in (None, project_id):
            raise HTTPException(status_code=409, detail="Project name is already linked to another Gatekeeper project")

    if not invoice:
        try:
            create_invoice(request, db, idempotency_key)
        except HTTPException:
            db.rollback()
            invoice = db.query(Invoice).filter_by(gatekeeper_project_id=project_id, archived_at=None).order_by(Invoice.created_at.desc()).first()
            if not invoice:
                raise

    invoice = db.query(Invoice).filter_by(gatekeeper_project_id=project_id, archived_at=None).order_by(Invoice.created_at.desc()).first()
    if not invoice:
        raise HTTPException(status_code=502, detail="Scribed failed to persist the Gatekeeper invoice")
    terms = json.loads(invoice.terms_json or "{}")
    invoice_total = request.original_amount or request.amount
    gatekeeper_paid = request.amount_paid or Decimal("0")
    if gatekeeper_paid > invoice_total:
        raise HTTPException(status_code=409, detail="Recorded payments exceed the invoice total")
    terms.update(request.model_dump(exclude_none=True))
    terms.update(amount=invoice_total, original_amount=invoice_total, amount_paid=gatekeeper_paid,
                 total_paid=gatekeeper_paid, outstanding_balance=invoice_total - gatekeeper_paid)
    terms["invoice_number"] = invoice.invoice_number
    recorded_paid = sum((Decimal(str(item.amount)) for item in db.query(Payment).filter_by(invoice_id=invoice.id).all()), Decimal("0"))
    invoice.client_name = request.client_name
    invoice.client_email = request.client_email
    invoice.project_name = request.project_name
    invoice.gatekeeper_project_id = project_id
    invoice.amount = invoice_total
    invoice.base_amount = invoice_total
    invoice.total_payments = max(recorded_paid, gatekeeper_paid)
    invoice.outstanding_balance = max(Decimal("0"), invoice_total - Decimal(str(invoice.total_payments)))
    invoice.currency = request.currency
    invoice.status = "paid" if invoice.outstanding_balance == 0 else ("partially_paid" if invoice.total_payments > 0 else "sent")
    invoice.terms_json = json.dumps(terms, default=str)
    refresh_invoice_pdf(db, invoice)
    audit(db, "gatekeeper_invoice_synced", "invoice", invoice.invoice_number, f"project_id={project_id}")
    db.commit()
    return {"invoice": {"id": invoice.id, "number": invoice.invoice_number, "status": invoice.status,
                         "amount": str(invoice.amount), "currency": invoice.currency}}


@app.get("/integrations/gatekeeper/invoices/{gatekeeper_project_id}")
def gatekeeper_invoice_status(gatekeeper_project_id: str, db: Session = Depends(get_db), x_gatekeeper_secret: str | None = Header(default=None)) -> dict:
    expected = os.getenv("SCRIBED_INTEGRATION_SECRET")
    if not expected or x_gatekeeper_secret != expected:
        raise HTTPException(status_code=401, detail="Invalid integration secret")
    invoice = db.query(Invoice).filter_by(gatekeeper_project_id=gatekeeper_project_id, archived_at=None).order_by(Invoice.created_at.desc()).first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found for project")
    terms = json.loads(invoice.terms_json or "{}")
    payments = db.query(Payment).filter_by(invoice_id=invoice.id).order_by(Payment.paid_at.desc()).all()
    paid = max(sum((Decimal(str(item.amount)) for item in payments), Decimal("0")), Decimal(str(invoice.total_payments or 0)))
    public_base = os.getenv("SCRIBED_PUBLIC_URL", "").rstrip("/")
    return {
        "invoice": {"id": invoice.id, "number": invoice.invoice_number, "status": invoice.status,
                    "amount": str(invoice.amount), "currency": invoice.currency,
                    "due_date": terms.get("due_date"),
                    "paid": str(paid), "balance": str(max(Decimal("0"), Decimal(str(invoice.amount)) - paid))},
        "payments": [{"id": item.id, "provider": item.provider, "provider_reference": item.provider_reference,
                      "amount": str(item.amount), "currency": item.currency, "paid_at": item.paid_at,
                      "receipt_number": item.receipt_number, "receipt_url": f"{public_base}/payments/{item.id}/receipt" if public_base else f"/payments/{item.id}/receipt"} for item in payments]
    }


@app.get("/integrations/gatekeeper/services/{gatekeeper_service_id}/invoice")
def gatekeeper_service_invoice_status(gatekeeper_service_id: str, db: Session = Depends(get_db), x_gatekeeper_secret: str | None = Header(default=None)) -> dict:
    expected = os.getenv("SCRIBED_INTEGRATION_SECRET")
    if not expected or x_gatekeeper_secret != expected:
        raise HTTPException(status_code=401, detail="Invalid integration secret")
    invoice = db.query(Invoice).filter_by(gatekeeper_service_id=gatekeeper_service_id, archived_at=None).order_by(Invoice.created_at.desc()).first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found for service")
    terms = json.loads(invoice.terms_json or "{}")
    payments = db.query(Payment).filter_by(invoice_id=invoice.id).order_by(Payment.paid_at.desc()).all()
    paid = max(sum((Decimal(str(item.amount)) for item in payments), Decimal("0")), Decimal(str(invoice.total_payments or 0)))
    invoice_total = Decimal(str(invoice.base_amount or invoice.amount)) + Decimal(str(invoice.total_adjustments or 0))
    balance = max(Decimal("0"), invoice_total - paid)
    return {"invoice": {"id": invoice.id, "number": invoice.invoice_number, "status": invoice.status,
                         "amount": str(invoice_total), "currency": invoice.currency, "due_date": terms.get("due_date"),
                         "paid": str(paid), "balance": str(balance),
                         "payments": [{"provider_reference": item.provider_reference, "amount": str(item.amount)} for item in payments]}}


@app.post("/integrations/gatekeeper/services/{gatekeeper_service_id}/invoices/ensure")
def ensure_gatekeeper_service_invoice(gatekeeper_service_id: str, request: InvoiceRequest, db: Session = Depends(get_db),
                                      x_gatekeeper_secret: str | None = Header(default=None),
                                      idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict:
    expected = os.getenv("SCRIBED_INTEGRATION_SECRET")
    if not expected or x_gatekeeper_secret != expected:
        raise HTTPException(status_code=401, detail="Invalid integration secret")
    if request.gatekeeper_service_id != gatekeeper_service_id:
        raise HTTPException(status_code=409, detail="Service ID does not match the request path")
    if not request.gatekeeper_project_id:
        raise HTTPException(status_code=422, detail="gatekeeper_project_id is required")
    invoice = db.query(Invoice).filter_by(gatekeeper_service_id=gatekeeper_service_id, archived_at=None).order_by(Invoice.created_at.desc()).first()
    created = invoice is None
    if not invoice:
        try:
            create_invoice(request, db, idempotency_key)
        except HTTPException:
            db.rollback()
            invoice = db.query(Invoice).filter_by(gatekeeper_service_id=gatekeeper_service_id, archived_at=None).order_by(Invoice.created_at.desc()).first()
            if not invoice:
                raise
        invoice = db.query(Invoice).filter_by(gatekeeper_service_id=gatekeeper_service_id, archived_at=None).order_by(Invoice.created_at.desc()).first()
    if not invoice:
        raise HTTPException(status_code=502, detail="Scribed failed to persist the Gatekeeper service invoice")
    return {"created": created, "invoice": {"id": invoice.id, "number": invoice.invoice_number, "status": invoice.status,
                                               "amount": str(invoice.amount), "currency": invoice.currency}}


@app.get("/audit/{number}")
def document_audit(number: str, db: Session = Depends(get_db)) -> list[dict]:
    return [{"action": item.action, "type": item.document_type, "number": item.document_number,
             "detail": item.detail, "created_at": item.created_at}
            for item in db.query(AuditLog).filter_by(document_number=number).order_by(AuditLog.created_at.asc()).all()]


@app.get("/clients/{client_name}")
def client_history(client_name: str, db: Session = Depends(get_db)) -> dict:
    contracts = db.query(Contract).filter(Contract.client_name.ilike(client_name), Contract.archived_at.is_(None)).all()
    invoices = db.query(Invoice).filter(Invoice.client_name.ilike(client_name), Invoice.archived_at.is_(None)).all()
    billed = sum((item.amount for item in invoices), 0)
    paid = sum((item.amount for item in invoices if item.status == "paid"), 0)
    return {"client_name": client_name, "contracts": [{"id": x.id, "number": x.contract_number, "project_name": x.project_name, "status": x.status} for x in contracts],
            "invoices": [{"id": x.id, "number": x.invoice_number, "project_name": x.project_name, "amount": str(x.amount), "currency": x.currency, "status": x.status} for x in invoices],
            "totals": {"billed": str(billed), "paid": str(paid), "balance": str(billed - paid)}}


@app.get("/contracts/by-project/{gatekeeper_project_id}")
def contract_by_project(gatekeeper_project_id: str, db: Session = Depends(get_db)) -> dict:
    document = db.query(Contract).filter_by(gatekeeper_project_id=gatekeeper_project_id).order_by(Contract.created_at.desc()).first()
    if not document: raise HTTPException(status_code=404, detail="Contract not found for project")
    terms = json.loads(document.terms_json)
    return {"id": document.id, "contract_number": document.contract_number, "project_name": document.project_name,
            "status": document.status, "late_payment_clause": "Work may be paused after written notice when payment is overdue.",
            "terms": terms}


@app.post("/integrations/gatekeeper/suspensions")
def record_gatekeeper_suspension(payload: dict, db: Session = Depends(get_db), x_gatekeeper_secret: str | None = Header(default=None)) -> dict:
    expected = os.getenv("SCRIBED_INTEGRATION_SECRET")
    if not expected or x_gatekeeper_secret != expected: raise HTTPException(status_code=401, detail="Invalid integration secret")
    project_id = str(payload.get("project_id", ""))
    if not project_id:
        raise HTTPException(status_code=422, detail="project_id is required")

    # A suspension is an operational event, not a contract-only event. Projects
    # can be blocked before a contract is created, so do not reject the event
    # merely because the optional contract link is absent.
    contract = db.query(Contract).filter_by(gatekeeper_project_id=project_id).order_by(Contract.created_at.desc()).first()
    invoice = db.query(Invoice).filter_by(gatekeeper_project_id=project_id, archived_at=None).order_by(Invoice.created_at.desc()).first()
    document_type = "contract" if contract else "invoice" if invoice else "project"
    document_number = contract.contract_number if contract else invoice.invoice_number if invoice else project_id
    reason = payload.get("reason", "Gatekeeper project suspended")
    audit(db, "suspension_recorded", document_type, document_number, str(reason))
    db.commit()
    return {"contract_number": contract.contract_number if contract else None,
            "invoice_number": invoice.invoice_number if invoice else None,
            "action": "suspension_recorded", "reason": reason}


@app.post("/integrations/gatekeeper/payments")
def record_gatekeeper_payment(payload: dict, db: Session = Depends(get_db), x_gatekeeper_secret: str | None = Header(default=None), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict:
    if not isinstance(idempotency_key, str): idempotency_key = None
    expected = os.getenv("SCRIBED_INTEGRATION_SECRET")
    if not expected or x_gatekeeper_secret != expected:
        raise HTTPException(status_code=401, detail="Invalid integration secret")
    project_id = str(payload.get("project_id", ""))
    service_id = str(payload.get("service_id", "") or "")
    provider_reference = str(payload.get("provider_reference", ""))
    provider = str(payload.get("provider", "unknown"))
    currency = str(payload.get("currency", ""))
    try: paid_amount = Decimal(str(payload.get("amount", "")))
    except (InvalidOperation, ValueError): raise HTTPException(status_code=422, detail="amount must be numeric")
    if not project_id or not provider_reference:
        raise HTTPException(status_code=422, detail="project_id and provider_reference are required")
    if service_id:
        invoice = db.query(Invoice).filter_by(gatekeeper_service_id=service_id, archived_at=None).order_by(Invoice.created_at.desc()).with_for_update().first()
    else:
        invoice = db.query(Invoice).filter_by(gatekeeper_project_id=project_id, archived_at=None).order_by(Invoice.created_at.desc()).with_for_update().first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found for payment owner")
    if currency.upper() != invoice.currency.upper():
        raise HTTPException(status_code=409, detail="Payment currency does not match the invoice")
    if len(provider_reference) > 255 or len(provider) > 30 or (idempotency_key and len(idempotency_key) > 255):
        raise HTTPException(status_code=422, detail="Payment reference, provider, or idempotency key is too long")
    payment_query = db.query(Payment).filter(Payment.provider_reference == provider_reference)
    existing_by_reference = payment_query.first()
    existing_by_key = db.query(Payment).filter_by(idempotency_key=idempotency_key).first() if idempotency_key else None
    if existing_by_reference and existing_by_key and existing_by_reference.id != existing_by_key.id:
        raise HTTPException(status_code=409, detail="Idempotency key and provider reference identify different payments")
    existing = existing_by_reference or existing_by_key
    if existing:
        if existing.invoice_id != invoice.id or existing.provider_reference != provider_reference:
            raise HTTPException(status_code=409, detail="Payment retry does not match the original payment")
        return {"invoice_number": invoice.invoice_number, "receipt_number": existing.receipt_number, "status": invoice.status, "idempotent": True}
    paid_at_raw = payload.get("paid_at")
    if paid_at_raw:
        try:
            paid_at = datetime.fromisoformat(str(paid_at_raw).replace("Z", "+00:00"))
            if paid_at.tzinfo is None:
                paid_at = paid_at.replace(tzinfo=timezone.utc)
            else:
                paid_at = paid_at.astimezone(timezone.utc)
        except ValueError:
            raise HTTPException(status_code=422, detail="paid_at must be an ISO date-time")
    else:
        paid_at = datetime.now(timezone.utc)
    recorded_paid = sum((Decimal(str(item.amount)) for item in db.query(Payment).filter_by(invoice_id=invoice.id).all()), Decimal("0"))
    existing_paid = max(recorded_paid, Decimal(str(invoice.total_payments or 0)))
    if (not paid_amount.is_finite() or paid_amount.as_tuple().exponent < -2
            or paid_amount > Decimal("999999999999.99") or paid_amount <= 0
            or existing_paid + paid_amount > Decimal(str(invoice.amount))):
        raise HTTPException(status_code=409, detail="Payment exceeds the invoice balance")
    total_paid = existing_paid + paid_amount
    invoice_total = Decimal(str(invoice.amount)).quantize(Decimal("0.01"))
    total_paid = total_paid.quantize(Decimal("0.01"))
    invoice.total_payments = total_paid
    invoice.outstanding_balance = max(Decimal("0"), Decimal(str(invoice.base_amount or invoice.amount)) + Decimal(str(invoice.total_adjustments or 0)) - total_paid)
    invoice.status = "paid" if total_paid >= invoice_total else "partially_paid"
    invoice.paid_at = paid_at if invoice.status == "paid" else invoice.paid_at
    receipt_suffix = hashlib.sha256(provider_reference.encode()).hexdigest()[:12]
    receipt_number = f"RCPT-{invoice.invoice_number}-{receipt_suffix}"
    receipt_data = {"receipt_number": receipt_number, "invoice_number": invoice.invoice_number,
                    "client_name": invoice.client_name, "project_name": invoice.project_name,
                    "provider": provider, "provider_reference": provider_reference,
                    "paid_at": paid_at.isoformat(), "amount": f"{paid_amount:,.2f}", "currency": invoice.currency}
    receipt_pdf = render_pdf("receipt.html", receipt_data)
    receipt_path = upload_pdf(f"receipts/{receipt_number}.pdf", receipt_pdf)
    try:
        db.add(Payment(invoice_id=invoice.id, provider=provider, provider_reference=provider_reference, amount=paid_amount, currency=currency, paid_at=paid_at, receipt_number=receipt_number, receipt_path=receipt_path, idempotency_key=idempotency_key))
        invoice.receipt_number = receipt_number
        invoice.receipt_path = receipt_path
        db.flush()
        refresh_invoice_pdf(db, invoice)
        existing_detail = f"provider_reference={provider_reference}"
        audit(db, "payment_received", "invoice", invoice.invoice_number, existing_detail)
        db.commit()
    except Exception:
        db.rollback()
        delete_pdf(receipt_path)
        raise
    return {"invoice_number": invoice.invoice_number, "receipt_number": receipt_number, "status": invoice.status, "paid_at": invoice.paid_at, "idempotent": False}


@app.post("/integrations/gatekeeper/ledger")
def sync_gatekeeper_ledger(payload: LedgerSyncRequest, db: Session = Depends(get_db), x_gatekeeper_secret: str | None = Header(default=None)) -> dict:
    expected = os.getenv("SCRIBED_INTEGRATION_SECRET")
    if not expected or x_gatekeeper_secret != expected:
        raise HTTPException(status_code=401, detail="Invalid integration secret")
    invoice = db.query(Invoice).filter_by(gatekeeper_project_id=payload.project_id, archived_at=None).order_by(Invoice.created_at.desc()).with_for_update().first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found for project")
    if payload.currency.upper() != invoice.currency.upper():
        raise HTTPException(status_code=409, detail="Ledger currency does not match the invoice")
    total_adjustments = payload.net_adjustments
    total_payments = payload.net_payments
    calculated = payload.base_amount + total_adjustments - total_payments
    if calculated < 0 or calculated.quantize(Decimal("0.01")) != payload.outstanding_balance.quantize(Decimal("0.01")):
        raise HTTPException(status_code=409, detail="Ledger totals do not reconcile")
    ledger_fingerprint = fingerprint(payload.model_dump())
    terms = json.loads(invoice.terms_json or "{}")
    previous_version = terms.get("_ledger_version")
    previous_order = numeric_version(previous_version) if isinstance(previous_version, str) else None
    incoming_order = numeric_version(payload.ledger_version)
    if previous_version and payload.ledger_version == "legacy-sync":
        if previous_version != "legacy-sync":
            raise HTTPException(status_code=409, detail="Unversioned ledger snapshot cannot replace a versioned snapshot")
        if terms.get("_ledger_fingerprint") == ledger_fingerprint:
            return {"invoice_number": invoice.invoice_number, "status": invoice.status,
                    "base_amount": str(invoice.base_amount), "total_adjustments": str(invoice.total_adjustments),
                    "total_payments": str(invoice.total_payments), "outstanding_balance": str(invoice.outstanding_balance),
                    "idempotent": True}
        raise HTTPException(status_code=409, detail="Unversioned ledger snapshot cannot replace an existing snapshot")
    if previous_version and payload.ledger_version != "legacy-sync":
        if payload.ledger_version == previous_version:
            if terms.get("_ledger_fingerprint") == ledger_fingerprint:
                return {"invoice_number": invoice.invoice_number, "status": invoice.status,
                        "base_amount": str(invoice.base_amount), "total_adjustments": str(invoice.total_adjustments),
                        "total_payments": str(invoice.total_payments), "outstanding_balance": str(invoice.outstanding_balance),
                        "idempotent": True}
            raise HTTPException(status_code=409, detail="Ledger version was reused with different totals")
        if previous_order is not None and incoming_order is not None and incoming_order < previous_order:
            raise HTTPException(status_code=409, detail="Stale ledger snapshot")
        if previous_order is not None and incoming_order == previous_order:
            if terms.get("_ledger_fingerprint") == ledger_fingerprint:
                return {"invoice_number": invoice.invoice_number, "status": invoice.status,
                        "base_amount": str(invoice.base_amount), "total_adjustments": str(invoice.total_adjustments),
                        "total_payments": str(invoice.total_payments), "outstanding_balance": str(invoice.outstanding_balance),
                        "idempotent": True}
            raise HTTPException(status_code=409, detail="Ledger version was reused with different totals")
    for adjustment in payload.adjustments:
        if not db.query(InvoiceAdjustment).filter_by(gatekeeper_adjustment_id=adjustment.id).first():
            # The same outbox event may be delivered concurrently during a replay.
            # Keep the unique constraint as the authority and isolate a duplicate
            # insert failure so the current invoice snapshot can still be committed.
            try:
                with db.begin_nested():
                    db.add(InvoiceAdjustment(invoice_id=invoice.id, gatekeeper_adjustment_id=adjustment.id, type=adjustment.type,
                                             amount=adjustment.amount, reason=adjustment.reason, actor=adjustment.actor,
                                             created_at=datetime.now(timezone.utc)))
                    db.flush()
            except IntegrityError:
                pass
    invoice.base_amount = payload.base_amount
    invoice.total_adjustments = total_adjustments
    invoice.total_payments = total_payments
    invoice.outstanding_balance = payload.outstanding_balance
    invoice.amount = payload.base_amount + total_adjustments
    invoice.status = "paid" if payload.outstanding_balance == 0 else ("partially_paid" if total_payments > 0 else "sent")
    terms["_ledger_version"] = payload.ledger_version
    terms["_ledger_fingerprint"] = ledger_fingerprint
    invoice.terms_json = json.dumps(terms, default=str)
    refresh_invoice_pdf(db, invoice)
    audit(db, "ledger_synced", "invoice", invoice.invoice_number, f"version={payload.ledger_version} base={payload.base_amount} adjustments={total_adjustments} payments={total_payments} outstanding={payload.outstanding_balance}")
    db.commit()
    return {"invoice_number": invoice.invoice_number, "status": invoice.status, "base_amount": str(invoice.base_amount), "total_adjustments": str(invoice.total_adjustments), "total_payments": str(invoice.total_payments), "outstanding_balance": str(invoice.outstanding_balance), "idempotent": False}


@app.post("/integrations/gatekeeper/services/{gatekeeper_service_id}/adjustments")
def sync_gatekeeper_service_adjustments(gatekeeper_service_id: str, payload: ServiceLedgerSyncRequest, db: Session = Depends(get_db), x_gatekeeper_secret: str | None = Header(default=None)) -> dict:
    expected = os.getenv("SCRIBED_INTEGRATION_SECRET")
    if not expected or x_gatekeeper_secret != expected:
        raise HTTPException(status_code=401, detail="Invalid integration secret")
    if payload.service_id != gatekeeper_service_id:
        raise HTTPException(status_code=409, detail="Service ID does not match the request path")
    invoice = db.query(Invoice).filter_by(gatekeeper_service_id=gatekeeper_service_id, archived_at=None).order_by(Invoice.created_at.desc()).first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found for service")
    for adjustment in payload.adjustments:
        if not db.query(InvoiceAdjustment).filter_by(gatekeeper_adjustment_id=adjustment.id).first():
            try:
                with db.begin_nested():
                    db.add(InvoiceAdjustment(invoice_id=invoice.id, gatekeeper_adjustment_id=adjustment.id, type=adjustment.type,
                                             amount=adjustment.amount, reason=adjustment.reason, actor=adjustment.actor,
                                             created_at=datetime.now(timezone.utc)))
                    db.flush()
            except IntegrityError:
                pass
    adjustments = db.query(InvoiceAdjustment).filter_by(invoice_id=invoice.id).all()
    additional = sum((Decimal(str(item.amount)) for item in adjustments if item.type == "ADDITIONAL_CHARGE"), Decimal("0"))
    discounts = sum((Decimal(str(item.amount)) for item in adjustments if item.type == "DISCOUNT"), Decimal("0"))
    net_adjustments = additional - discounts
    base_amount = Decimal(str(invoice.base_amount))
    if base_amount == 0:
        base_amount = Decimal(str(invoice.amount)) - Decimal(str(invoice.total_adjustments or 0))
    total_amount = base_amount + net_adjustments
    if total_amount <= 0:
        raise HTTPException(status_code=409, detail="Service adjustments cannot reduce the invoice total to zero or below")
    payments = db.query(Payment).filter_by(invoice_id=invoice.id).all()
    total_paid = max(sum((Decimal(str(item.amount)) for item in payments), Decimal("0")),
                     Decimal(str(invoice.total_payments or 0)))
    balance = max(Decimal("0"), total_amount - total_paid)
    invoice.base_amount = base_amount
    invoice.amount = total_amount
    invoice.total_adjustments = net_adjustments
    invoice.total_payments = total_paid
    invoice.outstanding_balance = balance
    invoice.status = "paid" if balance == 0 else ("partially_paid" if total_paid > 0 else "sent")
    refresh_invoice_pdf(db, invoice)
    db.commit()
    audit(db, "service_ledger_synced", "invoice", invoice.invoice_number,
          f"service_id={gatekeeper_service_id} adjustments={net_adjustments} paid={total_paid} balance={balance}")
    db.commit()
    return {"invoice_number": invoice.invoice_number, "status": invoice.status, "base_amount": str(base_amount),
            "total_adjustments": str(net_adjustments), "total_payments": str(total_paid), "outstanding_balance": str(balance)}


@app.get("/invoices/{document_id}/receipt", response_class=Response)
def get_invoice_receipt(document_id: int, db: Session = Depends(get_db)) -> Response:
    document = db.get(Invoice, document_id)
    if not document or not document.receipt_path:
        raise HTTPException(status_code=404, detail="Receipt not found")
    return _file_response(document.receipt_path, document.receipt_number or document.invoice_number)


@app.get("/invoices/{document_id}/receipts")
def list_invoice_receipts(document_id: int, db: Session = Depends(get_db)) -> list[dict]:
    document = db.get(Invoice, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Invoice not found")
    public_base = os.getenv("SCRIBED_PUBLIC_URL", "").rstrip("/")
    return [{"id": item.id, "provider": item.provider, "provider_reference": item.provider_reference,
             "amount": str(item.amount), "currency": item.currency, "paid_at": item.paid_at,
             "receipt_number": item.receipt_number,
             "receipt_url": f"{public_base}/payments/{item.id}/receipt" if public_base else f"/payments/{item.id}/receipt"}
            for item in db.query(Payment).filter_by(invoice_id=document.id).order_by(Payment.paid_at.desc()).all()]


@app.get("/payments/{payment_id}/receipt", response_class=Response)
def get_payment_receipt(payment_id: int, db: Session = Depends(get_db)) -> Response:
    payment = db.get(Payment, payment_id)
    if not payment: raise HTTPException(status_code=404, detail="Payment not found")
    return _file_response(payment.receipt_path, payment.receipt_number)


@app.get("/verify/{number}")
def verify_document(number: str, db: Session = Depends(get_db)) -> dict:
    contract = db.query(Contract).filter_by(contract_number=number).first()
    document_type = "contract"
    if not contract:
        contract = db.query(Invoice).filter_by(invoice_number=number).first()
        document_type = "invoice"
    if not contract: raise HTTPException(status_code=404, detail="Document not found")
    try: actual_hash = hashlib.sha256(read_pdf(contract.pdf_path)).hexdigest() if contract.pdf_path else None
    except (FileNotFoundError, OSError): actual_hash = None
    # The digest itself is safe to publish and lets a recipient independently
    # compare the issued file; no client, project, amount, or storage path is
    # exposed by this public endpoint.
    return {"number": number, "status": contract.status, "issued": bool(contract.pdf_path),
            "hash": contract.pdf_hash,
            "hash_valid": (bool(actual_hash and contract.pdf_hash and actual_hash == contract.pdf_hash)
                           if contract.pdf_path else None)}


@app.post("/contracts/{document_id}/send")
def send_contract(document_id: int, db: Session = Depends(get_db)) -> dict:
    document = db.get(Contract, document_id)
    if not document: raise HTTPException(status_code=404, detail="Contract not found")
    if document.archived_at or document.status != "draft": raise HTTPException(status_code=409, detail="Only active draft contracts can be sent")
    terms = json.loads(document.terms_json)
    if not terms.get("client_email"): raise HTTPException(status_code=400, detail="Contract has no client email")
    pdf = render_pdf("contract.html", contract_render_data(document))
    location = upload_pdf(document_storage_key("contracts", document.contract_number, pdf), pdf)
    document.pdf_path = location
    document.pdf_hash = hashlib.sha256(pdf).hexdigest()
    delivered = False
    try:
        send_pdf(terms["client_email"], f"Contract {document.contract_number}", f"{document.contract_number}.pdf", location)
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
    if not document: raise HTTPException(status_code=404, detail="Invoice not found")
    if document.archived_at or document.status != "draft": raise HTTPException(status_code=409, detail="Only active draft invoices can be sent")
    if not document.client_email: raise HTTPException(status_code=400, detail="Invoice has no client email")
    document.status = "sent"
    location = None
    delivered = False
    try:
        pdf = render_pdf("invoice.html", invoice_render_data(db, document))
        location = upload_pdf(document_storage_key("invoices", document.invoice_number, pdf), pdf)
        document.pdf_path = location
        document.pdf_hash = hashlib.sha256(pdf).hexdigest()
        send_pdf(document.client_email, f"Invoice {document.invoice_number}", f"{document.invoice_number}.pdf", location)
        delivered = True
        audit(db, "sent", "invoice", document.invoice_number)
        db.commit()
    except Exception:
        db.rollback()
        if location and not delivered:
            delete_pdf(location)
        raise
    return {"number": document.invoice_number, "status": document.status}


@app.post("/contracts/{document_id}/mark-accepted")
def mark_contract_accepted(document_id: int, db: Session = Depends(get_db)) -> dict:
    document = db.get(Contract, document_id)
    if not document: raise HTTPException(status_code=404, detail="Contract not found")
    if document.archived_at or document.status != "sent":
        raise HTTPException(status_code=409, detail="Only active sent contracts can be accepted")
    document.status = "accepted"; document.accepted_at = datetime.now(timezone.utc)
    audit(db, "accepted", "contract", document.contract_number)
    db.commit()
    return {"number": document.contract_number, "status": document.status, "accepted_at": document.accepted_at}


@app.post("/invoices/{document_id}/mark-paid")
def mark_invoice_paid(document_id: int, db: Session = Depends(get_db)) -> dict:
    raise HTTPException(status_code=410, detail="Invoices can only be marked paid by a tracked ledger payment event")
