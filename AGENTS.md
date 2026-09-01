# Scribed — Agent Instructions

## Project

Scribed is a standalone FastAPI service for generating branded contract and invoice PDFs for freelance client work under the `mikesplore` identity.

The development roadmap is defined in [scribed-development-plan.md](scribed-development-plan.md). Read it before starting substantial work and keep implementation aligned with the current phase.

## Repository conventions

- Always prefix shell commands with `rtk`, as required by `/home/mike/.codex/RTK.md`.
- Use the project virtual environment for Python commands: `.venv/bin/python`, `.venv/bin/pytest`, and `.venv/bin/uvicorn`.
- Install dependencies into `.venv` rather than the system interpreter.
- Use `rg` or `rg --files` for searching files and text.
- Use `apply_patch` for source and configuration edits.
- Preserve unrelated user changes in the working tree.
- Do not add authentication, persistence, email, Telegram, or Gatekeeper integration ahead of the phase that specifies it.

## Architecture

- API: FastAPI in `app/`.
- Templates: Jinja2 HTML in `app/templates/`.
- PDF rendering: WeasyPrint through `app/render.py`.
- Request and response validation: Pydantic models in `app/schemas.py`.
- Database and filesystem storage are Phase 2 concerns.
- Telegram is a management client, not a replacement for the HTTP API.

## Phase discipline

- Complete and verify Phase 1 before starting Phase 2.
- Phase 1 is JSON in → PDF out, with no persistence or email.
- Keep contracts approximately 1.5–2 rendered PDF pages and avoid unnecessary legal boilerplate.
- Do not introduce business registration, company-number, or VAT-style fields.
- Acceptance is email-reply based; do not add e-signature integrations.

## Document requirements

Contract PDFs must include:

- scope
- deliverables and timeline
- payment terms
- late-payment and work-suspension clause
- revisions
- IP ownership
- hosting and maintenance note
- confidentiality
- termination
- liability limitation
- governing law of Kenya
- acceptance line

Both document types should retain `mikesplore` as the issuer identity and support client name, project name, amount, dates, payment details, and document number fields where applicable.

## Verification

After implementation changes:

1. Run the focused tests with `rtk pytest -q`.
2. Run `rtk python -m compileall -q app` for Python changes.
3. For rendering changes, directly verify that generated output begins with `%PDF` and inspect page/layout behavior when practical.
4. Report any environment-specific limitations, especially WeasyPrint or dependency issues.

## Current API

- `GET /health`
- `POST /generate/contract`
- `POST /generate/invoice`
