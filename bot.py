"""Owner-gated Telegram client for Scribed."""
import os
import logging
import httpx
from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.ext import (Application, CommandHandler, ContextTypes, ConversationHandler,
                          CallbackQueryHandler, MessageHandler, filters)

load_dotenv()
logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
API_URL = os.getenv("SCRIBED_API_URL", "http://localhost:8000").rstrip("/")
OWNER_ID = int(os.environ["TELEGRAM_OWNER_ID"])
API_HEADERS = {"Authorization": f"Bearer {os.environ['SCRIBED_API_TOKEN']}"}

def api_error(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail", "The API returned an error.")
    except (ValueError, TypeError):
        detail = "The API returned an unexpected error."
    logger.error("Scribed API error: %s %s response=%s", response.request.method, response.request.url, response.status_code)
    return str(detail)[:300]

async def reply_in_chunks(target, text: str) -> None:
    for start in range(0, len(text), 3900):
        await target.reply_text(text[start:start + 3900])

def owner_only(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id == OWNER_ID)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not owner_only(update): return
    await update.message.reply_text(
        "Scribed commands:\n\n"
        "/newcontract — create a contract\n"
        "/newinvoice — create an invoice\n"
        "/contracts — list active contracts\n"
        "/invoices — list active invoices\n"
        "/status NUMBER — check document status\n"
        "/send NUMBER — send a document by email\n"
        "/accepted NUMBER — mark a contract accepted\n"
        "/paid NUMBER — mark an invoice paid\n\n"
        "Gatekeeper project selection and archiving are under /start → Delete document.\n\n"
        "/cancel — cancel a workflow\n"
        "/help — show this help"
    )

INVOICE_FIELDS = ["client_name", "project_name", "description", "amount"]
CONTRACT_FIELDS = ["client_name", "project_name", "scope", "deliverables", "timeline", "amount", "payment_schedule"]
FIELD_PROMPTS = {"client_name": "What is the client name?", "project_name": "What is the project name?", "description": "Describe the invoice item.", "scope": "Describe the scope of work.", "deliverables": "List the deliverables, separated by commas.", "timeline": "What is the timeline?", "amount": "What is the total amount in KES?", "payment_schedule": "What are the payment terms?"}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not owner_only(update): return
    keyboard = [[InlineKeyboardButton("Create contract", callback_data="new_contract"), InlineKeyboardButton("Create invoice", callback_data="new_invoice")],
                [InlineKeyboardButton("List contracts", callback_data="list_contracts"), InlineKeyboardButton("List invoices", callback_data="list_invoices")],
                [InlineKeyboardButton("Gatekeeper projects", callback_data="projects")],
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
    async with httpx.AsyncClient(base_url=API_URL, headers=API_HEADERS) as api: response = await api.post(endpoint, json=data)
    if response.is_success: await update.message.reply_document(InputFile(response.content, filename=filename))
    else: await update.message.reply_text(f"Could not create document: {api_error(response)}")
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
    async with httpx.AsyncClient(base_url=API_URL, headers=API_HEADERS) as api: response = await api.get("/contracts")
    if not response.is_success:
        await update.callback_query.message.reply_text(f"Could not load contracts: {api_error(response)}")
        return
    items = response.json()
    keyboard = [[InlineKeyboardButton(f"{x['number']} — {x['client_name']}", callback_data=f"delete_contract:{x['id']}")] for x in items]
    await update.callback_query.message.reply_text("Select a contract:", reply_markup=InlineKeyboardMarkup(keyboard or [[InlineKeyboardButton("No contracts found", callback_data="noop")]]))

async def delete_project_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not owner_only(update): return
    query = update.callback_query
    if query:
        await query.answer()
    target = query.message if query else update.message
    missing = [key for key in ("GATEKEEPER_BASE_URL", "GATEKEEPER_EMAIL", "GATEKEEPER_PASSWORD") if not os.getenv(key)]
    if missing:
        await target.reply_text(f"Gatekeeper is not configured. Missing: {', '.join(missing)}")
        return
    async with httpx.AsyncClient() as api:
        login = await api.post(os.environ["GATEKEEPER_BASE_URL"].rstrip("/") + "/api/auth/login", json={"email": os.environ["GATEKEEPER_EMAIL"], "password": os.environ["GATEKEEPER_PASSWORD"]})
        if not login.is_success:
            await target.reply_text(f"Gatekeeper login failed: {api_error(login)}")
            return
        token = login.json()["token"]
        response = await api.get(os.environ["GATEKEEPER_BASE_URL"].rstrip("/") + "/api/admin/projects", headers={"Authorization": f"Bearer {token}"})
    if not response.is_success:
        await target.reply_text(f"Could not load Gatekeeper projects: {api_error(response)}")
        return
    items = response.json()
    keyboard = [[InlineKeyboardButton(f"View {x['slug']}", callback_data=f"project:{x['slug']}"), InlineKeyboardButton("Archive", callback_data=f"delete_project:{x['slug']}")] for x in items]
    await target.reply_text("Select a Gatekeeper project to archive:", reply_markup=InlineKeyboardMarkup(keyboard or [[InlineKeyboardButton("No projects found", callback_data="noop")]]))

async def project_details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not owner_only(update): return
    query = update.callback_query
    if query: await query.answer()
    slug = query.data.split(":", 1)[1] if query else (context.args[0] if context.args else "")
    if not slug:
        await (query.message if query else update.message).reply_text("Usage: /project PROJECT_SLUG")
        return
    missing = [key for key in ("GATEKEEPER_BASE_URL", "GATEKEEPER_EMAIL", "GATEKEEPER_PASSWORD") if not os.getenv(key)]
    target = query.message if query else update.message
    if missing:
        await target.reply_text(f"Gatekeeper is not configured. Missing: {', '.join(missing)}")
        return
    async with httpx.AsyncClient() as api:
        base = os.environ["GATEKEEPER_BASE_URL"].rstrip("/")
        login = await api.post(base + "/api/auth/login", json={"email": os.environ["GATEKEEPER_EMAIL"], "password": os.environ["GATEKEEPER_PASSWORD"]})
        if not login.is_success:
            await target.reply_text(f"Gatekeeper login failed: {api_error(login)}"); return
        token = login.json().get("token")
        response = await api.get(f"{base}/api/admin/projects/{slug}", headers={"Authorization": f"Bearer {token}"})
    if not response.is_success:
        await target.reply_text(f"Could not load project: {api_error(response)}"); return
    body = response.json(); project = body.get("project", body)
    payments = body.get("payments", [])
    text = (f"Project: {project.get('name', slug)}\nSlug: {project.get('slug', slug)}\n"
            f"Status: {project.get('status', 'unknown')}\nDomain: {project.get('domain', '—')}\n"
            f"Client: {project.get('clientName', '—')}\nAmount due: {project.get('amountDue', '—')} {project.get('currency', '')}\n"
            f"Due date: {project.get('dueDate', '—')}\n\nPayments: {len(payments)}")
    if payments:
        text += "\n" + "\n".join(f"• {p.get('status', 'unknown')} — {p.get('amount', '—')} {project.get('currency', '')} — {p.get('paidAt', p.get('createdAt', '—'))}" for p in payments[:20])
    keyboard = []
    await target.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def project_payments(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not owner_only(update): return
    query = update.callback_query; await query.answer()
    slug = query.data.split(":", 1)[1]
    context.args = [slug]
    # The project endpoint includes the authoritative payment history.
    await project_details(update, context)

async def confirm_delete_project(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not owner_only(update): return
    await update.callback_query.answer(); slug = update.callback_query.data.split(":", 1)[1]
    context.user_data["delete_project_slug"] = slug
    await update.callback_query.message.reply_text(f"Archive Gatekeeper project `{slug}`? Reply `confirm` or `cancel`.", parse_mode="Markdown")

async def perform_delete_project(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not owner_only(update) or "delete_project_slug" not in context.user_data: return
    slug = context.user_data.pop("delete_project_slug")
    if update.message.text.lower() != "confirm": await update.message.reply_text("Archiving cancelled."); return
    missing = [key for key in ("GATEKEEPER_BASE_URL", "GATEKEEPER_EMAIL", "GATEKEEPER_PASSWORD") if not os.getenv(key)]
    if missing:
        await update.message.reply_text(f"Gatekeeper is not configured. Missing: {', '.join(missing)}")
        return
    async with httpx.AsyncClient() as api:
        base = os.environ["GATEKEEPER_BASE_URL"].rstrip("/")
        login = await api.post(base + "/api/auth/login", json={"email": os.environ["GATEKEEPER_EMAIL"], "password": os.environ["GATEKEEPER_PASSWORD"]})
        if not login.is_success:
            await update.message.reply_text(f"Gatekeeper login failed: {api_error(login)}")
            return
        token = login.json()["token"]
        response = await api.delete(f"{base}/api/admin/projects/{slug}", headers={"Authorization": f"Bearer {token}"})
    await update.message.reply_text("Project archived." if response.is_success else f"Could not archive project: {api_error(response)}")

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
    async with httpx.AsyncClient(base_url=API_URL, headers=API_HEADERS) as api: response = await api.delete(f"/contracts/{context.user_data.pop('delete_contract_id')}")
    await update.message.reply_text("Contract deleted." if response.is_success else f"Could not delete contract: {api_error(response)}")

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
    else: await update.message.reply_text(f"Could not create invoice: {api_error(response)}")

async def new_contract(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not owner_only(update): return
    values = " ".join(context.args).split("|")
    if len(values) != 7:
        await update.message.reply_text("Usage: /newcontract Client|Project|Scope|Deliverables, comma separated|Timeline|Amount|Payment schedule"); return
    client, project, scope, deliverables, timeline, amount, schedule = (value.strip() for value in values)
    payload = {"client_name": client, "project_name": project, "scope": scope, "deliverables": [x.strip() for x in deliverables.split(",") if x.strip()], "timeline": timeline, "amount": amount, "payment_schedule": schedule}
    async with httpx.AsyncClient(base_url=API_URL, headers=API_HEADERS) as api: response = await api.post("/contracts", json=payload)
    if response.is_success: await update.message.reply_document(InputFile(response.content, filename="contract.pdf"))
    else: await update.message.reply_text(f"Could not create contract: {api_error(response)}")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not owner_only(update): return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /status MK-CON-0001"); return
    async with httpx.AsyncClient(base_url=API_URL, headers=API_HEADERS) as api: response = await api.get(f"/documents/{context.args[0]}")
    await update.message.reply_text(response.text[:3900] if response.is_success else f"Could not update document: {api_error(response)}")

async def list_documents(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not owner_only(update): return
    query = update.callback_query
    if query: await query.answer()
    source = query.data if query else update.message.text.split()[0].lstrip("/")
    kind = "contracts" if "contracts" in source else "invoices"
    async with httpx.AsyncClient(base_url=API_URL, headers=API_HEADERS) as api: response = await api.get(f"/{kind}")
    target = query.message if query else update.message
    if not response.is_success:
        await target.reply_text(f"Could not load {kind}: {api_error(response)}"); return
    documents = response.json()
    if not documents:
        await target.reply_text(f"No {kind} found."); return
    lines = [f"{x['number']} — {x['client_name']} — {x['status']}" for x in documents]
    await reply_in_chunks(target, f"{kind.title()}:\n" + "\n".join(lines))

async def transition(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not owner_only(update) or len(context.args) != 1:
        await update.message.reply_text("Usage: /send, /accepted, or /paid NUMBER"); return
    number = context.args[0]
    async with httpx.AsyncClient(base_url=API_URL, headers=API_HEADERS) as api:
        info = await api.get(f"/documents/{number}")
        if not info.is_success:
            await update.message.reply_text(f"Could not find document: {api_error(info)}"); return
        data = info.json()
        action = update.message.text.split()[0].lstrip("/")
        paths = {"send": f"/{data['type']}s/{data['id']}/send", "accepted": f"/contracts/{data['id']}/mark-accepted", "paid": f"/invoices/{data['id']}/mark-paid"}
        response = await api.post(paths[action])
    await update.message.reply_text(response.text[:3900] if response.is_success else f"Could not update document: {api_error(response)}")

def build_application() -> Application:
    app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).post_init(set_command_menu).build()
    conversation = ConversationHandler(
        entry_points=[CommandHandler("newinvoice", begin_conversation), CommandHandler("newcontract", begin_conversation),
                      CallbackQueryHandler(begin_conversation, pattern="^new_(invoice|contract)$")],
        states={0: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_field)],
                1: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_conversation)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(delete_menu, pattern="^delete_menu$"))
    app.add_handler(CallbackQueryHandler(delete_contract_menu, pattern="^delete_contracts$"))
    app.add_handler(CallbackQueryHandler(delete_project_menu, pattern="^delete_projects$"))
    app.add_handler(CallbackQueryHandler(project_details, pattern="^projects:|^project:"))
    app.add_handler(CallbackQueryHandler(project_payments, pattern="^payments:"))
    app.add_handler(CallbackQueryHandler(delete_project_menu, pattern="^projects$"))
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
    app.add_handler(CommandHandler("project", project_details))
    app.add_handler(CommandHandler("projects", delete_project_menu))
    app.add_handler(CallbackQueryHandler(list_documents, pattern="^list_(contracts|invoices)$"))
    app.add_handler(CommandHandler("send", transition))
    app.add_handler(CommandHandler("accepted", transition))
    app.add_handler(CommandHandler("paid", transition))
    app.add_error_handler(error_handler)
    return app

async def set_command_menu(application: Application) -> None:
    await application.bot.set_my_short_description(
        "Create, send, and track mikesplore contracts and invoices."
    )
    await application.bot.set_my_description(
        "Your mikesplore document assistant. Create branded contracts and invoices, "
        "send documents by email, track acceptance and payment status, and manage "
        "linked Gatekeeper projects. Use /help to see all available commands."
    )
    await application.bot.set_my_commands([
        BotCommand("start", "Open the main menu"),
        BotCommand("help", "Show all capabilities"),
        BotCommand("newcontract", "Create a contract"),
        BotCommand("newinvoice", "Create an invoice"),
        BotCommand("contracts", "List contracts"),
        BotCommand("invoices", "List invoices"),
        BotCommand("projects", "List Gatekeeper projects"),
        BotCommand("project", "View project details and payments"),
        BotCommand("status", "Check document status"),
        BotCommand("send", "Send a document"),
        BotCommand("accepted", "Mark a contract accepted"),
        BotCommand("paid", "Mark an invoice paid"),
        BotCommand("cancel", "Cancel current workflow"),
    ])

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled bot error", exc_info=context.error)
    message = getattr(update, "effective_message", None)
    if message:
        if isinstance(context.error, httpx.RequestError):
            text = "I couldn't reach the Scribed service. Please try again later."
        elif isinstance(context.error, KeyError):
            text = f"Bot configuration is incomplete: missing {context.error.args[0]}."
        else:
            text = "Something went wrong while processing that request. Please try again later."
        await message.reply_text(text)

if __name__ == "__main__":
    logger.info("Starting Scribed Telegram bot (API: %s)", API_URL)
    logger.info("Bot will keep this terminal open while waiting for Telegram messages")
    build_application().run_polling(drop_pending_updates=True)
