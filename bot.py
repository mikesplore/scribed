"""Owner-gated Telegram client for Scribed."""
import os
import httpx
from dotenv import load_dotenv
from telegram import InputFile, Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()
API_URL = os.getenv("SCRIBED_API_URL", "http://localhost:8000").rstrip("/")
OWNER_ID = int(os.environ["TELEGRAM_OWNER_ID"])

def owner_only(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id == OWNER_ID)

async def new_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not owner_only(update): return
    values = " ".join(context.args).split("|")
    if len(values) != 4:
        await update.message.reply_text("Usage: /newinvoice Client|Project|Description|Amount"); return
    client, project, description, amount = (value.strip() for value in values)
    async with httpx.AsyncClient(base_url=API_URL) as api:
        response = await api.post("/invoices", json={"client_name": client, "project_name": project, "description": description, "amount": amount})
    if response.is_success:
        await update.message.reply_document(InputFile(response.content, filename="invoice.pdf"))
    else: await update.message.reply_text(f"API error ({response.status_code}): {response.text[:300]}")

async def new_contract(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not owner_only(update): return
    values = " ".join(context.args).split("|")
    if len(values) != 7:
        await update.message.reply_text("Usage: /newcontract Client|Project|Scope|Deliverables, comma separated|Timeline|Amount|Payment schedule"); return
    client, project, scope, deliverables, timeline, amount, schedule = (value.strip() for value in values)
    payload = {"client_name": client, "project_name": project, "scope": scope, "deliverables": [x.strip() for x in deliverables.split(",") if x.strip()], "timeline": timeline, "amount": amount, "payment_schedule": schedule}
    async with httpx.AsyncClient(base_url=API_URL) as api: response = await api.post("/contracts", json=payload)
    if response.is_success: await update.message.reply_document(InputFile(response.content, filename="contract.pdf"))
    else: await update.message.reply_text(f"API error ({response.status_code}): {response.text[:300]}")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not owner_only(update): return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /status MK-CON-0001"); return
    async with httpx.AsyncClient(base_url=API_URL) as api: response = await api.get(f"/documents/{context.args[0]}")
    await update.message.reply_text(response.text if response.is_success else f"{response.status_code}: {response.text}")

async def transition(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not owner_only(update) or len(context.args) != 1:
        await update.message.reply_text("Usage: /send, /accepted, or /paid NUMBER"); return
    number = context.args[0]
    async with httpx.AsyncClient(base_url=API_URL) as api:
        info = await api.get(f"/documents/{number}")
        if not info.is_success:
            await update.message.reply_text(info.text); return
        data = info.json()
        action = update.message.text.split()[0].lstrip("/")
        paths = {"send": f"/{data['type']}s/{data['id']}/send", "accepted": f"/contracts/{data['id']}/mark-accepted", "paid": f"/invoices/{data['id']}/mark-paid"}
        response = await api.post(paths[action])
    await update.message.reply_text(response.text if response.is_success else f"{response.status_code}: {response.text}")

def build_application() -> Application:
    app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    app.add_handler(CommandHandler("newinvoice", new_invoice))
    app.add_handler(CommandHandler("newcontract", new_contract))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("send", transition))
    app.add_handler(CommandHandler("accepted", transition))
    app.add_handler(CommandHandler("paid", transition))
    return app

if __name__ == "__main__": build_application().run_polling()
