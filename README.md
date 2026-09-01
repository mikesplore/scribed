# Scribed

Scribed generates mikesplore contract and invoice PDFs, stores them locally, and exposes them through a FastAPI API.

## First local test

```bash
cp .env.example .env
.venv/bin/uvicorn app.main:app --reload
```

Scribed loads variables from `.env` automatically. Do not commit `.env`.

Then open `http://localhost:8000/docs` and use `POST /contracts` or `POST /invoices`. The default SQLite database and `storage/` directory are created automatically.

For the Telegram bot, set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_OWNER_ID` in `.env`, then run:

```bash
.venv/bin/python bot.py
```

Document email delivery uses Resend. Set `RESEND_API_KEY` and `RESEND_FROM_EMAIL` before using the `/send` endpoints or Telegram `/send` command.

Use Telegram `/contracts` or `/invoices` to list stored documents, and `/status NUMBER` for one document.

Run automated checks with:

```bash
.venv/bin/pytest -q
```
