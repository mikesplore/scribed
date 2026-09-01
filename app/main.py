import json
import hashlib
import logging
import os
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session
from dotenv import load_dotenv

from .db import get_db, next_number, storage_path
from .models import AuditLog, Contract, Invoice
from .mailer import send_pdf

load_dotenv()
from .render import render_pdf
from .schemas import ContractRequest, InvoiceRequest

logger = logging.getLogger(__name__)
app = FastAPI(title="Scribed", description="mikesplore contract and invoice PDF generation")
_requests: dict[str, deque[float]] = defaultdict(deque)

def audit(db, action: str, kind: str, number: str, detail: str | None = None):
    db.add(AuditLog(action=action, document_type=kind, document_number=number, detail=detail))

@app.middleware("http")
async def require_api_token(request: Request, call_next):
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
    return {"status": "ok"}


@app.post("/generate/contract", response_class=Response)
def generate_contract(request: ContractRequest) -> Response:
    pdf = render_pdf("contract.html", request.model_dump())
    return Response(content=pdf, media_type="application/pdf")


@app.post("/contracts", response_class=Response)
def create_contract(request: ContractRequest, db: Session = Depends(get_db)) -> Response:
    number = next_number(db, "contract", "MK-CON")
    request_data = request.model_dump()
    request_data["contract_number"] = number
    pdf = render_pdf("contract.html", request_data)
    path = storage_path("contracts", number)
    path.write_bytes(pdf)
    document = Contract(contract_number=number, client_name=request.client_name, project_name=request.project_name,
                        gatekeeper_project_id=request.gatekeeper_project_id,
                        terms_json=json.dumps(request_data, default=str), pdf_path=str(path),
                        pdf_hash=hashlib.sha256(pdf).hexdigest())
    db.add(document)
    audit(db, "created", "contract", number)
    db.commit()
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{number}.pdf"'},
    )


@app.post("/generate/invoice", response_class=Response)
def generate_invoice(request: InvoiceRequest) -> Response:
    pdf = render_pdf("invoice.html", request.model_dump())
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{request.invoice_number}.pdf"'},
    )


@app.post("/invoices", response_class=Response)
def create_invoice(request: InvoiceRequest, db: Session = Depends(get_db)) -> Response:
    number = next_number(db, "invoice", "MK-INV")
    request_data = request.model_dump()
    request_data["invoice_number"] = number
    pdf = render_pdf("invoice.html", request_data)
    path = storage_path("invoices", number)
    path.write_bytes(pdf)
    document = Invoice(invoice_number=number, client_name=request.client_name, amount=request.amount,
                       client_email=request.client_email, currency=request.currency, pdf_path=str(path),
                       pdf_hash=hashlib.sha256(pdf).hexdigest())
    db.add(document)
    audit(db, "created", "invoice", number)
    db.commit()
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{number}.pdf"'})


def _file_response(path: str, filename: str) -> Response:
    if not Path(path).is_file():
        raise HTTPException(status_code=404, detail="PDF file not found")
    return Response(content=Path(path).read_bytes(), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{filename}.pdf"'})


@app.get("/contracts/{document_id}", response_class=Response)
def get_contract(document_id: int, db: Session = Depends(get_db)) -> Response:
    document = db.get(Contract, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Contract not found")
    return _file_response(document.pdf_path, document.contract_number)


@app.get("/invoices/{document_id}", response_class=Response)
def get_invoice(document_id: int, db: Session = Depends(get_db)) -> Response:
    document = db.get(Invoice, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return _file_response(document.pdf_path, document.invoice_number)


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
    path = Path(document.pdf_path)
    if path.is_file(): path.unlink()
    number = document.contract_number; audit(db, "permanently_deleted", "contract", number); db.delete(document); db.commit()
    return {"deleted": True, "number": number}


@app.get("/invoices")
def list_invoices(db: Session = Depends(get_db)) -> list[dict]:
    return [{"id": item.id, "number": item.invoice_number, "client_name": item.client_name,
             "status": item.status, "created_at": item.created_at}
            for item in db.query(Invoice).filter_by(archived_at=None).order_by(Invoice.created_at.desc()).all()]


@app.get("/audit/{number}")
def document_audit(number: str, db: Session = Depends(get_db)) -> list[dict]:
    return [{"action": item.action, "type": item.document_type, "number": item.document_number,
             "detail": item.detail, "created_at": item.created_at}
            for item in db.query(AuditLog).filter_by(document_number=number).order_by(AuditLog.created_at.asc()).all()]


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
    path = Path(contract.pdf_path)
    actual_hash = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    return {"number": number, "type": document_type, "status": contract.status,
            "issued": True, "hash": contract.pdf_hash, "hash_valid": actual_hash == contract.pdf_hash}


@app.post("/contracts/{document_id}/send")
def send_contract(document_id: int, db: Session = Depends(get_db)) -> dict:
    document = db.get(Contract, document_id)
    if not document: raise HTTPException(status_code=404, detail="Contract not found")
    if document.status != "draft": raise HTTPException(status_code=409, detail="Only draft contracts can be sent")
    terms = json.loads(document.terms_json)
    if not terms.get("client_email"): raise HTTPException(status_code=400, detail="Contract has no client email")
    send_pdf(terms["client_email"], f"Contract {document.contract_number}", f"{document.contract_number}.pdf", document.pdf_path)
    document.status = "sent"; db.commit()
    return {"number": document.contract_number, "status": document.status}


@app.post("/invoices/{document_id}/send")
def send_invoice(document_id: int, db: Session = Depends(get_db)) -> dict:
    document = db.get(Invoice, document_id)
    if not document: raise HTTPException(status_code=404, detail="Invoice not found")
    if document.status != "draft": raise HTTPException(status_code=409, detail="Only draft invoices can be sent")
    if not document.client_email: raise HTTPException(status_code=400, detail="Invoice has no client email")
    send_pdf(document.client_email, f"Invoice {document.invoice_number}", f"{document.invoice_number}.pdf", document.pdf_path)
    document.status = "sent"; db.commit()
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
