import json
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from .db import get_db, init_db, next_number, storage_path
from .models import Contract, Invoice
from .render import render_pdf
from .schemas import ContractRequest, InvoiceRequest

app = FastAPI(title="Scribed", description="mikesplore contract and invoice PDF generation")
init_db()


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
                        terms_json=json.dumps(request_data, default=str), pdf_path=str(path))
    db.add(document)
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
                       currency=request.currency, pdf_path=str(path))
    db.add(document)
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
