# SkySecure Microsoft Licensing Advisor UAT execution record

## Execution details

| Field | Value |
|---|---|
| Execution ID | |
| Date/time and timezone | |
| Tester | |
| Git commit | |
| Environment (`local`/Azure candidate) | |
| Storage mode | |
| Workflow mode | `simple_pricing` |
| Active workbook ETag/SHA-256 | |
| Public callback host | |
| Meta Phone Number ID (last 4 only) | |
| Seller number (last 4 only) | |

Never paste credentials, full phone numbers, raw webhook bodies, or customer data here.

## Test results

| ID | Check | Expected evidence | Pass/Fail | Evidence/reference |
|---|---|---|---|---|
| UAT-01 | Health/readiness | HTTP 200; positive current-price rows; expected storage/dispatch backends | | |
| UAT-02 | Webhook verification | Live Meta GET callback returns the challenge with HTTP 200 | | |
| UAT-03 | Signature validation | Live signed POST accepted; invalid signature rejected | | |
| UAT-04 | Seller allowlist | Named tester accepted; unauthorized sender rejected | | |
| UAT-05 | Manual text capture | SKU, quantity and annual term extracted; no price before confirmation | | |
| UAT-06 | Standard CSV/XLSX | Deterministic capture with full names and quantities | | |
| UAT-07 | Arbitrary Excel/Word/PDF/image/voice | Structured capture shown for seller confirmation | | |
| UAT-08 | Mobile capture table | Portrait image; full wrapped names; no horizontal scrolling | | |
| UAT-09 | Capture PDF | PDF received as a WhatsApp document | | |
| UAT-10 | Pre-confirmation correction | Seller can add/replace/remove/change quantity before pricing | | |
| UAT-11 | Initial validation gate | No pricing until explicit seller confirmation | | |
| UAT-12 | As-is pricing | SKU, quantity, annual term, unit price, line total and overall value | | |
| UAT-13 | V1 commercial boundary | No discount, margin, promotion, partner-price or internal calculation output | | |
| UAT-14 | Revised SKU/quantity | Seller-directed change recalculates immediately | | |
| UAT-15 | Fuzzy-match safety | Sub-100 match requires explicit candidate selection before mutation | | |
| UAT-16 | Recommendation safety | Generic request asks for capability/target/users; no invented entitlement | | |
| UAT-17 | Current-versus-revised comparison | Renew As-Is, revised total and difference are clearly separated | | |
| UAT-18 | Final validation gate | No final status/PDF before seller confirmation | | |
| UAT-19 | Final PDF | Customer-ready PDF received only after final confirmation | | |
| UAT-20 | Unsupported currency/billing | Unsupported request is rejected without silently repricing | | |
| UAT-21 | Zero/blank price | Visible unavailable warning; line excluded from total, never called free | | |
| UAT-22 | Duplicate webhook | Same Meta message ID does not apply an edit twice | | |
| UAT-23 | Session ordering | Two rapid edits from one seller are processed in order | | |
| UAT-24 | Restart behavior | Local session is lost; Azure Blob session resumes after restart | | |
| UAT-25 | Logging hygiene | No raw message, phone, file content, workbook row or secret in telemetry | | |

## Commercial evidence

| Configuration | SKU lines | Overall value | Difference vs Renew As-Is | Unavailable-price lines |
|---|---:|---:|---:|---:|
| Renew As-Is | | | `0.00` | |
| Revised | | | | |

## Document evidence

| Document/image | WhatsApp message ID hash/ref | Bytes | Pages/pixels | SHA-256 |
|---|---|---:|---|---|
| Capture table PNG | | | | |
| Capture PDF | | | | |
| As-is price PNG | | | | |
| As-is PDF | | | | |
| Comparison PNG | | | | |
| Comparison PDF | | | | |
| Final PDF | | | | |

## Defects and observations

| Defect ID | Severity | Description | Evidence | Owner | Status |
|---|---|---|---|---|---|
| | | | | | |

## Sign-off

| Role | Name | Decision | Date/time | Reference |
|---|---|---|---|---|
| Engineering | | | | |
| Licensing/business | | | | |
| Privacy/security | | | | |
| Product owner | | | | |
