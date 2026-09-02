import json
import hashlib
import logging
import os
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session
from dotenv import load_dotenv

from .db import get_db, next_number
from .models import AuditLog, Contract, Invoice
from .mailer import send_pdf
from .storage import delete_pdf, read_pdf, upload_pdf

load_dotenv()
from .render import render_pdf
from .schemas import ContractRequest, InvoiceRequest

logger = logging.getLogger(__name__)
production = os.getenv("ENVIRONMENT", "development").lower() == "production"
app = FastAPI(title="Scribed", description="mikesplore contract and invoice PDF generation",
              docs_url=None if production else "/docs", redoc_url=None if production else "/redoc",
              openapi_url=None if production else "/openapi.json")
MAX_BODY = 400 * 1024
_requests: dict[str, deque[float]] = defaultdict(deque)

def audit(db, action: str, kind: str, number: str, detail: str | None = None):
    db.add(AuditLog(action=action, document_type=kind, document_number=number, detail=detail))

def fingerprint(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()

def pdf_filename(project_id: str | None, project_name: str, kind: str) -> str:
    """Build a safe, human-friendly download name from the project slug."""
    raw = project_id or project_name
    slug = "-".join("".join(char.lower() if char.isalnum() else "-" for char in raw).split("-"))
    return f"{slug or 'mikesplore'}-{kind}.pdf"

@app.middleware("http")
async def require_api_token(request: Request, call_next):
    if request.headers.get("content-length") and int(request.headers["content-length"]) > MAX_BODY:
        return Response(content='{"detail":"Payload Too Large"}', status_code=413, media_type="application/json")
    path = request.url.path
    expected = os.getenv("SCRIBED_API_TOKEN", "").strip()
    if path in {"/health", "/docs", "/redoc", "/openapi.json"} or path.startswith("/verify/"):
        return await call_next(request)
    if not expected or request.headers.get("authorization") != f"Bearer {expected}":
        return Response(content='{"detail":"Authentication required"}', status_code=401, media_type="application/json")
    key = request.client.host if request.client else "unknown"
    now = time.monotonic(); window = _requests[key]
    while window and now - window[0] > 60: window.popleft()
    if len(window) >= 60:
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
    number = next_number(db, "contract", "MK-CON")
    request_data = request.model_dump()
    request_data["contract_number"] = number
    pdf = render_pdf("contract.html", request_data)
    location = f"contracts/{number}.pdf"
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
    number = next_number(db, "invoice", "MK-INV")
    request_data = request.model_dump()
    request_data["invoice_number"] = number
    pdf = render_pdf("invoice.html", request_data)
    location = f"invoices/{number}.pdf"
    document = Invoice(invoice_number=number, client_name=request.client_name, project_name=request.project_name, amount=request.amount,
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
    document = db.query(Contract).filter_by(gatekeeper_project_id=project_id).order_by(Contract.created_at.desc()).first()
    if not document: raise HTTPException(status_code=404, detail="Contract not found for project")
    return {"contract_number": document.contract_number, "action": "suspension_recorded",
            "reason": payload.get("reason", "Gatekeeper project suspended")}


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
    document = db.get(Invoice, document_id)
    if not document: raise HTTPException(status_code=404, detail="Invoice not found")
    document.status = "paid"; document.paid_at = datetime.now(timezone.utc); db.commit()
    return {"number": document.invoice_number, "status": document.status, "paid_at": document.paid_at}
