# V1 Simple Pricing UAT

Use synthetic licensing data only. The active configuration must show
`WORKFLOW_MODE=simple_pricing`, `AI_INTENT_BACKEND=openai`, and
`REQUIREMENT_CAPTURE_BACKEND=openai`.

## Seller journey

1. Send `/start`. Confirm the three-step help message does not offer discount,
   margin, adjustment, or promotion controls.
2. Send a manual requirement: `25 Power BI Pro licences, P1Y with annual billing`.
3. Confirm the mobile table shows the full SKU name, quantity, and term/billing,
   and that no price has been calculated yet.
4. Reply `Change L1 to 30 licences`. Confirm the refreshed capture table shows 30.
5. Choose **Confirm requirement**. Confirm the as-is table and PDF show SKU,
   quantity, billing term, Marketplace unit price, line total, and overall value.
6. Confirm no seller output contains `0% discount`, distributor discount, margin,
   adjustment, promotion eligibility, Partner Best Offer, or an internal price build-up.
7. Choose **Yes, revise**, then send `Add 10 Microsoft 365 Copilot licences`.
8. If more than one catalogue identity is offered, explicitly choose the intended
   ProductId/SkuId. Confirm no change occurs before that choice.
9. Confirm the revised table shows the added line and the comparison shows current,
   revised, and difference values.
10. Send `Replace L1 with Microsoft 365 E3`. Confirm the existing quantity is retained
    when no new quantity is stated and the replacement/addition is explained.
11. Send `Compare the current and revised configuration`. Confirm WhatsApp receives the
    final PDF as a document and that the PDF identifies the pricing source as
    `FYD Final Output Sheet - Price on Marketplace`.
12. Send `Finalize the revised configuration`, review the final seller-validation gate,
    and confirm. Verify the final PDF is sent only after this approval.

## Input-channel checks

Repeat steps 2-5 using each of the following synthetic inputs:

- standard CSV/XLSX template (deterministic parser, no extraction model call);
- arbitrarily laid-out Excel workbook;
- Word document;
- text-based and scanned PDF;
- PNG/JPEG/WebP screenshot; and
- WhatsApp voice note shorter than five minutes.

For voice, confirm the transcript is shown before the extracted requirement table. For
every unstructured channel, deliberately make one quantity unclear and verify the agent
asks a clarification question instead of guessing.

## Cost and authority boundary

- Standard CSV/XLSX: no OpenAI extraction call; only natural-language intent calls after
  capture if the seller uses free-form edits.
- Text/Word/PDF/arbitrary Excel/image: one bounded structured Responses API extraction.
- Voice: one bounded Audio Transcriptions API call plus one structured text extraction.
- Voice duration is limited by `MAX_AUDIO_SECONDS` (default 300), uploads are limited by
  byte-size configuration, PDF/image detail is `low`, output tokens are capped, and
  Responses calls use `store=False`.
- The model never receives the FYD pricebook and never calculates a price. Exact/fuzzy
  catalogue matching, Marketplace price selection, multiplication, totals, revisions, and
  confirmation gates remain deterministic application operations.
- Generic “recommend a better SKU” requests must not invent an entitlement decision while
  the business rule sheet is pending. The agent asks for the source line, target capability
  or SKU, and user count.
