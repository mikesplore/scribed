from pathlib import Path
from datetime import datetime
from decimal import Decimal, InvalidOperation
import multiprocessing
import os
import resource
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from weasyprint import HTML


BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = BASE_DIR / "templates"

environment = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    autoescape=select_autoescape(("html", "xml")),
    undefined=StrictUndefined,
)


def _decimal_value(value: Any, field: str) -> Decimal:
    try:
        number = Decimal(str(value if value is not None else 0))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not number.is_finite() or number.copy_abs() > Decimal("999999999999.99") or number.as_tuple().exponent < -2:
        raise ValueError(f"{field} must be finite and have at most two decimal places")
    return number


def _render_pdf(template_name: str, fields: dict[str, Any]) -> bytes:
    """Render a named HTML template to PDF bytes in the isolated worker."""
    template = environment.get_template(template_name)
    render_fields = dict(fields)
    # QR generation is optional until the verification asset is configured.
    render_fields.setdefault("qr_code_data_uri", "")
    render_fields.setdefault("due_date", None)
    render_fields.setdefault("payment_instructions", None)
    render_fields.setdefault("client_email", None)
    render_fields.setdefault("prepared_on_behalf_of", None)
    render_fields["amount"] = _decimal_value(render_fields.get("amount"), "amount")
    render_fields.setdefault("status", None)
    render_fields.setdefault("payment_status", None)
    render_fields.setdefault("from_address", "Leisure, Mombasa, 80100")
    render_fields.setdefault("from_phone", "0745434759")
    render_fields.setdefault("from_email", None)
    render_fields.setdefault("from_tax_id", None)
    render_fields.setdefault("from_name", "mikesplore")
    render_fields.setdefault("from_tagline", "Independent digital work")
    client_name = str(render_fields.get("client_name", "")).strip()
    render_fields.setdefault("client_first_name", client_name.split()[0] if client_name else "Client")
    render_fields.setdefault("items", None)
    if render_fields["items"]:
        normalized_items = []
        for item in render_fields["items"]:
            amount = _decimal_value(item.get("amount"), "item amount")
            quantity = _decimal_value(item.get("quantity", 1), "item quantity")
            unit_price = _decimal_value(item.get("unit_price", amount / quantity), "item unit price")
            if quantity <= 0:
                raise ValueError("item quantity must be greater than zero")
            normalized_items.append({**item, "amount": amount, "quantity": quantity, "unit_price": unit_price})
        render_fields["items"] = normalized_items
    render_fields["paid_amount"] = _decimal_value(render_fields.get("paid_amount", 0), "paid_amount")
    raw_balance = render_fields.get("balance_due")
    render_fields["balance_due"] = (
        _decimal_value(raw_balance, "balance_due") if raw_balance is not None
        else max(render_fields["amount"] - render_fields["paid_amount"], Decimal("0.00"))
    )
    render_fields.setdefault("invoice_notes", None)
    for field in ("issue_date", "due_date", "received_date"):
        value = render_fields.get(field)
        if value:
            try:
                render_fields[field] = datetime.fromisoformat(str(value)).strftime("%d %b %Y")
            except (TypeError, ValueError):
                pass
    render_fields.setdefault("logo_url", "")
    html = template.render(**render_fields)
    return HTML(string=html, base_url=str(TEMPLATE_DIR)).write_pdf()


def _render_worker(connection, template_name: str, fields: dict[str, Any], memory_limit: int) -> None:
    try:
        resource.setrlimit(resource.RLIMIT_AS, (memory_limit, memory_limit))
        resource.setrlimit(resource.RLIMIT_CPU, (30, 31))
        connection.send((True, _render_pdf(template_name, fields)))
    except BaseException as exc:
        connection.send((False, f"{type(exc).__name__}: {exc}"))
    finally:
        connection.close()


def render_pdf(template_name: str, fields: dict[str, Any]) -> bytes:
    """Render with a killable worker and bounded CPU, memory, and wall time."""
    timeout = max(1, int(os.getenv("PDF_RENDER_TIMEOUT_SECONDS", "25")))
    memory_limit = max(128, int(os.getenv("PDF_RENDER_MEMORY_MB", "512"))) * 1024 * 1024
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe(duplex=False)
    worker = context.Process(target=_render_worker,
                             args=(child, template_name, fields, memory_limit), daemon=True)
    worker.start()
    child.close()
    try:
        if not parent.poll(timeout):
            worker.kill()
            worker.join()
            raise TimeoutError(f"PDF rendering exceeded {timeout} seconds")
        succeeded, result = parent.recv()
        worker.join(timeout=1)
        if not succeeded:
            raise RuntimeError(f"PDF rendering failed: {result}")
        return result
    finally:
        parent.close()
        if worker.is_alive():
            worker.kill()
            worker.join()
