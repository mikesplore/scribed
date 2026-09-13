# Scribed

Scribed is the document and client-operations service for the **mikesplore** freelance platform. It generates professional contract and invoice PDFs, stores records, sends them through Resend, and tracks their lifecycle.

Scribed works alongside [`mikesplore/gatekeeperd`](https://github.com/mikesplore/gatekeeperd), which owns hosted projects, payment gating, access control, and Paystack events.

## Responsibilities

- Scribed: contracts, invoices, PDFs, numbering, Resend delivery, verification, and Telegram workflows.
- Gatekeeperd: hosted projects, blocking/unblocking, Paystack payments, Nginx integration, and project audit history.

The Telegram bot is a management interface that calls both APIs; it does not duplicate their business logic.

## Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set `SCRIBED_API_TOKEN`, `TELEGRAM_BOT_TOKEN`, and `TELEGRAM_OWNER_ID` in `.env`. Scribed loads `.env` automatically; never commit it.

For local development, start both processes together with:

```bash
./dev-start.sh
```

The API uses port `8000` by default; set `PORT` to change it. The Telegram bot
webhook is served by the same FastAPI process and port. Route the configured
webhook URL and path to the API through the reverse proxy.

## First local test

```bash
cp .env.example .env
./dev-start.sh
```

Scribed loads variables from `.env` automatically. Do not commit `.env`.

The API documentation is at <http://localhost:8000/docs> locally, or at <https://scribed.mikesplore.me/docs> in production. The default SQLite database and `storage/` directory are created automatically.

In Docker, database migrations run automatically before the API and Telegram bot
start (`alembic upgrade head`). Set `DATABASE_URL` in the container environment
to the PostgreSQL connection string. The `scribed` Compose service exposes one
port (`9005`) and starts both processes. For local development, update the
schema with `alembic upgrade head`.

Polling is disabled. Configure `TELEGRAM_WEBHOOK_URL` and
`TELEGRAM_WEBHOOK_SECRET` before starting the bot.

## Telegram commands

```bash
 /start
/newcontract
/newinvoice
/contracts
/invoices
/status NUMBER
/send NUMBER
/accepted NUMBER
/paid NUMBER
/cancel
```

Use `/help` to see the complete capability list. Telegram also shows these commands in its native command menu after the bot starts.

Document creation is conversational and requires confirmation. `/start` also provides buttons for creation, listing, and deletion. Only `TELEGRAM_OWNER_ID` can use commands.

## API and security

Management routes require `Authorization: Bearer <SCRIBED_API_TOKEN>`. `/health` and `/verify/{number}` are public. The API rate limit defaults to 60 requests per minute per client IP and is configurable with `RATE_LIMIT_PER_MINUTE`. CORS is disabled by default; configure trusted origins with the comma-separated `CORS_ALLOWED_ORIGINS` variable.

Contracts can be archived with `DELETE /contracts/{id}` and permanently deleted only afterward with `DELETE /contracts/{id}/permanent`. Audit history is available at `GET /audit/{number}`.

Resend delivery requires `RESEND_API_KEY` and `RESEND_FROM_EMAIL`.

## Gatekeeperd integration

Contracts can store a Gatekeeperd project UUID in `gatekeeper_project_id`. Gatekeeperd can retrieve the linked contract at `GET /contracts/by-project/{id}` and notify Scribed of a suspension at `POST /integrations/gatekeeper/suspensions` using `X-Gatekeeper-Secret`.

Configure `SCRIBED_CALLBACK_URL` and `SCRIBED_INTEGRATION_SECRET` in Gatekeeperd, with the matching secret in Scribed.

## Testing

```bash
.venv/bin/pytest -q
.venv/bin/python -m compileall -q app bot.py
```
