# scribed — Development Plan

Contract and invoice PDF generation service for freelance client work, branded under "mikesplore". Standalone service, separate from `gatekeeperd`, integrated with it in later phases.

## Stack

- **API**: FastAPI (Python)
- **Templates**: Jinja2 (HTML) + Tailwind/CSS
- **PDF render**: WeasyPrint
- **DB**: PostgreSQL
- **Storage**: local filesystem initially, S3-compatible bucket later if needed
- **Email**: SMTP or a transactional provider (Resend/Postmark/SES — pick one, don't build a custom mailer)

No registered business entity. No e-signature integration. Acceptance is via email reply ("I accept"), tracked manually in the DB.

No web UI. Management interface is a Telegram bot (`python-telegram-bot`), calling the FastAPI service as a client — not replacing it. Gatekeeper and any future consumer should still be able to call the API directly without going through Telegram.

## Repo structure

```
scribed/
  app/
    main.py
    templates/
      contract.html
      invoice.html
    render.py          # WeasyPrint render logic
    models.py           # DB models
    schemas.py           # Pydantic request/response models
    routers/
      contracts.py
      invoices.py
      verify.py           # phase 4+
    db.py
  static/
    styles.css           # or Tailwind build output
    logo.png              # phase 6
  tests/
  Dockerfile
  docker-compose.yml
```

---

## Phase 1 — Core render engine

**Goal**: JSON in, PDF out. No persistence, no email.

- Scaffold FastAPI app.
- Build `templates/contract.html` and `templates/invoice.html` with placeholders for: client name, project name, scope, amount, payment schedule, dates, contract/invoice number, "mikesplore" as issuer identity.
- Contract template must include these clauses (see "Contract content" section below for full text guidance): scope, deliverables/timeline, payment terms, late-payment/suspension clause, revisions, IP ownership, hosting/maintenance note, confidentiality, termination, liability limitation, governing law (Kenya), acceptance line.
- Implement `render.py`: takes a dict of fields, renders template, returns PDF bytes via WeasyPrint.
- Endpoints:
  - `POST /generate/contract` — body: client/project/terms data → returns PDF (streamed or as bytes response)
  - `POST /generate/invoice` — body: client/project/amount data → returns PDF
- No auth needed yet (single-user tool). No DB yet.

**Acceptance criteria**: hitting either endpoint with valid JSON returns a correctly formatted, readable PDF. Visually check spacing, fonts, page breaks.

---

## Phase 2 — Persistence & numbering

**Goal**: documents are stored and uniquely numbered.

- Add PostgreSQL. Tables:
  - `contracts`: id, contract_number (e.g. `MK-CON-0001`), client_name, project_name, terms_json (snapshot of data used to render), status (`draft`/`sent`/`accepted`), created_at, pdf_path
  - `invoices`: id, invoice_number (e.g. `MK-INV-0001`), client_name, amount, currency, status (`draft`/`sent`/`paid`), created_at, pdf_path
- Numbering: sequential per document type, generated at creation time, not reused.
- On generate, persist the PDF to disk under a predictable path (`storage/contracts/{contract_number}.pdf`) and store the path + terms snapshot in DB.
- New endpoints:
  - `GET /contracts/{id}` — fetch metadata + PDF
  - `GET /invoices/{id}` — same

**Acceptance criteria**: generating a document creates a DB row with a unique sequential number and a retrievable PDF.

### Telegram bot (basic)

- Standalone service using `python-telegram-bot`, calling the scribed API as a client.
- Gate by Telegram user ID — only your own ID is allowed to issue commands.
- `/newcontract` and `/newinvoice` — collect client/project/amount data via a short conversation or a single structured message, call `POST /generate/{type}`, return the PDF in-chat.
- `/status {number}` — fetch and report document status.

**Acceptance criteria**: can generate and receive a contract or invoice PDF entirely from Telegram, with no other user able to trigger it.

---

## Phase 3 — Delivery & acceptance tracking

**Goal**: send it, track the reply.

- Add email sending: on `POST /contracts/{id}/send`, email the PDF to the client from a consistent sender address, mark status `sent`. Wire this to a bot command: `/send {number}`.
- Add `POST /contracts/{id}/mark-accepted` — you call this once the client replies "I accept", sets status `accepted` + timestamp. Bot command: `/accepted {number}`.
- Same pattern for invoices (`sent` → `paid`, marked via `/paid {number}` or a future Gatekeeper/Paystack webhook hook).

**Acceptance criteria**: can send a real contract to a test email address and manually flip its status after replying.

---

## Phase 4 — Authenticity/verification layer

**Goal**: replace "corporate branding" with a real trust signal.

- On generation, compute SHA256 hash of the PDF bytes, store on the DB row.
- Add `GET /verify/{contract_number_or_invoice_number}` — public-facing, returns status (issued/sent/accepted) and confirms hash integrity. This is what `mikesplore.me/verify/{id}` will call.
- Optional: embed a QR code in the PDF footer pointing to the verify URL. Generate with `qrcode` (Python lib), embed as an image in the Jinja2 template before render.

**Acceptance criteria**: verify endpoint correctly reports status and hash for a real generated document; QR code in PDF resolves to the right page.

---

## Phase 5 — Gatekeeper integration

**Goal**: link contract terms to enforcement.

- Add a `gatekeeper_project_id` foreign key (or loose reference string) on `contracts` linking it to the relevant `gatekeeperd` client/project.
- When `gatekeeperd` suspends a project for non-payment, it should be able to look up the linked contract and log the suspension against it (e.g. "suspended per contract MK-CON-0001, late-payment clause") — this is a data link and a log line, not new business logic in scribed itself.
- Optional: expose `GET /contracts/by-project/{gatekeeper_project_id}` so gatekeeperd can pull the relevant contract/clause text when it suspends.
- Optional: auto-populate invoice amounts from Gatekeeper's Paystack payment records instead of manual entry.

**Acceptance criteria**: a Gatekeeper suspension event can resolve back to the specific contract and clause it's enforcing.

---

## Phase 6 — Polish

- Drop in the mikesplore logo once available; update templates.
- Set PDF metadata (author: mikesplore, title: contract/invoice number).
- Polish the Telegram bot flow (better prompts, inline confirmation before send, error handling on bad input).
- Standardize the late-fee/suspension timeline (e.g. 7 days = warning, 14 days = suspension) as a constant/config value referenced in both the contract template and Gatekeeper logic, not re-decided per contract.

---

## Notes for the agent

- Phase 1 must be fully working and visually correct before starting Phase 2. Don't build ahead.
- Keep the contract template to roughly 1.5-2 pages of rendered PDF. Don't pad with generic legal boilerplate.
- No e-signature library, no third-party contract-signing service — acceptance is reply-based and manually tracked in Phase 3.
- No business registration fields (no company number, no VAT-style fields) — issuer identity is just "mikesplore" / Mike's name.