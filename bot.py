"""Owner-gated Telegram client for Scribed."""
import os
import logging
import httpx
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.ext import (Application, CommandHandler, ContextTypes, ConversationHandler,
                          CallbackQueryHandler, MessageHandler, filters)

load_dotenv()
logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
API_URL = os.getenv("SCRIBED_API_URL", "http://localhost:8000").rstrip("/")
OWNER_ID = int(os.environ["TELEGRAM_OWNER_ID"])

def owner_only(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id == OWNER_ID)

INVOICE_FIELDS = ["client_name", "project_name", "description", "amount"]
CONTRACT_FIELDS = ["client_name", "project_name", "scope", "deliverables", "timeline", "amount", "payment_schedule"]
FIELD_PROMPTS = {"client_name": "What is the client name?", "project_name": "What is the project name?", "description": "Describe the invoice item.", "scope": "Describe the scope of work.", "deliverables": "List the deliverables, separated by commas.", "timeline": "What is the timeline?", "amount": "What is the total amount in KES?", "payment_schedule": "What are the payment terms?"}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not owner_only(update): return
    keyboard = [[InlineKeyboardButton("Create contract", callback_data="new_contract"), InlineKeyboardButton("Create invoice", callback_data="new_invoice")],
                [InlineKeyboardButton("List contracts", callback_data="list_contracts"), InlineKeyboardButton("List invoices", callback_data="list_invoices")],
                [InlineKeyboardButton("Delete document", callback_data="delete_menu")]]
    await update.message.reply_text("Welcome to Scribed 👋\n\nWhat would you like to do?", reply_markup=InlineKeyboardMarkup(keyboard))

async def begin_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not owner_only(update): return ConversationHandler.END
    query = update.callback_query
    if query: await query.answer()
    kind = query.data.replace("new_", "") if query else update.message.text.split()[0].lstrip("/").replace("new", "", 1)
    context.user_data.clear(); context.user_data["kind"] = kind; context.user_data["fields"] = INVOICE_FIELDS if kind == "invoice" else CONTRACT_FIELDS; context.user_data["index"] = 0
    target = query.message if query else update.message
    await target.reply_text(f"Creating a {kind}. Type /cancel at any time.\n\n{FIELD_PROMPTS[context.user_data['fields'][0]]}")
    return 0

async def collect_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    fields = context.user_data["fields"]; index = context.user_data["index"]
    context.user_data[fields[index]] = update.message.text.strip(); index += 1
    if index < len(fields):
        context.user_data["index"] = index; await update.message.reply_text(FIELD_PROMPTS[fields[index]]); return 0
    summary = "\n".join(f"{field.replace('_', ' ').title()}: {context.user_data[field]}" for field in fields)
    await update.message.reply_text(f"Please confirm:\n\n{summary}\n\nReply `confirm` to create it or `cancel` to stop.")
    return 1

async def confirm_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.text.lower() != "confirm":
        await update.message.reply_text("Cancelled. Use /start to begin again."); return ConversationHandler.END
    data = {field: context.user_data[field] for field in context.user_data["fields"]}
    if context.user_data["kind"] == "invoice":
        data["description"] = data["description"]
        endpoint, filename = "/invoices", "invoice.pdf"
    else:
        data["deliverables"] = [x.strip() for x in data["deliverables"].split(",") if x.strip()]
        endpoint, filename = "/contracts", "contract.pdf"
    async with httpx.AsyncClient(base_url=API_URL) as api: response = await api.post(endpoint, json=data)
    if response.is_success: await update.message.reply_document(InputFile(response.content, filename=filename))
    else: await update.message.reply_text(f"Could not create document ({response.status_code}): {response.text[:300]}")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear(); await update.message.reply_text("Cancelled. Use /start when you are ready."); return ConversationHandler.END

async def delete_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not owner_only(update): return
    await update.callback_query.answer()
    await update.callback_query.message.reply_text("What would you like to delete?", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("Contract", callback_data="delete_contracts")],
        [InlineKeyboardButton("Gatekeeper project", callback_data="delete_projects")],
    ]))

async def delete_contract_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not owner_only(update): return
    await update.callback_query.answer()
    async with httpx.AsyncClient(base_url=API_URL) as api: response = await api.get("/contracts")
    items = response.json() if response.is_success else []
    keyboard = [[InlineKeyboardButton(f"{x['number']} — {x['client_name']}", callback_data=f"delete_contract:{x['id']}")] for x in items]
    await update.callback_query.message.reply_text("Select a contract:", reply_markup=InlineKeyboardMarkup(keyboard or [[InlineKeyboardButton("No contracts found", callback_data="noop")]]))

async def delete_project_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not owner_only(update): return
    await update.callback_query.answer()
    async with httpx.AsyncClient() as api:
        login = await api.post(os.environ["GATEKEEPER_BASE_URL"].rstrip("/") + "/api/auth/login", json={"email": os.environ["GATEKEEPER_EMAIL"], "password": os.environ["GATEKEEPER_PASSWORD"]})
        token = login.json()["token"]
        response = await api.get(os.environ["GATEKEEPER_BASE_URL"].rstrip("/") + "/api/admin/projects", headers={"Authorization": f"Bearer {token}"})
    items = response.json() if response.is_success else []
    keyboard = [[InlineKeyboardButton(f"{x['slug']} — {x['name']}", callback_data=f"delete_project:{x['slug']}")] for x in items]
    await update.callback_query.message.reply_text("Select a Gatekeeper project to archive:", reply_markup=InlineKeyboardMarkup(keyboard or [[InlineKeyboardButton("No projects found", callback_data="noop")]]))

async def confirm_delete_project(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not owner_only(update): return
    await update.callback_query.answer(); slug = update.callback_query.data.split(":", 1)[1]
    context.user_data["delete_project_slug"] = slug
    await update.callback_query.message.reply_text(f"Archive Gatekeeper project `{slug}`? Reply `confirm` or `cancel`.", parse_mode="Markdown")

async def perform_delete_project(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not owner_only(update) or "delete_project_slug" not in context.user_data: return
    slug = context.user_data.pop("delete_project_slug")
    if update.message.text.lower() != "confirm": await update.message.reply_text("Archiving cancelled."); return
    async with httpx.AsyncClient() as api:
        base = os.environ["GATEKEEPER_BASE_URL"].rstrip("/")
        login = await api.post(base + "/api/auth/login", json={"email": os.environ["GATEKEEPER_EMAIL"], "password": os.environ["GATEKEEPER_PASSWORD"]})
        token = login.json()["token"]
        response = await api.delete(f"{base}/api/admin/projects/{slug}", headers={"Authorization": f"Bearer {token}"})
    await update.message.reply_text("Project archived." if response.is_success else f"Could not archive: {response.text}")

async def confirm_delete_contract(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not owner_only(update): return
    await update.callback_query.answer()
    document_id = update.callback_query.data.split(":", 1)[1]
    await update.callback_query.message.reply_text("Delete this contract permanently? Reply `confirm` or `cancel`.")
    context.user_data["delete_contract_id"] = document_id

async def perform_delete_contract(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not owner_only(update) or "delete_contract_id" not in context.user_data: return
    if update.message.text.lower() != "confirm":
        context.user_data.pop("delete_contract_id", None); await update.message.reply_text("Deletion cancelled."); return
    async with httpx.AsyncClient(base_url=API_URL) as api: response = await api.delete(f"/contracts/{context.user_data.pop('delete_contract_id')}")
    await update.message.reply_text(response.text if response.is_success else f"Could not delete: {response.text}")

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

async def list_documents(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not owner_only(update): return
    query = update.callback_query
    if query: await query.answer()
    source = query.data if query else update.message.text.split()[0].lstrip("/")
    kind = "contracts" if "contracts" in source else "invoices"
    async with httpx.AsyncClient(base_url=API_URL) as api: response = await api.get(f"/{kind}")
    target = query.message if query else update.message
    if not response.is_success:
        await target.reply_text(response.text); return
    documents = response.json()
    if not documents:
        await target.reply_text(f"No {kind} found."); return
    await target.reply_text("\n".join(f"{x['number']} — {x['client_name']} — {x['status']}" for x in documents))

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
    conversation = ConversationHandler(
        entry_points=[CommandHandler("newinvoice", begin_conversation), CommandHandler("newcontract", begin_conversation),
                      CallbackQueryHandler(begin_conversation, pattern="^new_(invoice|contract)$")],
        states={0: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_field)],
                1: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_conversation)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(delete_menu, pattern="^delete_menu$"))
    app.add_handler(CallbackQueryHandler(delete_contract_menu, pattern="^delete_contracts$"))
    app.add_handler(CallbackQueryHandler(delete_project_menu, pattern="^delete_projects$"))
    app.add_handler(CallbackQueryHandler(confirm_delete_contract, pattern="^delete_contract:"))
    app.add_handler(CallbackQueryHandler(confirm_delete_project, pattern="^delete_project:"))
    app.add_handler(MessageHandler(filters.Regex("(?i)^(confirm|cancel)$"), perform_delete_contract))
    app.add_handler(MessageHandler(filters.Regex("(?i)^(confirm|cancel)$"), perform_delete_project))
    app.add_handler(conversation)
    app.add_handler(CommandHandler("newinvoice", new_invoice))
    app.add_handler(CommandHandler("newcontract", new_contract))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("contracts", list_documents))
    app.add_handler(CommandHandler("invoices", list_documents))
    app.add_handler(CallbackQueryHandler(list_documents, pattern="^list_(contracts|invoices)$"))
    app.add_handler(CommandHandler("send", transition))
    app.add_handler(CommandHandler("accepted", transition))
    app.add_handler(CommandHandler("paid", transition))
    return app

if __name__ == "__main__":
    logger.info("Starting Scribed Telegram bot (API: %s)", API_URL)
    logger.info("Bot will keep this terminal open while waiting for Telegram messages")
    build_application().run_polling(drop_pending_updates=True)
