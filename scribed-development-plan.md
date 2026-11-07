# scribed — Development Plan

Contract, invoice, and receipt PDF generation service for freelance client work, branded under "mikesplore". Standalone service with no external business-system integration. Receipt inputs are caller assertions; payment validation and receipt eligibility remain outside Scribed.

## Stack

- **API**: FastAPI (Python)
- **Templates**: Jinja2 (HTML) + Tailwind/CSS
- **PDF render**: WeasyPrint
- **DB**: PostgreSQL
- **Storage**: local filesystem initially, S3-compatible bucket later if needed
- **Email**: SMTP or a transactional provider (Resend/Postmark/SES — pick one, don't build a custom mailer)

No registered business entity. No e-signature integration. Acceptance is via email reply ("I accept"), tracked manually in the DB.

No web UI or chat bot. Authorized consumers use the FastAPI document API directly.

## Repo structure

```
scribed/
  app/
    main.py
    templates/
      contract.html
      invoice.html
      receipt.html
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
  - `POST /generate/receipt` — body: receipt details supplied by the caller → returns preview PDF
- No auth needed yet (single-user tool). No DB yet.

**Acceptance criteria**: hitting either endpoint with valid JSON returns a correctly formatted, readable PDF. Visually check spacing, fonts, page breaks.

---

## Phase 2 — Persistence & numbering

**Goal**: documents are stored and uniquely numbered.

- Add PostgreSQL. Tables:
  - `contracts`: id, contract_number (e.g. `MK-CON-0001`), client_name, project_name, terms_json (snapshot of data used to render), status (`draft`/`sent`/`accepted`), created_at, pdf_path
  - `invoices`: id, invoice_number (e.g. `MK-INV-0001`), client_name, amount, currency, status (`draft`/`sent`), created_at, pdf_path
  - `receipts`: id, receipt_number (e.g. `MK-REC-0001`), validated receipt input snapshot, status, created_at, pdf_path
- Numbering: sequential per document type, generated at creation time, not reused.
- On generate, persist the structured terms snapshot in DB and return a freshly rendered PDF without storing a draft PDF.
- Render draft PDFs on demand from the saved snapshot. When a document is sent or otherwise issued, store that exact PDF and its hash in local storage or R2 so later downloads and verification refer to the issued file.
- New endpoints:
  - `GET /contracts/{id}` — fetch metadata + PDF
  - `GET /invoices/{id}` — same
  - `POST /receipts` — persist and issue an immutable receipt from caller-supplied content; retries use `Idempotency-Key`
  - `GET /receipts/{id}` — retrieve the issued receipt PDF

**Acceptance criteria**: generating a document creates a DB row with a unique sequential number and a retrievable PDF; drafts have no stored PDF, and the first issued PDF is stored as an immutable artifact.

---

## Phase 3 — Delivery & acceptance tracking

**Goal**: send it, track the reply.

- Add email sending: on `POST /contracts/{id}/send`, email the PDF to the client from a consistent sender address, mark status `sent`. Wire this to a bot command: `/send {number}`.
- Add `POST /contracts/{id}/mark-accepted` — you call this once the client replies "I accept", sets status `accepted` + timestamp. Bot command: `/accepted {number}`.
- Same pattern for invoice delivery status; payment status is outside this service.

**Acceptance criteria**: can send a real contract to a test email address and manually flip its status after replying.

---

## Phase 4 — Authenticity/verification layer

**Goal**: replace "corporate branding" with a real trust signal.

- On generation, compute SHA256 hash of the PDF bytes, store on the DB row.
- Add `GET /verify/{contract_number_or_invoice_number}` — public-facing, returns status (issued/sent/accepted) and confirms hash integrity. This is what `mikesplore.me/verify/{id}` will call.
- Optional: embed a QR code in the PDF footer pointing to the verify URL. Generate with `qrcode` (Python lib), embed as an image in the Jinja2 template before render.

**Acceptance criteria**: verify endpoint correctly reports status and hash for a real generated document; QR code in PDF resolves to the right page.

---

---

## Phase 5 — Polish

- Drop in the mikesplore logo once available; update templates.
- Set PDF metadata (author: mikesplore, title: contract/invoice number).
- Keep payment and suspension enforcement rules outside this service.

---

## Notes for the agent

- Phase 1 must be fully working and visually correct before starting Phase 2. Don't build ahead.
- Keep the contract template to roughly 1.5-2 pages of rendered PDF. Don't pad with generic legal boilerplate.
- No e-signature library, no third-party contract-signing service — acceptance is reply-based and manually tracked in Phase 3.
- No business registration fields (no company number, no VAT-style fields) — issuer identity is just "mikesplore" / Mike's name.
