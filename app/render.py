from pathlib import Path
from datetime import datetime
from decimal import Decimal, InvalidOperation
from markupsafe import escape
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
        return Decimal(str(value if value is not None else 0))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc


def render_pdf(template_name: str, fields: dict[str, Any]) -> bytes:
    """Render a named HTML template to PDF bytes."""
    template = environment.get_template(template_name)
    render_fields = dict(fields)
    # QR generation is optional until the verification asset is configured.
    render_fields.setdefault("qr_code_data_uri", "")
    render_fields.setdefault("amount_paid", None)
    render_fields.setdefault("original_amount", render_fields.get("amount"))
    render_fields.setdefault("due_date", None)
    render_fields.setdefault("payment_instructions", None)
    render_fields.setdefault("client_email", None)
    render_fields.setdefault("payments", [])
    render_fields.setdefault("adjustments", [])
    render_fields["base_amount"] = render_fields.get("base_amount") or render_fields.get("original_amount") or render_fields.get("amount", 0)
    render_fields.setdefault("total_adjustments", 0)
    render_fields.setdefault("outstanding_balance", render_fields.get("amount", 0))
    for field in ("base_amount", "total_adjustments", "outstanding_balance"):
        render_fields[field] = _decimal_value(render_fields[field], field)
    render_fields.setdefault("payment_portal_url", None)
    render_fields.setdefault("status", None)
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
        render_fields["items"] = [
            {**item, "amount": _decimal_value(item.get("amount"), "item amount")}
            for item in render_fields["items"]
        ]
    render_fields.setdefault("invoice_notes", None)
    for field in ("issue_date", "due_date"):
        value = render_fields.get(field)
        if value:
            try:
                render_fields[field] = datetime.fromisoformat(str(value)).strftime("%d %b %Y")
            except (TypeError, ValueError):
                pass
    render_fields.setdefault("total_paid", render_fields.get("amount_paid") or 0)
    normalized_payments = []
    for payment in render_fields["payments"]:
        item = dict(payment)
        raw_date = item.get("paidAt") or item.get("createdAt")
        try:
            item["display_date"] = datetime.fromisoformat(str(raw_date).replace("Z", "+00:00")).strftime("%d %b %Y")
        except (TypeError, ValueError):
            item["display_date"] = "Unknown date"
        try:
            item["display_amount"] = f"{float(item.get('amount', 0)):,.2f}"
        except (TypeError, ValueError):
            item["display_amount"] = "0.00"
        normalized_payments.append(item)
    render_fields["payments"] = normalized_payments
    paid_value = render_fields.get("total_paid")
    if paid_value is None:
        paid_value = render_fields.get("amount_paid") or 0
    try:
        render_fields["total_paid"] = f"{float(paid_value or 0):,.2f}"
    except (TypeError, ValueError):
        render_fields["total_paid"] = "0.00"
    bundled_logo = TEMPLATE_DIR / "ScribedLogo.png"
    render_fields.setdefault(
        "logo_url",
        bundled_logo.as_uri() if bundled_logo.is_file() else "",
    )
    html = template.render(**render_fields)
    # Keep the fixed verification strip clear of the final page's content.
    html = html.replace("</head>", "<style>@page{margin-bottom:48mm}</style></head>")
    if render_fields["logo_url"]:
        logo = f'<img src="{escape(render_fields["logo_url"])}" class="brand-logo" style="max-height:52px;max-width:190px;width:auto;height:auto;display:block" alt="mikesplore">'
        html = html.replace('<div class="brand-mark">mikesplore</div>', logo)
        html = html.replace('<div class="brand">mikesplore</div>', logo)
    return HTML(string=html, base_url=str(TEMPLATE_DIR)).write_pdf()
