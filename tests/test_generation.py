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


def test_invoice_snapshot_accepts_paid_to_date_and_adjustment_items():
    request = InvoiceRequest(
        client_name="Ada Lovelace", project_name="Website", description="Website build",
        amount="90", paid_amount="54", balance_due="36",
        items=[{"name": "Website", "amount": "100"}, {"name": "Discount", "amount": "-10"}],
    )
    assert request.paid_amount == 54
    assert request.balance_due == 36
    assert request.items[1]["amount"] == -10


def test_live_invoice_pdf_receives_current_payment_snapshot(monkeypatch):
    from app import main
    captured = {}
    monkeypatch.setattr(main, "render_pdf", lambda _template, fields: captured.update(fields) or b"%PDF-test")
    response = generate_invoice(InvoiceRequest(
        client_name="Ada", project_name="Website", description="Build",
        amount="100", paid_amount="54", balance_due="46", payment_status="partially_paid",
        invoice_number="GK-12345678-1234-1234-1234-123456789abc",
    ))
    assert response.body == b"%PDF-test"
    assert captured["amount"] == 100
    assert captured["paid_amount"] == 54
    assert captured["balance_due"] == 46
    assert captured["payment_status"] == "partially_paid"


def test_invoice_rejects_inconsistent_paid_and_balance_values():
    import pytest
    with pytest.raises(ValueError):
        InvoiceRequest(client_name="Ada", project_name="Website", description="Build",
                       amount="100", paid_amount="54", balance_due="100")


def test_invoice_rejects_line_items_that_do_not_match_totals():
    import pytest
    with pytest.raises(ValueError):
        InvoiceRequest(client_name="Ada", project_name="Website", description="Build", amount="100",
                       items=[{"name": "Hosting", "quantity": "2", "unit_price": "40", "amount": "80"}])


def test_invoice_layout_uses_service_name_and_portal_payment_instruction():
    from app.render import environment
    fields = InvoiceRequest(
        client_name="Ada", project_name="Gatekeeperd", description="Internal service description",
        amount="100", invoice_number="MK-INV-000123",
        items=[{"name": "API hosting", "description": "Internal identifier 123", "amount": "100"}],
    ).model_dump()
    fields.update(from_name="mikesplore", from_tagline="Independent digital work", from_address="Mombasa",
                  from_phone="0700000000", from_email=None, logo_url="", prepared_on_behalf_of=None,
                  payment_status=None, status=None, verification_url="", qr_code_data_uri="", invoice_notes=None)
    html = environment.get_template("invoice.html").render(**fields)
    assert "API hosting" in html
    assert "Internal identifier 123" not in html
    assert "Log in to your portal using your email address to pay." in html
    assert "Payment reference" not in html


def test_project_download_filename_uses_project_name():
    response = generate_invoice(InvoiceRequest(**{
        "client_name": "Ada", "project_name": "Website", "description": "Build",
        "amount": "120000",
    }))
    assert 'filename="website.pdf"' in response.headers["content-disposition"]


def test_invoice_verification_url_is_not_duplicated():
    response = generate_invoice(InvoiceRequest(client_name="Ada", project_name="Website", description="Build", amount="500"))
    from app.render import render_pdf
    pdf = render_pdf("invoice.html", {**InvoiceRequest(client_name="Ada", project_name="Website", description="Build", amount="500").model_dump(), "invoice_number": "MK-INV-0001"})
    assert b"scribed.scribed" not in pdf


def test_invalid_amount_is_rejected():
    import pytest
    with pytest.raises(ValueError):
        InvoiceRequest(client_name="Ada", project_name="X", description="X", amount=0)


def test_invoice_rejects_unsafe_or_malformed_input():
    import pytest
    with pytest.raises(ValueError):
        InvoiceRequest(client_name="Ada", project_name="X", description="X", amount="Infinity")
    with pytest.raises(ValueError):
        InvoiceRequest(client_name="Ada", project_name="X", description="X", amount="1.001")
    with pytest.raises(ValueError):
        InvoiceRequest(client_name="Ada", project_name="X", description="X", amount=1,
                       client_email="not-an-email")
