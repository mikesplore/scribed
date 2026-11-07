# Scribed plan

## Phase 1 — Core document generation

FastAPI service with Jinja2 contract and invoice templates and WeasyPrint rendering. `POST /generate/contract` and `POST /generate/invoice` accept validated structured data and return PDF bytes.

## Phase 2 — Document persistence

Persist contract, invoice, and receipt records, assign sequential document numbers, store immutable issued PDFs, and retrieve documents by ID. Receipt content is supplied by the calling client; Scribed does not validate payment or decide receipt eligibility.

## Phase 3 — Delivery and acceptance

Send issued documents through a transactional email provider and record contract acceptance after an email reply.

## Phase 4 — Authenticity

Store SHA256 hashes and expose a public verification endpoint for issued documents.

## Phase 5 — Polish

Add branding assets and PDF metadata. Keep payment verification, balances, and receipt eligibility outside Scribed.
