Phase 1 — Core docgen engine
Goal: turn structured data into a PDF, nothing else.

FastAPI service, single repo, separate from Gatekeeper
Two Jinja2/HTML templates (contract, invoice) styled with Tailwind/CSS
WeasyPrint render pipeline: POST /generate/contract, POST /generate/invoice → PDF bytes
Hardcode your info (mikesplore, address if you want one) into templates, pass client/project data as JSON
No database yet. No email. Just prove the render pipeline works and looks right.

Phase 2 — Data & numbering
Goal: make documents persistent and uniquely identifiable.

PostgreSQL: contracts and invoices tables (client name, project ref, amount, terms snapshot, status, created_at)
Sequential numbering per document type (e.g. MK-CON-0001, MK-INV-0001)
Store the generated PDF (filesystem or S3-compatible bucket) keyed by that ID
API to fetch a document by ID

Phase 3 — Delivery & acceptance
Goal: get it to the client and record their response.

Email send (SMTP or a transactional provider) with the PDF attached, from a consistent address
Acceptance tracking: since it's reply-based, this can be manual at first — you mark a contract accepted in the DB once the client replies "I accept," logging the date
Optional later: a small inbox-parsing job that watches for reply subject lines and auto-flips status. Skip this in Phase 3, it's not worth automating yet.

Phase 4 — Authenticity layer
Goal: the trust signal that replaces "corporate polish."

Compute a SHA256 hash of each generated PDF, store it against the document ID
Small public endpoint/page: mikesplore.me/verify/{id} shows document status (issued/accepted) and confirms the hash matches
Optional: embed a QR code in the PDF footer linking to that verify page

Phase 5 — Gatekeeper integration
Goal: close the loop between contract terms and enforcement.

Link a contracts row to a Gatekeeper-managed project/client record
When Gatekeeper suspends a project for non-payment, it can reference the contract's late-payment clause (even just logging "suspended per contract MK-CON-0001, clause 4") — mostly a data link, not new logic
Optional: auto-generate the invoice from Gatekeeper's payment records instead of typing amounts by hand

Phase 6 — Polish

Logo once you have one, dropped into the template
Simple admin form/CLI so you're not hand-writing JSON to generate a contract each time
PDF metadata (author, title) set properly for professionalism