from app.main import generate_contract, generate_invoice
from app.schemas import ContractRequest, InvoiceRequest


def contract_request(**overrides):
    data = {"client_name": "Ada Lovelace", "project_name": "Website", "scope": "A marketing website.",
            "deliverables": ["Design", "Build"], "timeline": "4 weeks", "amount": "120000",
            "payment_schedule": "50% upfront and 50% on delivery"}
    data.update(overrides)
    return ContractRequest(**data)


def test_contract_generates_pdf():
    response = generate_contract(ContractRequest(**{
        "client_name": "Ada Lovelace", "project_name": "Website", "scope": "A marketing website.",
        "deliverables": ["Design", "Build"], "timeline": "4 weeks", "amount": "120000",
        "payment_schedule": "50% upfront and 50% on delivery", "contract_number": "MK-CON-0001",
    }))
    assert response.media_type == "application/pdf"
    assert response.body.startswith(b"%PDF")


def test_invoice_generates_pdf():
    response = generate_invoice(InvoiceRequest(**{
        "client_name": "Ada Lovelace", "project_name": "Website", "description": "Website build",
        "amount": "120000", "invoice_number": "MK-INV-0001",
    }))
    assert response.body.startswith(b"%PDF")


def test_project_download_filename_uses_slug():
    response = generate_invoice(InvoiceRequest(**{
        "client_name": "Ada", "project_name": "Website", "description": "Build",
        "amount": "120000", "gatekeeper_project_id": "client-portal",
    }))
    assert 'filename="client-portal-invoice.pdf"' in response.headers["content-disposition"]


def test_invalid_amount_is_rejected():
    import pytest
    with pytest.raises(ValueError):
        InvoiceRequest(client_name="Ada", project_name="X", description="X", amount=0)
