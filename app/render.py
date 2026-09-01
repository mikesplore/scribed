from pathlib import Path
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
    html = template.render(**render_fields)
    html = html.replace("mikesplore.me/verify/", "scribed.mikesplore.me/verify/")
    return HTML(string=html, base_url=str(TEMPLATE_DIR)).write_pdf()
