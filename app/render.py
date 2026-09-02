from pathlib import Path
import os
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
    render_fields.setdefault("status", None)
    public_url = os.getenv("R2_PUBLIC_URL", "").rstrip("/")
    render_fields.setdefault(
        "logo_url",
        f"{public_url}/logo/logo.png" if public_url else "https://i.ibb.co/xS0CwpSn/logo.png",
    )
    html = template.render(**render_fields)
    if render_fields["logo_url"]:
        logo = f'<img src="{escape(render_fields["logo_url"])}" class="brand-logo" alt="mikesplore">'
        html = html.replace('<div class="brand-mark">mikesplore</div>', logo)
        html = html.replace('<div class="brand">mikesplore</div>', logo)
    payments = render_fields.get("payments") or []
    if payments:
        rows = []
        for payment in payments:
            status = str(payment.get("status", "")).lower()
            if status not in {"paid", "success", "successful", "completed"}:
                continue
            rows.append(
                "<tr class=\"paid-row\"><td>Payment — "
                f"{escape(payment.get('paidAt', payment.get('createdAt', '')))}</td>"
                f"<td class=\"amount-col\">{escape(render_fields.get('currency', ''))} "
                f"{escape(payment.get('amount', '0'))}</td></tr>"
            )
        if rows:
            html = html.replace("</tbody></table>", "".join(rows) + "</tbody></table>")
    return HTML(string=html, base_url=str(TEMPLATE_DIR)).write_pdf()
