import os
import base64
import httpx
from .storage import read_pdf


def send_pdf(recipient: str, subject: str, filename: str, pdf_path: str) -> None:
    api_key = os.environ["RESEND_API_KEY"]
    response = httpx.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "from": os.environ["RESEND_FROM_EMAIL"],
            "to": [recipient],
            "subject": subject,
            "html": "<p>Please find the attached document from mikesplore.</p>",
            "attachments": [{"filename": filename, "content": base64.b64encode(read_pdf(pdf_path)).decode("ascii")}],
        },
        timeout=20,
    )
    response.raise_for_status()
