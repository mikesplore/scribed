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

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
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
from .schemas import ContractRequest, InvoiceRequest, LedgerSyncRequest

logger = logging.getLogger(__name__)
production = os.getenv("ENVIRONMENT", "development").lower() == "production"
telegram_application = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global telegram_application
    webhook_url = os.getenv("TELEGRAM_WEBHOOK_URL", "").rstrip("/")
    if webhook_url and os.getenv("TELEGRAM_BOT_TOKEN"):
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

def refresh_invoice_pdf(db, invoice: Invoice) -> None:
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
    pdf = render_pdf("invoice.html", data)
    invoice.pdf_path = upload_pdf(f"invoices/{pdf_filename(invoice.gatekeeper_project_id, invoice.project_name, 'invoice')}", pdf)

def fingerprint(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()

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


def existing_project_document(db: Session, model, project_name: str):
    """Return an existing document for this project, including archived rows."""
    normalized_name = project_name.strip().lower()
    return db.query(model).filter(
        func.lower(func.trim(model.project_name)) == normalized_name
    ).first()

@app.middleware("http")
async def require_api_token(request: Request, call_next):
    if request.headers.get("content-length") and int(request.headers["content-length"]) > MAX_BODY:
        return Response(content='{"detail":"Payload Too Large"}', status_code=413, media_type="application/json")
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
    if expected and request.headers.get("x-telegram-bot-api-secret-token") != expected:
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
    if not isinstance(idempotency_key, str): idempotency_key = None
    if idempotency_key:
        existing = db.query(Contract).filter_by(idempotency_key=idempotency_key).first()
        if existing:
            if existing.request_fingerprint != fingerprint(request.model_dump()):
                raise HTTPException(status_code=409, detail="Idempotency-Key was already used with different data")
            return _file_response(existing.pdf_path, existing.contract_number,
                                  existing.gatekeeper_project_id, existing.project_name, "contract")
    existing = existing_project_document(db, Contract, request.project_name)
    if existing:
        raise HTTPException(status_code=409, detail="A contract already exists for this project")
    number = next_number(db, "contract", "MK-CON")
    request_data = request.model_dump()
    request_data["contract_number"] = number
    pdf = render_pdf("contract.html", request_data)
    location = f"contracts/{pdf_filename(request.gatekeeper_project_id, request.project_name, 'contract')}"
    document = Contract(contract_number=number, client_name=request.client_name, project_name=request.project_name,
                        gatekeeper_project_id=request.gatekeeper_project_id,
                        terms_json=json.dumps(request_data, default=str), pdf_path=location,
                        pdf_hash=hashlib.sha256(pdf).hexdigest(), idempotency_key=idempotency_key,
                        request_fingerprint=fingerprint(request.model_dump()))
    db.add(document)
    audit(db, "created", "contract", number)
    db.commit()
    document.pdf_path = upload_pdf(location, pdf)
    db.commit()
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
    if not isinstance(idempotency_key, str): idempotency_key = None
    if idempotency_key:
        existing = db.query(Invoice).filter_by(idempotency_key=idempotency_key).first()
        if existing:
            if existing.request_fingerprint != fingerprint(request.model_dump()):
                raise HTTPException(status_code=409, detail="Idempotency-Key was already used with different data")
            return _file_response(existing.pdf_path, existing.invoice_number,
                                  existing.gatekeeper_project_id, existing.project_name, "invoice")
    existing = existing_project_document(db, Invoice, request.project_name)
    if existing:
        raise HTTPException(status_code=409, detail="An invoice already exists for this project")
    number = next_number(db, "invoice", "MK-INV")
    request_data = request.model_dump()
    request_data["invoice_number"] = number
    pdf = render_pdf("invoice.html", request_data)
    location = f"invoices/{pdf_filename(request.gatekeeper_project_id, request.project_name, 'invoice')}"
    document = Invoice(invoice_number=number, client_name=request.client_name, project_name=request.project_name, amount=request.amount,
                       base_amount=request.original_amount or request.amount, outstanding_balance=request.amount,
                       client_email=request.client_email, gatekeeper_project_id=request.gatekeeper_project_id,
                       currency=request.currency, terms_json=json.dumps(request_data, default=str), pdf_path=location,
                       pdf_hash=hashlib.sha256(pdf).hexdigest(), idempotency_key=idempotency_key,
                       request_fingerprint=fingerprint(request.model_dump()))
    db.add(document)
    audit(db, "created", "invoice", number)
    db.commit()
    document.pdf_path = upload_pdf(location, pdf)
    db.commit()
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{pdf_filename(request.gatekeeper_project_id, request.project_name, "invoice")}"'})


@app.post("/contracts/{document_id}/duplicate", response_class=Response)
def duplicate_contract(document_id: int, db: Session = Depends(get_db)) -> Response:
    source = db.get(Contract, document_id)
    if not source: raise HTTPException(status_code=404, detail="Contract not found")
    data = json.loads(source.terms_json)
    data.pop("contract_number", None)
    data["gatekeeper_project_id"] = source.gatekeeper_project_id
    return create_contract(ContractRequest(**data), db)


@app.post("/invoices/{document_id}/duplicate", response_class=Response)
def duplicate_invoice(document_id: int, db: Session = Depends(get_db)) -> Response:
    source = db.get(Invoice, document_id)
    if not source: raise HTTPException(status_code=404, detail="Invoice not found")
    data = json.loads(source.terms_json)
    data.pop("invoice_number", None)
    data["gatekeeper_project_id"] = source.gatekeeper_project_id
    return create_invoice(InvoiceRequest(**data), db)


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


@app.get("/contracts/{document_id}", response_class=Response)
def get_contract(document_id: int, db: Session = Depends(get_db)) -> Response:
    document = db.get(Contract, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Contract not found")
    return _file_response(document.pdf_path, document.contract_number,
                          document.gatekeeper_project_id, document.project_name, "contract")


@app.get("/invoices/{document_id}", response_class=Response)
def get_invoice(document_id: int, db: Session = Depends(get_db)) -> Response:
    document = db.get(Invoice, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return _file_response(document.pdf_path, document.invoice_number,
                          document.gatekeeper_project_id, document.project_name, "invoice")


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
    terms.update(request.model_dump(exclude_none=True))
    terms["invoice_number"] = invoice.invoice_number
    recorded_paid = sum((Decimal(str(item.amount)) for item in db.query(Payment).filter_by(invoice_id=invoice.id).all()), Decimal("0"))
    gatekeeper_paid = Decimal(str(request.amount_paid or 0))
    invoice.client_name = request.client_name
    invoice.client_email = request.client_email
    invoice.project_name = request.project_name
    invoice.gatekeeper_project_id = project_id
    invoice.amount = request.amount
    invoice.base_amount = request.original_amount or request.amount
    invoice.total_payments = max(recorded_paid, gatekeeper_paid)
    invoice.outstanding_balance = max(Decimal("0"), Decimal(str(invoice.amount)) - Decimal(str(invoice.total_payments)))
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
    payments = db.query(Payment).filter_by(invoice_id=invoice.id).order_by(Payment.paid_at.desc()).all()
    paid = sum((Decimal(str(item.amount)) for item in payments), Decimal("0"))
    public_base = os.getenv("SCRIBED_PUBLIC_URL", "").rstrip("/")
    return {
        "invoice": {"id": invoice.id, "number": invoice.invoice_number, "status": invoice.status,
                    "amount": str(invoice.amount), "currency": invoice.currency,
                    "paid": str(paid), "balance": str(max(Decimal("0"), Decimal(str(invoice.amount)) - paid))},
        "payments": [{"id": item.id, "provider": item.provider, "provider_reference": item.provider_reference,
                      "amount": str(item.amount), "currency": item.currency, "paid_at": item.paid_at,
                      "receipt_number": item.receipt_number, "receipt_url": f"{public_base}/payments/{item.id}/receipt" if public_base else f"/payments/{item.id}/receipt"} for item in payments]
    }


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
    provider_reference = str(payload.get("provider_reference", ""))
    provider = str(payload.get("provider", "unknown"))
    currency = str(payload.get("currency", ""))
    try: paid_amount = Decimal(str(payload.get("amount", "")))
    except (InvalidOperation, ValueError): raise HTTPException(status_code=422, detail="amount must be numeric")
    if not project_id or not provider_reference:
        raise HTTPException(status_code=422, detail="project_id and provider_reference are required")
    invoice = db.query(Invoice).filter_by(gatekeeper_project_id=project_id, archived_at=None).order_by(Invoice.created_at.desc()).first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found for project")
    if currency.upper() != invoice.currency.upper():
        raise HTTPException(status_code=409, detail="Payment currency does not match the invoice")
    payment_query = db.query(Payment).filter(Payment.provider_reference == provider_reference)
    if idempotency_key:
        payment_query = db.query(Payment).filter((Payment.provider_reference == provider_reference) | (Payment.idempotency_key == idempotency_key))
    existing = payment_query.first()
    if existing:
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
    existing_paid = sum((Decimal(str(item.amount)) for item in db.query(Payment).filter_by(invoice_id=invoice.id).all()), Decimal("0"))
    if paid_amount <= 0 or existing_paid + paid_amount > Decimal(str(invoice.amount)):
        raise HTTPException(status_code=409, detail="Payment exceeds the invoice balance")
    total_paid = existing_paid + paid_amount
    invoice_total = Decimal(str(invoice.amount)).quantize(Decimal("0.01"))
    total_paid = total_paid.quantize(Decimal("0.01"))
    invoice.total_payments = total_paid
    invoice.outstanding_balance = max(Decimal("0"), Decimal(str(invoice.base_amount or invoice.amount)) + Decimal(str(invoice.total_adjustments or 0)) - total_paid)
    invoice.status = "paid" if total_paid >= invoice_total else "partially_paid"
    invoice.paid_at = paid_at if invoice.status == "paid" else invoice.paid_at
    receipt_number = f"RCPT-{invoice.invoice_number}-{provider_reference[-8:]}"
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
    invoice = db.query(Invoice).filter_by(gatekeeper_project_id=payload.project_id, archived_at=None).order_by(Invoice.created_at.desc()).first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found for project")
    if payload.currency.upper() != invoice.currency.upper():
        raise HTTPException(status_code=409, detail="Ledger currency does not match the invoice")
    total_adjustments = payload.net_adjustments
    total_payments = payload.net_payments
    calculated = payload.base_amount + total_adjustments - total_payments
    if calculated < 0 or calculated.quantize(Decimal("0.01")) != payload.outstanding_balance.quantize(Decimal("0.01")):
        raise HTTPException(status_code=409, detail="Ledger totals do not reconcile")
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
    refresh_invoice_pdf(db, invoice)
    db.commit()
    audit(db, "ledger_synced", "invoice", invoice.invoice_number, f"version={payload.ledger_version} base={payload.base_amount} adjustments={total_adjustments} payments={total_payments} outstanding={payload.outstanding_balance}")
    db.commit()
    return {"invoice_number": invoice.invoice_number, "status": invoice.status, "base_amount": str(invoice.base_amount), "total_adjustments": str(invoice.total_adjustments), "total_payments": str(invoice.total_payments), "outstanding_balance": str(invoice.outstanding_balance)}


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
    try: actual_hash = hashlib.sha256(read_pdf(contract.pdf_path)).hexdigest()
    except (FileNotFoundError, OSError): actual_hash = None
    # The digest itself is safe to publish and lets a recipient independently
    # compare the issued file; no client, project, amount, or storage path is
    # exposed by this public endpoint.
    return {"number": number, "status": contract.status, "issued": True,
            "hash": contract.pdf_hash, "hash_valid": actual_hash == contract.pdf_hash}


@app.post("/contracts/{document_id}/send")
def send_contract(document_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)) -> dict:
    document = db.get(Contract, document_id)
    if not document: raise HTTPException(status_code=404, detail="Contract not found")
    if document.status != "draft": raise HTTPException(status_code=409, detail="Only draft contracts can be sent")
    terms = json.loads(document.terms_json)
    if not terms.get("client_email"): raise HTTPException(status_code=400, detail="Contract has no client email")
    document.status = "sent"; db.commit()
    background_tasks.add_task(send_pdf, terms["client_email"], f"Contract {document.contract_number}", f"{document.contract_number}.pdf", document.pdf_path)
    return {"number": document.contract_number, "status": document.status}


@app.post("/invoices/{document_id}/send")
def send_invoice(document_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)) -> dict:
    document = db.get(Invoice, document_id)
    if not document: raise HTTPException(status_code=404, detail="Invoice not found")
    if document.status != "draft": raise HTTPException(status_code=409, detail="Only draft invoices can be sent")
    if not document.client_email: raise HTTPException(status_code=400, detail="Invoice has no client email")
    document.status = "sent"; db.commit()
    background_tasks.add_task(send_pdf, document.client_email, f"Invoice {document.invoice_number}", f"{document.invoice_number}.pdf", document.pdf_path)
    return {"number": document.invoice_number, "status": document.status}


@app.post("/contracts/{document_id}/mark-accepted")
def mark_contract_accepted(document_id: int, db: Session = Depends(get_db)) -> dict:
    document = db.get(Contract, document_id)
    if not document: raise HTTPException(status_code=404, detail="Contract not found")
    document.status = "accepted"; document.accepted_at = datetime.now(timezone.utc); db.commit()
    return {"number": document.contract_number, "status": document.status, "accepted_at": document.accepted_at}


@app.post("/invoices/{document_id}/mark-paid")
def mark_invoice_paid(document_id: int, db: Session = Depends(get_db)) -> dict:
    raise HTTPException(status_code=410, detail="Invoices can only be marked paid by a tracked ledger payment event")
