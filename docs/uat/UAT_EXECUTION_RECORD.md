# SkySecure Microsoft Licensing Advisor UAT Execution Record

## Execution details

| Field | Value |
|---|---|
| Execution ID | |
| Date/time and timezone | |
| Tester | |
| Git commit | |
| Storage mode | `local` |
| Workflow mode | `upgrade_comparison` |
| Workbook SHA-256 | |
| ngrok URL | |
| Meta test-number Phone Number ID (last 4 only) | |
| Seller test number (last 4 only) | |

## Test results

| ID | Check | Expected evidence | Pass/Fail | Evidence/reference |
|---|---|---|---|---|
| UAT-01 | Health/readiness | HTTP 200, 4,030 price rows, local/memory | | |
| UAT-02 | Webhook verification | Meta GET callback returns challenge/200 | | |
| UAT-03 | Signature validation | Live POST logs signature validated | | |
| UAT-04 | Allowlist | Authorized sender accepted; unauthorized sender rejected | | |
| UAT-05 | CSV upload | Five exact SKU matches | | |
| UAT-06 | Mobile estate table image | Portrait PNG; full wrapped names; no horizontal scrolling or truncation | | |
| UAT-07 | Estate PDF | PDF received as WhatsApp document with dates/flags | | |
| UAT-08 | Automatic renewal | Renew As-Is prepared without bundle selection | | |
| UAT-09 | Promo safety | Promo-only E3 price unavailable until eligibility confirmed | | |
| UAT-10 | Initial seller validation gate | Edits/compare blocked until seller confirms refreshed analysis and pricing | | |
| UAT-11 | Natural quantity edit | `Change L1 to 120 licences` recalculates immediately | | |
| UAT-12 | Natural add/remove/replace | Requested line changes applied or safely confirmed | | |
| UAT-13 | Discount/adjustment | Five percent and -25,000 reflected in total | | |
| UAT-14 | Commercial settings | Term, billing, segment and currency validated | | |
| UAT-15 | Comment | Seller comment persists into final output | | |
| UAT-16 | Final validation gate | Finalize request sends summary; no final status/PDF before seller confirms | | |
| UAT-17 | Final proposal PDF | PDF received only after explicit final confirmation | | |
| UAT-18 | Annual options | Renew, ME3, ME5 and ME7 use P1Y/Annual | | |
| UAT-19 | No bundle inference | Every non-core add-on is retained/priced in each upgrade | | |
| UAT-20 | Annual difference | Comparison shows amount versus Renew As-Is | | |
| UAT-21 | Mobile comparison table image | All four options and recommendation visible in an inline portrait PNG | | |
| UAT-22 | Comparison PDF | Four-scenario PDF received as WhatsApp document | | |
| UAT-23 | Image delivery fallback | Forced image failure returns full-name responsive text, not an ASCII table | | |
| UAT-24 | Restart behavior | Session is lost after local server restart | | |

## Commercial evidence

| Proposal revision | Subtotal | Discount | Adjustment | Final total | Unresolved decisions |
|---|---:|---:|---:|---:|---:|
| Renew As-Is — initial | | | | | |
| Renew As-Is — final | | | | | |

| ME3 annual | | | | | |
| ME5 annual | | | | | |
| ME7 annual | | | | | |

## PDF evidence

| Document | WhatsApp message ID | Bytes | Pages | SHA-256 |
|---|---|---:|---:|---|
| Estate PDF | | | | |
| Renewal proposal PDF | | | | |
| Annual comparison PDF | | | | |

## Mobile table-image evidence

| Image | WhatsApp message ID | Pixels | Bytes | SHA-256 |
|---|---|---:|---:|---|
| Estate table PNG | | | | |
| Scenario table PNG | | | | |
| Annual comparison PNG | | | | |

## Defects and observations

| Defect ID | Severity | Description | Evidence | Owner | Status |
|---|---|---|---|---|---|
| | | | | | |

## Sign-off

| Role | Name | Decision | Date | Comments |
|---|---|---|---|---|
| Technical owner | | | | |
| Licensing/business reviewer | | | | |
| UAT owner | | | | |
