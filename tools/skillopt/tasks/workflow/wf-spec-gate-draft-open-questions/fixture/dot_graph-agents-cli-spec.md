# Invoice assistant: spec

Status: DRAFT -- not approved

## Purpose
Answer finance staff's questions about customer invoices (status, due dates, amounts).

## External APIs
- billing API (`/invoices`, `/invoices/{invoice_id}`), bearer token `BILLING_API_TOKEN`.

## Open questions (must be answered before approval)
1. Access: read-only, or may the agent also mark invoices as paid (`POST /invoices/{invoice_id}/pay`)?
2. Model provider: may invoice data go to a hosted provider, or must it stay on our network?

## Deployment
Prototype first.
