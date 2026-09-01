from decimal import Decimal

from bot import payment_summary


def test_payment_summary_uses_successful_payments_only():
    paid, balance = payment_summary(
        {"amountDue": "100000"},
        [{"status": "successful", "amount": "25000"},
         {"status": "pending", "amount": "5000"}],
    )
    assert paid == Decimal("25000")
    assert balance == Decimal("75000")
