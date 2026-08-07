# Multi-format Licensing Requirement UAT Pack

All files in this folder contain synthetic data only. They represent the same five-line,
one-year licensing requirement so testers can compare extraction results across channels.

## Expected requirement

| Line | SKU | Quantity | Term | Billing | Renewal date |
|---|---|---:|---|---|---|
| 1 | Microsoft 365 E3 | 120 | P1Y | Annual | 2026-09-30 |
| 2 | Power BI Pro | 30 | P1Y | Annual | 2026-10-15 |
| 3 | Enterprise Mobility + Security E3 | 120 | P1Y | Annual | 2026-09-30 |
| 4 | Microsoft Teams Phone Standard | 60 | P1Y | Annual | 2026-12-31 |
| 5 | Microsoft Defender for Office 365 (Plan 1) | 120 | P1Y | Annual | 2026-09-30 |

Every successful positive test must return exactly these five lines. Confirm full product
names, quantities, P1Y terms, annual billing, and renewal dates before approving pricing.

## Upload files

| File | Send through WhatsApp as | Expected processing path |
|---|---|---|
| `licensing_requirement.csv` | Document | Deterministic parser; no extraction-model call |
| `licensing_requirement.xlsx` | Document | Deterministic parser; no extraction-model call |
| `licensing_requirement_arbitrary_layout.xlsx` | Document | Deterministic parser finds the header below the cover rows |
| `licensing_requirement.tsv` | Document | Structured OpenAI extraction |
| `licensing_requirement.txt` | Document | Structured OpenAI extraction |
| `licensing_requirement.docx` | Document | Structured OpenAI extraction |
| `licensing_requirement_text.pdf` | Document | Structured OpenAI extraction from selectable text |
| `licensing_requirement_scanned.pdf` | Document | Structured OpenAI extraction from the embedded page image |
| `licensing_requirement.png` | Image/photo | Multimodal OpenAI extraction |
| `licensing_requirement.jpg` | Image/photo | Multimodal OpenAI extraction |
| `licensing_requirement.webp` | Image/photo | Multimodal OpenAI extraction |
| `negative_unclear_quantity.jpg` | Image/photo | Must ask for the missing Power BI Pro quantity; must not guess |
| `voice_note_script.txt` | Read aloud as a WhatsApp voice note | Transcription followed by structured extraction |

## Recommended test procedure

1. Send `/start` and confirm the professional advisor introduction appears.
2. Upload one positive test file.
3. Compare the returned confirmation table with the expected requirement above.
4. Correct a quantity before confirmation and verify the refreshed table.
5. Confirm the requirement and verify the Renew As-Is annual pricing output.
6. Upload the next file to begin another capture and repeat.
7. Send `negative_unclear_quantity.jpg`. Verify the agent asks for the Power BI Pro
   quantity and does not calculate a value or create a guessed line.
8. Read `voice_note_script.txt` aloud using the WhatsApp microphone. Verify the transcript
   is returned before the five-line confirmation table.

Use the actual image/photo control for PNG, JPG, and WebP. Do not send those three files
as generic documents; the WhatsApp image event is the path being tested.

## Cost note

CSV and both XLSX files use deterministic parsing. TSV, TXT, Word, both PDFs, images, and
voice require bounded OpenAI extraction. Use each unstructured format once during normal
UAT unless a failure needs investigation. Pricing and total calculations remain
deterministic application operations.

## Regeneration

Regenerate the complete pack after changing the canonical test data:

```powershell
.\.venv\Scripts\python.exe scripts\generate_multiformat_uat.py
```

