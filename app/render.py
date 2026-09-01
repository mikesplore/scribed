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
    html = template.render(**fields)
    return HTML(string=html, base_url=str(BASE_DIR)).write_pdf()
