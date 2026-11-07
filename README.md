# Scribed

Scribed is a standalone HTTP service for validating, rendering, storing, and retrieving branded contract and invoice PDFs for freelance work under the **mikesplore** identity.

## Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set `SCRIBED_API_TOKEN` in `.env`. Do not commit `.env`.

Start the API and apply migrations with `./dev-start.sh`. The local API uses port `8001` by default; set `PORT` to change it. API documentation is available at <http://localhost:8001/docs> in development.

To run Uvicorn directly from the repository root, use `.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload`. The ASGI module is `app.main`, not `main`.

The service accepts complete contract, invoice, and receipt document inputs, renders PDFs, stores issued artifacts, and serves them through its HTTP API. A receipt request is the calling client's assertion of the receipt content; Scribed does not verify payments, calculate balances, or decide whether a receipt is eligible to be issued.

In Docker, database migrations run before the API starts. Set `DATABASE_URL` to the PostgreSQL connection string. The Compose service exposes port `9005`.

## API and security

Management routes require `Authorization: Bearer <SCRIBED_API_TOKEN>`. `/health` and `/verify/{number}` are public. Rate limiting defaults to 60 requests per minute per client IP and is configurable with `RATE_LIMIT_PER_MINUTE`. CORS is disabled by default; set `CORS_ALLOWED_ORIGINS` to trusted origins if needed.

The API supports direct PDF previews at `POST /generate/contract`, `POST /generate/invoice`, and `POST /generate/receipt`; persisted creation at `POST /contracts`, `POST /invoices`, and `POST /receipts`; and retrieval at `GET /contracts/{id}`, `GET /invoices/{id}`, and `GET /receipts/{id}`. `Idempotency-Key` makes persisted document creation retries safe. Receipts require this header; they are issued and stored as immutable PDFs as part of `POST /receipts`. The PDF response includes `X-Document-Id`, `X-Document-Number`, and `Location` headers for later retrieval. A request accepted before a rendering interruption remains pending and can be safely retried with the same key. The retry returns the stored PDF when issuance already completed.

## API client registration

The `SCRIBED_API_TOKEN` is the administrator credential. Use it to register each calling application at `POST /api-clients` with its name, address, contact email, and phone. Scribed returns a random API token once; store it securely. Scribed stores only its hash. Use `GET /api-clients` to view registered profiles, `PUT /api-clients/{id}` to update one, and `DELETE /api-clients/{id}` to revoke its token. A scoped client can read and update only its own profile using `GET /api-clients/me` and `PATCH /api-clients/me`; this lets integrations maintain their document attribution without needing the administrator credential.

Send the registered client's token as `Authorization: Bearer <client-token>` with document requests. The form contains only the document's fields. Scribed identifies the requester from the token and adds the registered profile under “Prepared on behalf of” in generated documents. The profile used is snapshotted with persisted documents. Client tokens can generate documents and retrieve only their own persisted documents; the administrator token manages registrations and has full API access.

For example, register `Example Studio` with its business contact details, then submit an invoice form with `client_name`, `project_name`, `description`, and `amount` using that client's token. The form does not include `Example Studio`'s identity; Scribed adds it from the credential's registered profile.

A receipt form uses `client_name`, `project_name`, `description`, `amount`, `currency`, `received_date`, and optional `payment_method` and `payment_reference`. Its number is assigned by Scribed. The registered API client is added as the “Prepared on behalf of” identity. The client is responsible for submitting only receipt details that it is authorized to issue; Scribed does not confirm that money was received.

PDF rendering runs in an isolated worker with configurable `PDF_RENDER_TIMEOUT_SECONDS` and `PDF_RENDER_MEMORY_MB` limits. Contracts can be archived with `DELETE /contracts/{id}` and permanently deleted afterward with `DELETE /contracts/{id}/permanent`.

## Testing

```bash
.venv/bin/pytest -q
.venv/bin/python -m compileall -q app
```
