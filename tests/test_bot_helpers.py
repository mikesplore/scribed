from decimal import Decimal
from types import SimpleNamespace
import httpx
import pytest

from bot import api_error, friendly_json, owner_only, payment_summary, transition_path


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


def test_bot_only_accepts_owner_in_private_chat():
    from bot import OWNER_ID
    owner = SimpleNamespace(effective_user=SimpleNamespace(id=OWNER_ID),
                             effective_chat=SimpleNamespace(type="private"))
    group = SimpleNamespace(effective_user=SimpleNamespace(id=OWNER_ID),
                            effective_chat=SimpleNamespace(type="group"))
    assert owner_only(owner)
    assert not owner_only(group)


def test_transition_commands_reject_wrong_document_type():
    assert transition_path("send", "invoice", 3) == "/invoices/3/send"
    assert transition_path("accepted", "contract", 3) == "/contracts/3/mark-accepted"
    with pytest.raises(ValueError):
        transition_path("accepted", "invoice", 3)
    with pytest.raises(ValueError):
        transition_path("paid", "contract", 3)
