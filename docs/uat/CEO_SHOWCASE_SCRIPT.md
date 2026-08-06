# SkySecure Microsoft Licensing Advisor — CEO Showcase

## Product identity

**Product name:** SkySecure Microsoft Licensing Advisor

**Short name:** SkySecure Licensing Advisor
**Positioning:** A secure, conversational commercial assistant that analyses a Microsoft
licence estate, prepares editable annual proposals, compares ME3/ME5/ME7 options, and
produces customer-ready recommendations from the maintained Microsoft pricebook.

## Recommended demonstration format

Use the **Executive journey** for the main demonstration. It takes approximately ten
minutes and shows the complete customer outcome. Use the **Extended controls journey**
only if the CEO asks to see deeper seller operations and safety controls.

Only upload `docs/uat/synthetic_enterprise_estate.csv`. It contains synthetic data and
exact ProductId/SkuId values from the maintained `Final Output Sheet`.

## Why Microsoft 365 E3 initially requires confirmation

This is intentional and should be presented as a commercial safety feature, not a defect.
In Microsoft SKU V5.0, `Final Output Sheet` row 1,978 provides a promotional annual price
for Microsoft 365 E3 but no standard non-promotional price. The ME5 and ME7 target rows are
also promotion-only. The agent shows this pricing provisionally inside a locked validation
stage; the seller's single validation explicitly attests customer eligibility before any
editing or comparison is enabled.

Do not replace E3 in the synthetic file merely to hide this control. Removing the core-suite
line would make the upgrade seat base ambiguous and would not remove the ME3/ME5/ME7
promotion requirement. Instead, demonstrate that one seller confirmation validates the
estate, pricing, and promotion eligibility for all four options.

## Pre-demo checklist

- FastAPI `/health/ready` returns HTTP 200.
- ngrok forwards the current HTTPS URL to `http://localhost:8000`.
- Meta subscribes the WhatsApp Business Account webhook to `messages`.
- The tester's E.164 phone number is in `WHATSAPP_SELLER_ALLOWLIST`.
- `.env` uses `STORAGE_MODE=local`, `WORKFLOW_MODE=upgrade_comparison`, and
  `AI_INTENT_BACKEND=openai`.
- `RATE_CARD_LOCAL_PATH=docs/microsoft_sku_v5.xlsx` and
  `RATE_CARD_SHEET_NAME=Final Output Sheet`.
- Only the supplied synthetic customer file is used.
- Secrets, tokens, phone numbers, and `.env` are hidden from screen sharing.
- Start with a fresh upload so an earlier test session cannot affect the demonstration.

## Executive journey — exact WhatsApp prompts

### 1. Introduce the advisor

Send:

```text
/help
```

Show the branded capability guide. Explain that OpenAI understands the seller's language,
while SKU identity, price selection, edits, and totals remain deterministic and auditable.

### 2. Upload and analyse the estate

Attach:

```text
docs/uat/synthetic_enterprise_estate.csv
```

Show:

- the mobile portrait estate table with full wrapped product names;
- grouping by product family, quantities, and renewal dates;
- exact ProductId/SkuId matching for all five lines;
- the estate PDF delivered as a WhatsApp document; and
- the automatically prepared Renew As-Is proposal.

CEO talk track:

> The seller uploads the existing estate once. The advisor identifies each exact SKU,
> calculates renewable quantities, presents a phone-friendly table, and creates a formal
> PDF without the seller navigating the workbook.

### 3. Demonstrate provisional promotion governance

Point out that the E3 promotional amount is already displayed, but the proposal remains in
a locked seller-validation stage. The table is provisional and cannot yet be edited or
compared.

CEO talk track:

> The maintained workbook has a promotional E3 quote but no standard quote. The advisor
> can display that real row provisionally, but the seller must attest eligibility as part of
> the initial validation before any operation is enabled. The language model never decides
> eligibility or calculates the amount.

### 4. Record the initial seller validation

Point out the validation request beneath the refreshed proposal. Before confirming, try:

```text
Apply a 5 percent discount.
```

Show that the operation is blocked. Then choose `Confirm details` or send:

```text
I confirm the uploaded licence details, initial pricing, and promotion eligibility.
```

CEO talk track:

> Upload and automated pricing do not immediately grant permission to modify the proposal.
> The seller explicitly validates the SKU matches, quantities, dates, prices, total, and
> promotion eligibility in one approval.
> That approval is persisted before editing and comparison are enabled.

### 5. Demonstrate immediate quantity recalculation

Send:

```text
Change L1 to 120 licences.
```

Show that the revision number and final total change immediately.

### 6. Apply commercial controls naturally

Send these as separate messages:

```text
Apply a 5 percent discount.
```

```text
Subtract INR 25,000 as a commercial adjustment.
```

```text
Add the comment: Synthetic CEO demonstration; customer approval pending.
```

Show subtotal, discount percentage and amount, adjustment, and final annual total.

### 7. Generate all four annual options

Send:

```text
Compare the annual Renew As-Is, ME3, ME5, and ME7 options.
```

Show:

- all four scenarios created without manually building each one;
- one-year term and annual billing;
- annual total and difference from Renew As-Is;
- base, Copilot, and additional/retained cost components;
- existing add-ons retained separately without inventing bundle entitlement;
- the recommended option and one-line rationale;
- the mobile portrait comparison table; and
- `licensing-commercial-comparison.pdf` delivered as a WhatsApp document.

CEO talk track:

> The recommendation is explainable. It is calculated from review readiness and annual
> commercial totals, not generated from an untraceable model answer. Every option remains
> editable by the seller.

### 8. Finalize the selected proposal

For the shortest executive journey, keep Renew As-Is active and send:

```text
Finalize the proposal.
```

Show that the advisor displays a final validation summary with option, revision, subtotal,
discount, adjustment, and final annual total. Point out that no final PDF has arrived yet.
Then choose `Confirm & finalize` or send:

```text
Yes, I confirm these values and authorize finalization.
```

Show `licensing-renewal-proposal.pdf` arriving only after approval, with the complete
line-level configuration, commercial fields, comments, assumptions, unresolved-decision
section, and totals.

## Extended controls journey

Perform this section only when additional operational depth is useful. Begin with a fresh
upload and confirm the combined initial validation so the extended flow is isolated and
easy to explain.

### 9. Add a new SKU by exact maintained title

Send:

```text
Add 10 Visio Plan 2 licences.
```

Expected result: exact SKU resolution, a new proposal line, and immediate recalculation
using the standard P1Y/Annual Commercial price in the workbook.

### 10. Remove a retained line

Send:

```text
Remove L4 from the proposal.
```

Expected result: Teams Phone is removed from the active proposal and the total changes.

### 11. Demonstrate safe fuzzy-match confirmation

Send:

```text
Replace L5 with Defender for Office 365 Plan Two, quantity 115.
```

The deliberately non-canonical product wording should produce `confirmation_required`.
Read the numbered
candidates returned by the advisor and reply with the displayed number, for example:

```text
Choose option 1.
```

Do not assume the number in advance; select the candidate whose complete title is
`Microsoft Defender for Office 365 (Plan 2)`.

CEO talk track:

> A fuzzy result is never committed silently. The advisor persists the proposed change,
> shows the best maintained-SKU candidates, and mutates the commercial proposal only after
> explicit seller confirmation.

### 12. Demonstrate independent Copilot quantity

Prepare ME5:

```text
Prepare the ME5 option with 25 Copilot licences.
```

The newly prepared option inherits the promotion eligibility already validated by the
seller. Then send:

```text
Set Copilot to 30 licences.
```

Show that Copilot changes independently from the ME5 base quantity and is priced as a
separate line.

### 13. Demonstrate annual-contract validation

Send:

```text
Change the billing plan to monthly.
```

Expected result: a clear rejection because this use case enforces annual billing. Then send:

```text
Keep annual billing.
```

### 14. Demonstrate currency safety

Send:

```text
Convert the proposal to USD.
```

Expected result: the advisor refuses to invent an exchange rate because the workbook has no
approved FX table. Continue in INR.

### 15. Recompare edited scenarios

Send:

```text
Compare all four annual options again.
```

Show that the changed scenario total, recommendation, mobile table, and comparison PDF are
regenerated from persisted proposal revisions.

### 16. Finalize the active scenario

Resolve any decision still shown by the advisor, then send:

```text
Finalize this proposal.
```

Show that finalization is blocked if a price or seller decision remains unresolved. When the
proposal is ready, show the separate final-validation summary and confirm it with:

```text
Yes, finalize this proposal.
```

The final PDF must be delivered only after that second seller approval.

## Closing CEO script

Use this wording after the final PDF arrives:

> SkySecure Microsoft Licensing Advisor converts a complex, workbook-driven licensing
> review into a controlled WhatsApp workflow. It analyses exact SKUs, preserves commercial
> governance, accepts ordinary seller language, recalculates deterministically after every
> approved edit, compares four annual strategies, explains its recommendation, and produces
> customer-ready evidence. OpenAI provides the conversational interface, but it cannot
> select prices, invent entitlement rules, or change totals. The maintained Microsoft
> workbook remains the commercial source of truth, and the application is independent of
> the legacy Phase 1 code.

## Evidence to capture

- `/health/ready` HTTP 200.
- ngrok POST 200 and successful webhook-signature validation.
- WhatsApp message IDs for upload, comparison, and finalization.
- Estate, proposal, and comparison PNG screenshots on the phone.
- Estate, proposal, and comparison PDFs received as WhatsApp documents.
- Before/after revision numbers and totals for quantity, discount, and adjustment edits.
- The fuzzy SKU confirmation prompt and explicit seller selection.
- The recommendation and rationale.
- Any error output with credentials and customer identifiers redacted.

Record formal results in `docs/uat/UAT_EXECUTION_RECORD.md`.
