"""Owner-gated Telegram client for Scribed."""
import os
import logging
import re
from urllib.parse import quote
import httpx
from decimal import Decimal, InvalidOperation
from uuid import uuid4
from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.ext import (Application, CommandHandler, ContextTypes, ConversationHandler,
                          CallbackQueryHandler, MessageHandler, filters)

load_dotenv()
class SecretRedactionFilter(logging.Filter):
    """Prevent credentials embedded in URLs or headers from reaching logs."""

    _patterns = (
        (re.compile(r"(/bot)[^/\s]+", re.IGNORECASE), r"\1[REDACTED]"),
        (re.compile(r"(Bearer\s+)[^\s,]+", re.IGNORECASE), r"\1[REDACTED]"),
    )

    def filter(self, record: logging.LogRecord) -> bool:
        rendered = record.getMessage()
        for pattern, replacement in self._patterns:
            rendered = pattern.sub(replacement, rendered)
        record.msg = rendered
        record.args = ()
        return True


logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
for handler in logging.getLogger().handlers:
    handler.addFilter(SecretRedactionFilter())
# These request logs include Telegram's bot token in the URL path. Application
# errors remain logged above, with secrets redacted by the root handler.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)
API_URL = os.getenv("SCRIBED_API_URL", "http://localhost:8000").rstrip("/")
OWNER_ID = int(os.environ["TELEGRAM_OWNER_ID"])
API_HEADERS = {"Authorization": f"Bearer {os.environ['SCRIBED_API_TOKEN']}"}
HTTP_TIMEOUT = httpx.Timeout(20.0)

def valid_amount(value: str) -> bool:
    try: return Decimal(value.strip()) > 0
    except (InvalidOperation, ValueError): return False

def valid_number(value: str) -> bool:
    return bool(__import__("re").fullmatch(r"MK-(?:CON|INV)-\d{4,}", value.strip(), __import__("re").IGNORECASE))

def payment_summary(project: dict, payments: list[dict]) -> tuple[Decimal | None, Decimal | None]:
    """Return paid and outstanding amounts from Gatekeeper's project payload."""
    total_due = project.get("amountDue") or project.get("amount_due")
    try:
        due = Decimal(str(total_due)) if total_due is not None else None
    except (InvalidOperation, ValueError):
        due = None
    paid = Decimal("0")
    for payment in payments:
        if str(payment.get("status", "")).lower() in {"paid", "success", "successful", "completed"}:
            try:
                paid += Decimal(str(payment.get("amount", 0)))
            except (InvalidOperation, ValueError):
                continue
    # Gatekeeper's amountDue is already the remaining balance. Payments are
    # displayed separately and must not be subtracted a second time.
    return paid, due

def api_error(response: httpx.Response) -> str:
    status = f"HTTP {response.status_code}"
    try:
        body = response.json()
        detail = body.get("detail") if isinstance(body, dict) else None
    except (ValueError, TypeError):
        detail = None
    if not detail:
        detail = response.text.strip() or "The API returned an empty error response."
    logger.error("Scribed API error: %s %s response=%s body=%r", response.request.method,
                 response.request.url, response.status_code, response.text[:1000])
    return f"{status}: {str(detail)[:300]}"

def response_filename(response: httpx.Response, fallback: str) -> str:
    match = re.search(r'filename="?([^";]+)', response.headers.get("content-disposition", ""), re.IGNORECASE)
    return match.group(1) if match else fallback

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
        "\nCreation options:\n"
        "• From scratch\n"
        "• From a Gatekeeper project (prefills client, project, balance, and link)\n\n"
        "/contracts — list active contracts\n"
        "/invoices — list active invoices\n"
        "Use the buttons on each document to view status, send, duplicate, or archive.\n"
        "/projects — list Gatekeeper projects\n"
        "/project SLUG — view project payments and balance\n"
        "/client NAME — view client documents and totals\n"
        "/duplicate NUMBER — create a new draft from an existing document\n"
        "/status NUMBER — check document status\n"
        "/send NUMBER — send a document by email\n"
        "/accepted NUMBER — mark a contract accepted\n"
        "/paid NUMBER — mark an invoice paid\n\n"
        "/duplicate NUMBER — create a new draft from a document\n"
        "/client NAME — view a client's document and payment history\n\n"
        "Gatekeeper project selection and archiving are under /start → Delete document.\n\n"
        "/cancel — cancel a workflow\n"
        "/help — show this help"
    )

INVOICE_FIELDS = ["client_name", "client_email", "project_name", "description", "amount"]
CONTRACT_FIELDS = ["client_name", "client_email", "project_name", "scope", "deliverables", "timeline", "amount", "payment_schedule"]
FIELD_PROMPTS = {"client_name": "What is the client name?", "client_email": "What is the client email?", "project_name": "What is the project name?", "description": "Describe the invoice item.", "scope": "Describe the scope of work.", "deliverables": "List the deliverables, separated by commas.", "timeline": "What is the timeline?", "amount": "What is the total amount in KES?", "payment_schedule": "What are the payment terms?"}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not owner_only(update): return
    keyboard = [[InlineKeyboardButton("Create contract", callback_data="choose_contract"), InlineKeyboardButton("Create invoice", callback_data="choose_invoice")],
                [InlineKeyboardButton("List contracts", callback_data="list_contracts"), InlineKeyboardButton("List invoices", callback_data="list_invoices")],
                [InlineKeyboardButton("Gatekeeper projects", callback_data="projects")],
                [InlineKeyboardButton("Delete document", callback_data="delete_menu")]]
    await update.message.reply_text("Welcome to Scribed 👋\n\nWhat would you like to do?", reply_markup=InlineKeyboardMarkup(keyboard))

async def choose_invoice_source(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not owner_only(update): return
    await update.callback_query.answer()
    await update.callback_query.message.reply_text(
        "How would you like to start the invoice?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("From scratch", callback_data="new_invoice_scratch")],
            [InlineKeyboardButton("Use a Gatekeeper project", callback_data="invoice_projects")],
        ]),
    )

async def choose_contract_source(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not owner_only(update): return
    await update.callback_query.answer()
    await update.callback_query.message.reply_text(
        "How would you like to start the contract?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("From scratch", callback_data="new_contract_scratch")],
            [InlineKeyboardButton("Use a Gatekeeper project", callback_data="contract_projects")],
        ]),
    )

async def choose_contract_source(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not owner_only(update): return
    await update.callback_query.answer()
    await update.callback_query.message.reply_text(
        "How would you like to start the contract?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("From scratch", callback_data="new_contract_scratch")],
            [InlineKeyboardButton("Use a Gatekeeper project", callback_data="contract_projects")],
        ]),
    )

async def invoice_project_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not owner_only(update): return
    query = update.callback_query
    await query.answer()
    missing = [key for key in ("GATEKEEPER_BASE_URL", "GATEKEEPER_EMAIL", "GATEKEEPER_PASSWORD") if not os.getenv(key)]
    if missing:
        await query.message.reply_text(f"Gatekeeper is not configured. Missing: {', '.join(missing)}")
        return
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as api:
            base = os.environ["GATEKEEPER_BASE_URL"].rstrip("/")
            login = await api.post(base + "/api/auth/login", json={"email": os.environ["GATEKEEPER_EMAIL"], "password": os.environ["GATEKEEPER_PASSWORD"]})
            if not login.is_success:
                await query.message.reply_text(f"Gatekeeper login failed: {api_error(login)}"); return
            response = await api.get(base + "/api/admin/projects", headers={"Authorization": f"Bearer {login.json()['token']}"})
    except httpx.RequestError:
        await query.message.reply_text("I couldn't reach Gatekeeper. Please try again shortly."); return
    if not response.is_success:
        await query.message.reply_text(f"Could not load Gatekeeper projects: {api_error(response)}"); return
    items = response.json()
    kind = "contract" if query.data == "contract_projects" else "invoice"
    keyboard = [[InlineKeyboardButton(f"{x.get('name', x.get('slug', 'Project'))}", callback_data=f"project_document:{kind}:{x['slug']}")] for x in items]
    await query.message.reply_text(f"Choose a project for the {kind}:", reply_markup=InlineKeyboardMarkup(keyboard or [[InlineKeyboardButton("No projects found", callback_data="noop")]]))

async def begin_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not owner_only(update): return ConversationHandler.END
    query = update.callback_query
    if query: await query.answer()
    kind = query.data.replace("new_", "") if query else update.message.text.split()[0].lstrip("/").replace("new", "", 1)
    context.user_data.clear(); context.user_data["kind"] = kind; context.user_data["fields"] = INVOICE_FIELDS if kind == "invoice" else CONTRACT_FIELDS; context.user_data["index"] = 0
    target = query.message if query else update.message
    await target.reply_text(f"Creating a {kind}. Type /cancel at any time.\n\n{FIELD_PROMPTS[context.user_data['fields'][0]]}")
    return 0

async def begin_invoice_scratch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    context.user_data.update({"kind": "invoice", "fields": INVOICE_FIELDS, "index": 0})
    await query.message.reply_text("Starting a fresh invoice. Type /cancel at any time.\n\nWhat is the client name?")
    return 0

async def begin_contract_scratch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    context.user_data.update({"kind": "contract", "fields": CONTRACT_FIELDS, "index": 0})
    await query.message.reply_text("Starting a fresh contract. Type /cancel at any time.\n\nWhat is the client name?")
    return 0

async def begin_project_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start a document using the project currently shown in the bot."""
    if not owner_only(update): return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    kind = query.data.split(":", 2)[1]
    project = context.user_data.get("gatekeeper_project", {})
    payments = context.user_data.get("gatekeeper_payments", [])
    if not project:
        slug = query.data.split(":", 2)[2]
        base = os.environ.get("GATEKEEPER_BASE_URL", "").rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as api:
                login = await api.post(base + "/api/auth/login", json={"email": os.environ["GATEKEEPER_EMAIL"], "password": os.environ["GATEKEEPER_PASSWORD"]})
                response = await api.get(base + f"/api/admin/projects/{slug}", headers={"Authorization": f"Bearer {login.json()['token']}"})
            if response.is_success:
                body = response.json(); project = body.get("project", body)
                payments = body.get("payments", [])
        except (httpx.RequestError, KeyError, ValueError):
            project = {}
    if not project:
        await query.message.reply_text("That project preview has expired. Open the project again and try once more.")
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data.update({"kind": kind, "fields": INVOICE_FIELDS if kind == "invoice" else CONTRACT_FIELDS, "index": 0})
    paid, balance = payment_summary(project, payments)
    values = {
        "client_name": project.get("clientName") or project.get("client_name"),
        "client_email": project.get("clientEmail") or project.get("client_email") or project.get("email"),
        "project_name": project.get("name") or project.get("projectName") or project.get("project_name"),
        "amount": str(balance) if balance is not None and balance > 0 else None,
        "currency": project.get("currency"),
    }
    if kind == "invoice" and balance is not None:
        values["amount_paid"] = str(paid) if paid else None
        values["original_amount"] = str(balance + paid) if paid else str(balance)
    if kind == "contract":
        values["gatekeeper_project_id"] = project.get("id") or project.get("slug")
    context.user_data.update({key: value for key, value in values.items() if value not in (None, "")})
    fields = context.user_data["fields"]
    missing = [field for field in fields if not context.user_data.get(field)]
    context.user_data["all_fields"] = fields
    context.user_data["fields"] = missing
    context.user_data["index"] = 0
    if not missing:
        await query.message.reply_text(
            "I filled this from Gatekeeper. Ready to create it?",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Create", callback_data="create_confirm"),
                                                 InlineKeyboardButton("Edit", callback_data="create_edit"),
                                                 InlineKeyboardButton("Cancel", callback_data="create_cancel")]]),
        )
        return 1
    await query.message.reply_text(
        f"Starting a {kind} for {values.get('client_name', 'this project')}. "
        f"I filled in what Gatekeeper knows.\n\n{FIELD_PROMPTS[missing[0]]}"
    )
    return 0

async def collect_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    fields = context.user_data["fields"]; index = context.user_data["index"]
    if fields[index] == "amount" and not valid_amount(update.message.text):
        await update.message.reply_text("Please enter a positive number, e.g. 25000.")
        return 0
    context.user_data[fields[index]] = update.message.text.strip(); index += 1
    if index < len(fields):
        context.user_data["index"] = index; await update.message.reply_text(FIELD_PROMPTS[fields[index]]); return 0
    summary = "\n".join(f"{field.replace('_', ' ').title()}: {context.user_data[field]}" for field in fields)
    await update.message.reply_text(
        f"Here’s the draft preview:\n\n{summary}\n\nReady to make it official?",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Create", callback_data="create_confirm"),
                                             InlineKeyboardButton("Edit", callback_data="create_edit"),
                                             InlineKeyboardButton("Cancel", callback_data="create_cancel")]]),
    )
    return 1

async def confirm_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
        action = query.data
        target = query.message
        if action == "create_edit":
            context.user_data["index"] = 0
            await target.reply_text("Which field would you like to revisit? Type /cancel to stop, or start with the client name.")
            return 0
        if action == "create_cancel":
            context.user_data.clear()
            await target.reply_text("Cancelled. Nothing was created.")
            return ConversationHandler.END
    else:
        action = update.message.text.lower()
        target = update.message
    if action != "create_confirm" and action != "confirm":
        await target.reply_text("Cancelled. Nothing was created."); return ConversationHandler.END
    all_fields = context.user_data.get("all_fields", context.user_data["fields"])
    data = {field: context.user_data[field] for field in all_fields if field in context.user_data}
    for field in ("original_amount", "amount_paid"):
        if field in context.user_data:
            data[field] = context.user_data[field]
    if context.user_data.get("gatekeeper_project_id"):
        data["gatekeeper_project_id"] = context.user_data["gatekeeper_project_id"]
    context.user_data["idempotency_key"] = context.user_data.get("idempotency_key", str(uuid4()))
    if context.user_data["kind"] == "invoice":
        data["description"] = data["description"]
        endpoint, filename = "/invoices", "invoice.pdf"
    else:
        data["deliverables"] = [x.strip() for x in data["deliverables"].split(",") if x.strip()]
        endpoint, filename = "/contracts", "contract.pdf"
    try:
        async with httpx.AsyncClient(base_url=API_URL, headers=API_HEADERS, timeout=HTTP_TIMEOUT) as api: response = await api.post(endpoint, json=data, headers={"Idempotency-Key": context.user_data["idempotency_key"]})
    except httpx.RequestError:
        logger.exception("Scribed API request failed")
        await target.reply_text("I couldn't reach Scribed. Nothing was created—tap Create to retry safely.")
        return 1
    if response.is_success: await target.reply_document(InputFile(response.content, filename=response_filename(response, filename)), caption="Done — your document is ready.")
    else:
        await target.reply_text(f"I hit a snag while creating it: {api_error(response)}\nNothing was created. You can tap Create to retry or /cancel.")
        return 1
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear(); await update.message.reply_text("Cancelled. Use /start when you are ready."); return ConversationHandler.END

async def unsupported_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if owner_only(update) and update.effective_message:
        await update.effective_message.reply_text("Please send text only. Files and other message types are not supported.")

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
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as api:
            login = await api.post(os.environ["GATEKEEPER_BASE_URL"].rstrip("/") + "/api/auth/login", json={"email": os.environ["GATEKEEPER_EMAIL"], "password": os.environ["GATEKEEPER_PASSWORD"]})
            if not login.is_success:
                await target.reply_text(f"Gatekeeper login failed: {api_error(login)}")
                return
            token = login.json()["token"]
            response = await api.get(os.environ["GATEKEEPER_BASE_URL"].rstrip("/") + "/api/admin/projects", headers={"Authorization": f"Bearer {token}"})
    except httpx.RequestError:
        logger.exception("Gatekeeper request failed")
        await target.reply_text("I couldn't reach Gatekeeper. Check its URL, DNS, and network access from the bot container.")
        return
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
    context.user_data["gatekeeper_project"] = project
    context.user_data["gatekeeper_payments"] = payments
    paid, balance = payment_summary(project, payments)
    currency = project.get("currency", "")
    text = (f"Project: {project.get('name', slug)}\nSlug: {project.get('slug', slug)}\n"
            f"Status: {project.get('status', 'unknown')}\nDomain: {project.get('domain', '—')}\n"
            f"Client: {project.get('clientName', '—')}\nAmount due: {project.get('amountDue', '—')} {currency}\n"
            f"Paid: {paid if paid is not None else '—'} {currency}\n"
            f"Balance: {balance if balance is not None else '—'} {currency}\n"
            f"Due date: {project.get('dueDate', '—')}\n\nPayments: {len(payments)}")
    if payments:
        text += "\n" + "\n".join(f"• {p.get('status', 'unknown')} — {p.get('amount', '—')} {project.get('currency', '')} — {p.get('paidAt', p.get('createdAt', '—'))}" for p in payments[:20])
    keyboard = [[InlineKeyboardButton("Create contract", callback_data=f"project_document:contract:{slug}"),
                 InlineKeyboardButton("Create invoice", callback_data=f"project_document:invoice:{slug}")]]
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
    if not valid_amount(amount):
        await update.message.reply_text("Amount must be a positive number."); return
    async with httpx.AsyncClient(base_url=API_URL, headers=API_HEADERS, timeout=HTTP_TIMEOUT) as api:
        response = await api.post("/invoices", json={"client_name": client, "project_name": project, "description": description, "amount": amount}, headers={"Idempotency-Key": str(uuid4())})
    if response.is_success:
        await update.message.reply_document(InputFile(response.content, filename="invoice.pdf"))
    else: await update.message.reply_text(f"Could not create invoice: {api_error(response)}")

async def new_contract(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not owner_only(update): return
    values = " ".join(context.args).split("|")
    if len(values) != 7:
        await update.message.reply_text("Usage: /newcontract Client|Project|Scope|Deliverables, comma separated|Timeline|Amount|Payment schedule"); return
    client, project, scope, deliverables, timeline, amount, schedule = (value.strip() for value in values)
    if not valid_amount(amount):
        await update.message.reply_text("Amount must be a positive number."); return
    payload = {"client_name": client, "project_name": project, "scope": scope, "deliverables": [x.strip() for x in deliverables.split(",") if x.strip()], "timeline": timeline, "amount": amount, "payment_schedule": schedule}
    async with httpx.AsyncClient(base_url=API_URL, headers=API_HEADERS, timeout=HTTP_TIMEOUT) as api: response = await api.post("/contracts", json=payload, headers={"Idempotency-Key": str(uuid4())})
    if response.is_success: await update.message.reply_document(InputFile(response.content, filename="contract.pdf"))
    else: await update.message.reply_text(f"Could not create contract: {api_error(response)}")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not owner_only(update): return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /status MK-CON-0001"); return
    if not valid_number(context.args[0]):
        await update.message.reply_text("Document number must look like MK-CON-0001 or MK-INV-0001."); return
    async with httpx.AsyncClient(base_url=API_URL, headers=API_HEADERS) as api: response = await api.get(f"/documents/{context.args[0]}")
    await update.message.reply_text(response.text[:3900] if response.is_success else f"Could not update document: {api_error(response)}")

async def document_lookup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Recovery commands for finding a document after a Telegram/API drop."""
    if not owner_only(update): return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /document MK-CON-0001"); return
    async with httpx.AsyncClient(base_url=API_URL, headers=API_HEADERS, timeout=HTTP_TIMEOUT) as api:
        response = await api.get(f"/documents/{context.args[0]}")
    await update.message.reply_text(response.text[:3900] if response.is_success else f"Could not find document: {api_error(response)}")

async def duplicate_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    target = update.effective_message
    if not owner_only(update) or len(context.args) != 1 or not valid_number(context.args[0]):
        if target:
            await target.reply_text("Usage: /duplicate MK-CON-0001")
        return
    number = context.args[0]
    async with httpx.AsyncClient(base_url=API_URL, headers=API_HEADERS, timeout=HTTP_TIMEOUT) as api:
        info = await api.get(f"/documents/{number}")
        if not info.is_success:
            await target.reply_text(f"Could not find document: {api_error(info)}"); return
        data = info.json()
        response = await api.post(f"/{data['type']}s/{data['id']}/duplicate")
    if response.is_success:
        await target.reply_document(InputFile(response.content, filename=f"{number}-copy.pdf"))
    else:
        await target.reply_text(f"Could not duplicate document: {api_error(response)}")

async def client_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    target = update.effective_message
    if not owner_only(update) or not context.args:
        if target:
            await target.reply_text("Usage: /client CLIENT NAME")
        return
    name = " ".join(context.args)
    async with httpx.AsyncClient(base_url=API_URL, headers=API_HEADERS, timeout=HTTP_TIMEOUT) as api:
        response = await api.get(f"/clients/{quote(name, safe='')}")
    if not response.is_success:
        await target.reply_text(f"Could not load client history: {api_error(response)}"); return
    body = response.json(); totals = body["totals"]
    lines = [f"Client: {body['client_name']}", f"Billed: {totals['billed']}", f"Paid: {totals['paid']}", f"Balance: {totals['balance']}", ""]
    lines += [f"{x['number']} — {x['project_name']} — {x['status']}" for x in body["contracts"]]
    lines += [f"{x['number']} — {x['project_name']} — {x['amount']} {x['currency']} — {x['status']}" for x in body["invoices"]]
    await target.reply_text("\n".join(lines)[:3900])

async def my_documents(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not owner_only(update): return
    await list_documents(update, context)

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
    for item in documents[:30]:
        label = f"{item['number']} — {item['client_name']} — {item['status']}"
        actions = [InlineKeyboardButton("Status", callback_data=f"doc_action:status:{kind}:{item['id']}:{item['number']}"),
                   InlineKeyboardButton("Duplicate", callback_data=f"doc_action:duplicate:{kind}:{item['id']}")]
        if item["status"] == "draft":
            actions.insert(1, InlineKeyboardButton("Send", callback_data=f"doc_action:send:{kind}:{item['id']}"))
        if kind == "contracts":
            actions.append(InlineKeyboardButton("Archive", callback_data=f"doc_action:archive:{kind}:{item['id']}"))
        await target.reply_text(label, reply_markup=InlineKeyboardMarkup([actions]))

async def document_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not owner_only(update): return
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":", 4)
    _, action, kind, document_id = parts[:4]
    number = parts[4] if len(parts) == 5 else None
    async with httpx.AsyncClient(base_url=API_URL, headers=API_HEADERS, timeout=HTTP_TIMEOUT) as api:
        if action == "status":
            response = await api.get(f"/documents/{number}")
        elif action == "archive":
            response = await api.delete(f"/{kind}/{document_id}")
        else:
            response = await api.post(f"/{kind}/{document_id}/{action}")
    if not response.is_success:
        await query.message.reply_text(f"Could not {action} document: {api_error(response)}")
        return
    if action in {"duplicate", "send"} and response.headers.get("content-type", "").startswith("application/pdf"):
        await query.message.reply_document(InputFile(response.content, filename=f"{action}.pdf"), caption="Done.")
    else:
        await query.message.reply_text("Done — " + response.text[:500])

async def transition(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not owner_only(update) or len(context.args) != 1:
        await update.message.reply_text("Usage: /send, /accepted, or /paid NUMBER"); return
    number = context.args[0]
    if not valid_number(number):
        await update.message.reply_text("Document number must look like MK-CON-0001 or MK-INV-0001."); return
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
                      CallbackQueryHandler(begin_conversation, pattern="^new_(invoice|contract)$"),
                      CallbackQueryHandler(begin_invoice_scratch, pattern="^new_invoice_scratch$"),
                      CallbackQueryHandler(begin_contract_scratch, pattern="^new_contract_scratch$"),
                      CallbackQueryHandler(begin_project_document, pattern="^project_document:(invoice|contract):")],
        states={0: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_field)],
                1: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_conversation),
                    CallbackQueryHandler(confirm_conversation, pattern="^create_(confirm|edit|cancel)$")]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(delete_menu, pattern="^delete_menu$"))
    app.add_handler(CallbackQueryHandler(choose_invoice_source, pattern="^choose_invoice$"))
    app.add_handler(CallbackQueryHandler(choose_contract_source, pattern="^choose_contract$"))
    app.add_handler(CallbackQueryHandler(invoice_project_menu, pattern="^(invoice|contract)_projects$"))
    app.add_handler(CallbackQueryHandler(delete_contract_menu, pattern="^delete_contracts$"))
    app.add_handler(CallbackQueryHandler(delete_project_menu, pattern="^delete_projects$"))
    app.add_handler(CallbackQueryHandler(project_details, pattern="^projects:|^project:"))
    app.add_handler(CallbackQueryHandler(project_payments, pattern="^payments:"))
    app.add_handler(CallbackQueryHandler(delete_project_menu, pattern="^projects$"))
    app.add_handler(CallbackQueryHandler(confirm_delete_contract, pattern="^delete_contract:"))
    app.add_handler(CallbackQueryHandler(confirm_delete_project, pattern="^delete_project:"))
    # Conversations must receive confirm/cancel before the document-deletion
    # handlers below; otherwise those handlers swallow invoice confirmations.
    app.add_handler(conversation)
    app.add_handler(MessageHandler(filters.Regex("(?i)^(confirm|cancel)$"), perform_delete_contract))
    app.add_handler(MessageHandler(filters.Regex("(?i)^(confirm|cancel)$"), perform_delete_project))
    app.add_handler(MessageHandler(~filters.TEXT, unsupported_input))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("document", document_lookup))
    app.add_handler(CommandHandler("duplicate", duplicate_document))
    app.add_handler(CommandHandler("client", client_history))
    app.add_handler(CommandHandler("my_documents", my_documents))
    app.add_handler(CommandHandler("contracts", list_documents))
    app.add_handler(CommandHandler("invoices", list_documents))
    app.add_handler(CommandHandler("project", project_details))
    app.add_handler(CommandHandler("projects", delete_project_menu))
    app.add_handler(CallbackQueryHandler(list_documents, pattern="^list_(contracts|invoices)$"))
    app.add_handler(CallbackQueryHandler(document_action, pattern="^doc_action:(status|send|duplicate|archive):"))
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
        BotCommand("client", "View client history"),
        BotCommand("duplicate", "Duplicate a document"),
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
