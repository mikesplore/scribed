import os
import smtplib
from email.message import EmailMessage
from pathlib import Path


def send_pdf(recipient: str, subject: str, filename: str, pdf_path: str) -> None:
    message = EmailMessage()
    message["From"] = os.environ["SMTP_FROM"]
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content("Please find the attached document from mikesplore.")
    message.add_attachment(Path(pdf_path).read_bytes(), maintype="application", subtype="pdf", filename=filename)
    host, port = os.environ.get("SMTP_HOST", "localhost"), int(os.environ.get("SMTP_PORT", "25"))
    with smtplib.SMTP(host, port) as smtp:
        if os.getenv("SMTP_STARTTLS", "false").lower() == "true": smtp.starttls()
        if os.getenv("SMTP_USERNAME"): smtp.login(os.environ["SMTP_USERNAME"], os.environ["SMTP_PASSWORD"])
        smtp.send_message(message)
