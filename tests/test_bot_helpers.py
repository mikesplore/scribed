from decimal import Decimal
import httpx

from bot import api_error, friendly_json, payment_summary


def test_friendly_json_formats_transition_response():
    response = httpx.Response(200, json={"number": "MK-INV-0171", "status": "paid"})

    assert friendly_json(response) == "MK-INV-0171 is now paid."


def test_friendly_json_does_not_expose_raw_response():
    response = httpx.Response(200, json={"number": "MK-INV-0171", "status": "paid"})

    assert "{" not in friendly_json(response)


def test_api_error_extracts_gatekeeper_message_without_raw_json():
    response = httpx.Response(
        404,
        json={"error": "project_not_found", "message": "Project not found"},
        request=httpx.Request("GET", "http://gatekeeper.test/projects/scw"),
    )

    assert api_error(response) == "HTTP 404: Project not found"


def test_payment_summary_uses_successful_payments_only():
    paid, balance = payment_summary(
        {"amountDue": "100000"},
        [{"status": "successful", "amount": "25000"},
         {"status": "pending", "amount": "5000"}],
    )
    assert paid == Decimal("25000")
    assert balance == Decimal("100000")
