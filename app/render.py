from pathlib import Path
import os
from datetime import datetime
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
    render_fields.setdefault("payment_portal_url", None)
    render_fields.setdefault("status", None)
    render_fields.setdefault("from_address", "Leisure, Mombasa, 80100")
    render_fields.setdefault("from_phone", "0745434759")
    render_fields.setdefault("from_tax_id", None)
    render_fields.setdefault("from_name", "mikesplore")
    render_fields.setdefault("from_tagline", "Independent digital work")
    render_fields.setdefault("items", None)
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
    try:
        render_fields["total_paid"] = f"{float(render_fields.get('amount_paid') or 0):,.2f}"
    except (TypeError, ValueError):
        render_fields["total_paid"] = "0.00"
    public_url = os.getenv("R2_PUBLIC_URL", "").rstrip("/")
    render_fields.setdefault(
        "logo_url",
        f"https://i.ibb.co/ymbBfLfs/logocool.png",
    )
    html = template.render(**render_fields)
    if render_fields["logo_url"]:
        logo = f'<img src="{escape(render_fields["logo_url"])}" class="brand-logo" style="max-height:52px;max-width:190px;width:auto;height:auto;display:block" alt="mikesplore">'
        html = html.replace('<div class="brand-mark">mikesplore</div>', logo)
        html = html.replace('<div class="brand">mikesplore</div>', logo)
    return HTML(string=html, base_url=str(TEMPLATE_DIR)).write_pdf()
