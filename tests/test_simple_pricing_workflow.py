import re
import unittest
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import wave

from pypdf import PdfReader

from app.api.whatsapp.service import HELP_TEXT, ServiceConfiguration, WhatsAppWebhookService
from app.core.licensing.analysis import LicenseAnalyzer
from app.core.licensing.agent import OfficialRecommendation
from app.core.licensing.capture import (
    CapturedRequirement,
    ExtractedRequirementLine,
    RequirementExtraction,
    OpenAIRequirementExtractor,
    _audio_duration_seconds,
    _prepare_audio,
)
from app.core.licensing.models import (
    EstateStatus,
    LicenseEstate,
    NormalizedLicenseLine,
    ParsedLicenseRow,
    ScenarioType,
    WorkflowStage,
)
from app.core.licensing.orchestrator import LicensingOrchestrator
from app.core.licensing.rate_card import LocalRateCardSource, RateCardProvider
from app.core.licensing.renderer import (
    format_pending_matches,
    render_proposal_pdf,
    render_simple_commercial_pdf,
)
from app.core.licensing.scenarios import ScenarioEngine
from app.core.licensing.store import InMemoryWorkflowStore
from app.core.whatsapp import WhatsAppMedia
from app.schema.whatsapp import WhatsAppWebhookPayload


ROOT = Path(__file__).parents[1]
WORKBOOK = ROOT / "docs" / "microsoft_sku_v5.xlsx"
CUSTOMER = ROOT / "docs" / "client_upload_sheet.csv"


class FakeWhatsAppClient:
    def __init__(self, media: WhatsAppMedia) -> None:
        self.media = media
        self.messages: list[object] = []
        self.documents: list[dict[str, object]] = []
        self.images: list[dict[str, object]] = []

    async def download_media(self, **_: object) -> WhatsAppMedia:
        return self.media

    async def send_message(self, message: object) -> dict[str, object]:
        self.messages.append(message)
        return {}

    async def send_document(self, **kwargs: object) -> dict[str, object]:
        self.documents.append(kwargs)
        return {}

    async def send_image(self, **kwargs: object) -> dict[str, object]:
        self.images.append(kwargs)
        return {}


class FakeRequirementExtractor:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.capture = CapturedRequirement(
            extraction=RequirementExtraction(
                lines=[
                    ExtractedRequirementLine(
                        sku_name="Power BI Pro",
                        quantity=25,
                        term_duration="P1Y",
                        billing_plan="Annual",
                        product_id="",
                        sku_id="",
                        expiration_date="",
                        renewal_date="",
                    )
                ],
                warnings=[],
                needs_clarification=False,
                clarification="",
            )
        )

    async def extract_text(self, *_: object, **__: object) -> CapturedRequirement:
        self.calls.append("text")
        return self.capture

    async def extract_file(self, *_: object, **__: object) -> CapturedRequirement:
        self.calls.append("file")
        return self.capture

    async def extract_image(self, *_: object, **__: object) -> CapturedRequirement:
        self.calls.append("image")
        return self.capture

    async def extract_audio(self, *_: object, **__: object) -> CapturedRequirement:
        self.calls.append("audio")
        return self.capture.model_copy(update={"transcript": "25 Power BI Pro annual"})

    async def close(self) -> None:
        return None


class ContextAwareFragmentExtractor:
    """Deterministic fake that proves consecutive seller turns are combined."""

    def __init__(self, *, product: str = "Office 365 E1", quantity: int = 10) -> None:
        self.product = product
        self.quantity = quantity
        self.inputs: list[str] = []

    async def extract_text(
        self,
        text: str,
        **_: object,
    ) -> CapturedRequirement:
        self.inputs.append(text)
        has_quantity_turn = "Seller message 2:" in text
        return CapturedRequirement(
            extraction=RequirementExtraction(
                lines=[
                    ExtractedRequirementLine(
                        sku_name=self.product,
                        quantity=self.quantity if has_quantity_turn else 0,
                        term_duration="P1Y",
                        billing_plan="Annual",
                        product_id="",
                        sku_id="",
                        expiration_date="",
                        renewal_date="",
                    )
                ],
                warnings=[],
                needs_clarification=not has_quantity_turn,
                clarification=(
                    "How many licences should I include?"
                    if not has_quantity_turn
                    else ""
                ),
            )
        )

    async def close(self) -> None:
        return None


class TranscriptRequirementExtractor:
    """Contract-faithful fake for short, multi-turn product/quantity capture."""

    def __init__(self) -> None:
        self.inputs: list[str] = []

    async def extract_text(self, text: str, **_: object) -> CapturedRequirement:
        self.inputs.append(text)
        normalized = text.casefold()
        if "copilot" in normalized:
            product = "Copilot"
        elif "power bi" in normalized:
            product = "Power BI licence"
        else:
            product = "E1"
        seller_followups = re.findall(r"Seller message \d+:\s*([^\n]+)", text)
        quantity = 0
        for value in seller_followups[1:]:
            match = re.search(r"\b(\d+)\b", value)
            if match:
                quantity = int(match.group(1))
        needs_clarification = quantity <= 0
        return CapturedRequirement(
            extraction=RequirementExtraction(
                lines=[
                    ExtractedRequirementLine(
                        sku_name=product,
                        quantity=quantity,
                        term_duration="P1Y",
                        billing_plan="Annual",
                        product_id="",
                        sku_id="",
                        expiration_date="",
                        renewal_date="",
                    )
                ],
                warnings=[],
                needs_clarification=needs_clarification,
                clarification=(
                    f"How many {product} licences should I include?"
                    if needs_clarification
                    else ""
                ),
            )
        )

    async def close(self) -> None:
        return None


class FakeRecommendationAdvisor:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def advise(self, **kwargs: object) -> OfficialRecommendation:
        self.calls.append(kwargs)
        return OfficialRecommendation(
            recommendation=(
                "Official Microsoft documentation supports reviewing Microsoft 365 E5 "
                "for the capability requested by the seller."
            ),
            clarification_question="",
            suggested_candidate_numbers=[1],
            source_urls=[
                "https://learn.microsoft.com/en-us/microsoft-365/enterprise/"
            ],
        )

    async def validate_model_access(self) -> None:
        return None

    async def close(self) -> None:
        return None


class FakeResponses:
    def __init__(self, parsed: RequirementExtraction) -> None:
        self.parsed = parsed
        self.calls: list[dict[str, object]] = []

    async def parse(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(status="completed", output_parsed=self.parsed)


class FakeTranscriptions:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(text="25 Power BI Pro licences, annual billing")


class FakeOpenAIClient:
    def __init__(self, parsed: RequirementExtraction) -> None:
        self.responses = FakeResponses(parsed)
        self.audio = SimpleNamespace(transcriptions=FakeTranscriptions())


def _webhook(message: dict[str, object]) -> WhatsAppWebhookPayload:
    return WhatsAppWebhookPayload.model_validate(
        {
            "object": "whatsapp_business_account",
            "entry": [{"changes": [{"value": {"messages": [message]}}]}],
        }
    )


class OpenAIRequirementCaptureContractTests(unittest.IsolatedAsyncioTestCase):
    def test_whatsapp_ogg_opus_is_converted_to_supported_wav(self) -> None:
        import av

        ogg_buffer = BytesIO()
        output = av.open(ogg_buffer, "w", format="ogg")
        stream = output.add_stream("libopus", rate=48000)
        stream.layout = "mono"
        frame = av.AudioFrame(format="s16", layout="mono", samples=4800)
        frame.sample_rate = 48000
        for plane in frame.planes:
            plane.update(bytes(plane.buffer_size))
        for packet in stream.encode(frame):
            output.mux(packet)
        for packet in stream.encode(None):
            output.mux(packet)
        output.close()

        wav, filename, mime_type = _prepare_audio(
            ogg_buffer.getvalue(),
            filename="voice.ogg",
            mime_type="audio/ogg; codecs=opus",
        )

        self.assertEqual(filename, "voice-note.wav")
        self.assertEqual(mime_type, "audio/wav")
        self.assertTrue(wav.startswith(b"RIFF"))
        self.assertGreater(_audio_duration_seconds(wav), 0)

    async def test_file_image_and_voice_calls_are_bounded_and_not_stored(self) -> None:
        parsed = RequirementExtraction(
            lines=[
                ExtractedRequirementLine(
                    sku_name="Power BI Pro",
                    quantity=25,
                    term_duration="P1Y",
                    billing_plan="Annual",
                    product_id="",
                    sku_id="",
                    expiration_date="",
                    renewal_date="",
                )
            ],
            warnings=[],
            needs_clarification=False,
            clarification="",
        )
        client = FakeOpenAIClient(parsed)
        extractor = OpenAIRequirementExtractor(
            api_key="test-key",
            model="gpt-5.6-luna",
            transcription_model="gpt-transcribe",
            client=client,
        )

        await extractor.extract_file(
            b"%PDF-synthetic",
            filename="requirement.pdf",
            mime_type="application/pdf",
        )
        file_call = client.responses.calls[-1]
        self.assertFalse(file_call["store"])
        self.assertEqual(file_call["max_output_tokens"], 2400)
        file_item = file_call["input"][0]["content"][0]  # type: ignore[index]
        self.assertEqual(file_item["type"], "input_file")
        self.assertEqual(file_item["detail"], "low")

        await extractor.extract_image(
            b"synthetic-image",
            filename="requirement.png",
            mime_type="image/png",
        )
        image_call = client.responses.calls[-1]
        image_item = image_call["input"][0]["content"][0]  # type: ignore[index]
        self.assertEqual(image_item["type"], "input_image")
        self.assertEqual(image_item["detail"], "low")

        wav_buffer = BytesIO()
        with wave.open(wav_buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(16000)
            wav_file.writeframes(b"\x00\x00" * 1600)
        captured = await extractor.extract_audio(
            wav_buffer.getvalue(),
            filename="voice.wav",
            mime_type="audio/wav",
        )
        self.assertEqual(captured.transcript, "25 Power BI Pro licences, annual billing")
        audio_call = client.audio.transcriptions.calls[-1]
        self.assertEqual(audio_call["model"], "gpt-transcribe")
        self.assertEqual(len(client.responses.calls), 3)


class SimplePricingWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.provider = RateCardProvider(
            LocalRateCardSource(WORKBOOK),
            sheet_name="Final Output Sheet",
            refresh_seconds=3600,
        )
        self.store = InMemoryWorkflowStore()
        self.orchestrator = LicensingOrchestrator(
            analyzer=LicenseAnalyzer(self.provider),
            rate_cards=self.provider,
            scenarios=ScenarioEngine(
                apply_bundle_rules=False,
                price_basis="marketplace",
            ),
            store=self.store,
            default_term_duration="P1Y",
            default_billing_plan="Annual",
            default_segment="Commercial",
        )

    async def asyncTearDown(self) -> None:
        await self.store.close()
        await self.provider.close()

    async def test_explicit_public_access_accepts_sender_outside_allowlist(self) -> None:
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = WhatsAppWebhookService(
            client,  # type: ignore[arg-type]
            self.orchestrator,
            ServiceConfiguration(
                seller_allowlist=frozenset(),
                max_document_bytes=10 * 1024 * 1024,
                allow_all_sellers=True,
                workflow_mode="simple_pricing",
            ),
        )

        await service.handle(
            _webhook(
                {
                    "id": "public-help",
                    "from": "911111111111",
                    "type": "text",
                    "text": {"body": "/help"},
                }
            )
        )

        self.assertEqual(len(client.messages), 1)
        self.assertIn(
            "SkySecure Microsoft Licensing Advisor",
            client.messages[0].text.body,  # type: ignore[attr-defined]
        )

    async def test_default_access_rejects_sender_outside_allowlist(self) -> None:
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = WhatsAppWebhookService(
            client,  # type: ignore[arg-type]
            self.orchestrator,
            ServiceConfiguration(
                seller_allowlist=frozenset(),
                max_document_bytes=10 * 1024 * 1024,
                workflow_mode="simple_pricing",
            ),
        )

        await service.handle(
            _webhook(
                {
                    "id": "blocked-help",
                    "from": "911111111111",
                    "type": "text",
                    "text": {"body": "/help"},
                }
            )
        )

        self.assertEqual(len(client.messages), 1)
        self.assertEqual(
            client.messages[0].text.body,  # type: ignore[attr-defined]
            "This WhatsApp number is not authorized.",
        )

    async def test_confirmation_precedes_marketplace_pricing_and_revision(self) -> None:
        client = FakeWhatsAppClient(
            WhatsAppMedia(CUSTOMER.read_bytes(), CUSTOMER.name, "text/csv")
        )
        service = WhatsAppWebhookService(
            client,  # type: ignore[arg-type]
            self.orchestrator,
            ServiceConfiguration(
                frozenset(),
                10 * 1024 * 1024,
                allow_all_sellers=True,
                workflow_mode="simple_pricing",
            ),
        )
        await service.handle(
            _webhook(
                {
                    "id": "simple-upload",
                    "from": "911111111111",
                    "type": "document",
                    "document": {
                        "id": "customer-file",
                        "filename": CUSTOMER.name,
                        "mime_type": "text/csv",
                    },
                }
            )
        )

        session = await self.orchestrator.get_session("911111111111")
        assert session is not None
        self.assertEqual(session.stage, WorkflowStage.AWAITING_INITIAL_VALIDATION)
        self.assertEqual(session.scenarios, {})
        self.assertIsNone(session.confirmed_as_is)

        await service._handle_text("911111111111", "/set L1 46")
        session = await self.orchestrator.get_session("911111111111")
        assert session is not None and session.estate is not None
        self.assertEqual(session.stage, WorkflowStage.AWAITING_INITIAL_VALIDATION)
        self.assertEqual(session.estate.lines[0].renewal_quantity, 46)
        self.assertEqual(session.scenarios, {})

        await service._handle_text("911111111111", "/validate")
        session = await self.orchestrator.get_session("911111111111")
        assert session is not None and session.confirmed_as_is is not None
        current = session.confirmed_as_is
        self.assertEqual(session.stage, WorkflowStage.REVIEWING_SCENARIO)
        first = next(line for line in current.lines if line.line_id == "L1")
        catalog = await self.provider.get()
        price_row = catalog.price_rows(
            product_id=first.product_id or "",
            sku_id=first.sku_id or "",
            sku_title=first.sku_title,
            term_duration=first.term_duration,
            billing_plan=first.billing_plan,
            segment="Commercial",
        )[0]
        self.assertEqual(first.unit_price, price_row.marketplace_price.quantize(Decimal("0.01")))
        self.assertNotEqual(first.unit_price, price_row.partner_best_offer.quantize(Decimal("0.01")))
        baseline_total = current.total_value

        await service._handle_text("911111111111", "/set L1 50")
        session = await self.orchestrator.get_session("911111111111")
        assert session is not None and session.confirmed_as_is is not None
        revised = session.scenarios[session.active_scenario]  # type: ignore[index]
        self.assertEqual(
            next(line for line in session.confirmed_as_is.lines if line.line_id == "L1").proposed_quantity,
            46,
        )
        self.assertEqual(next(line for line in revised.lines if line.line_id == "L1").proposed_quantity, 50)
        self.assertEqual(
            revised.total_value - baseline_total,
            first.unit_price * 4,
        )

        await service._handle_text(
            "911111111111",
            "/replace L1 | Power BI Pro | 50",
        )

        await service._handle_text("911111111111", "/compare")
        filenames = [item["filename"] for item in client.documents]
        self.assertIn("as-is-commercial.pdf", filenames)
        self.assertIn("renew-as-is-vs-selected.pdf", filenames)
        self.assertTrue(any(name.startswith("as-is-cost-") for name in [item["filename"] for item in client.images]))
        final_pdf = next(
            item["content"]
            for item in client.documents
            if item["filename"] == "renew-as-is-vs-selected.pdf"
        )
        reader = PdfReader(BytesIO(final_pdf))  # type: ignore[arg-type]
        pdf_text = "\n".join(page.extract_text() or "" for page in reader.pages)
        for required in (
            "Confirmed Renew As-Is configuration",
            "Seller-requested revised configuration",
            "Term / billing",
            "Renewal / expiration",
            "Unit price",
            "Overall requirement value",
            "Commercial comparison",
            "Difference",
            "Replaced 10-Year Audit Log Retention Add On with Power BI Pro",
        ):
            self.assertIn(required, pdf_text)
        for forbidden in (
            "0% discount",
            "Distributor discount",
            "Available margin",
            "Partner Best Offer",
            "Discount percentage",
            "CSP",
            "Rate-card version",
            "Source file:",
            "Source requirement:",
            "Revision",
        ):
            self.assertNotIn(forbidden, pdf_text)
        assert session.estate is not None
        shareable_pdfs = [
            *(document["content"] for document in client.documents),
            render_proposal_pdf(session.estate, revised),
        ]
        for content in shareable_pdfs:
            document_text = "\n".join(
                page.extract_text() or ""
                for page in PdfReader(BytesIO(content)).pages  # type: ignore[arg-type]
            )
            self.assertNotIn(CUSTOMER.name, document_text)
            self.assertNotIn("Source file:", document_text)
            self.assertNotIn("Source requirement:", document_text)
            self.assertNotIn("Revision", document_text)

    async def test_ambiguous_e5_requires_relevant_seller_confirmation(self) -> None:
        sender = "ambiguous-e5-seller"
        estate = await self.orchestrator.analyze_extracted(
            sender=sender,
            source_file="seller-message",
            rows=[
                ParsedLicenseRow(
                    row_number=2,
                    product_title="E5 license",
                    total_licenses=25,
                    expired_licenses=0,
                    assigned_licenses=0,
                    renewal_quantity=25,
                )
            ],
        )

        self.assertEqual(estate.status, EstateStatus.AWAITING_MATCH_CONFIRMATION)
        line = estate.lines[0]
        self.assertEqual(line.match_method, "unresolved")
        self.assertIsNone(line.sku_title)
        self.assertEqual(
            [candidate.sku_title for candidate in line.candidates],
            [
                "Microsoft 365 E5 without Audio Conferencing",
                "Office 365 E5 without Audio Conferencing",
                "Enterprise Mobility + Security E5",
            ],
        )
        self.assertFalse(
            any("Dynamics 365" in candidate.sku_title for candidate in line.candidates)
        )

        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = WhatsAppWebhookService(
            client,  # type: ignore[arg-type]
            self.orchestrator,
            ServiceConfiguration(
                frozenset(),
                10 * 1024 * 1024,
                allow_all_sellers=True,
                workflow_mode="simple_pricing",
            ),
        )
        await service._process_captured_estate(sender, estate)
        message_text = "\n".join(
            message.text.body  # type: ignore[attr-defined]
            for message in client.messages
            if getattr(message, "text", None) is not None
        )
        self.assertIn("Which product family and plan do you mean?", message_text)
        self.assertIn("Microsoft 365 E5 without Audio Conferencing", message_text)
        self.assertNotIn("Dynamics 365", message_text)
        self.assertNotIn("confidence", message_text.casefold())
        self.assertNotIn("ProductId", message_text)

    async def test_fragmented_product_and_quantity_are_combined_without_reasking(self) -> None:
        class CaptureInterpreter:
            async def interpret(self, *_: object) -> object:
                return SimpleNamespace(action="capture_requirement")

        sender = "fragmented-e1-seller"
        extractor = ContextAwareFragmentExtractor()
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = WhatsAppWebhookService(
            client,  # type: ignore[arg-type]
            self.orchestrator,
            ServiceConfiguration(
                frozenset(),
                10 * 1024 * 1024,
                allow_all_sellers=True,
                workflow_mode="simple_pricing",
            ),
            intent_interpreter=CaptureInterpreter(),  # type: ignore[arg-type]
            requirement_extractor=extractor,  # type: ignore[arg-type]
        )

        await service._handle_text(sender, "Let's start with Office 365 E1 licence")
        first_response = client.messages[-1].text.body  # type: ignore[attr-defined]
        self.assertIn("I’ve noted Office 365 E1", first_response)
        self.assertIn("How many licences", first_response)
        self.assertNotIn("Which Microsoft product", first_response)

        await service._handle_text(sender, "Maybe around 10")

        self.assertEqual(len(extractor.inputs), 2)
        self.assertIn("Office 365 E1 licence", extractor.inputs[1])
        self.assertIn("Maybe around 10", extractor.inputs[1])
        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.estate is not None
        self.assertEqual(session.capture_messages, [])
        self.assertEqual(len(session.estate.lines), 1)
        self.assertEqual(session.estate.lines[0].display_title, "Office 365 E1")
        self.assertEqual(session.estate.lines[0].renewal_quantity, 10)
        self.assertEqual(session.stage, WorkflowStage.AWAITING_INITIAL_VALIDATION)

    async def test_single_e1_candidate_supports_unsure_and_full_name_confirmation(self) -> None:
        sender = "single-e1-candidate-seller"
        estate = await self.orchestrator.analyze_extracted(
            sender=sender,
            source_file="seller-message",
            rows=[
                ParsedLicenseRow(
                    row_number=2,
                    product_title="E1",
                    total_licenses=10,
                    expired_licenses=0,
                    assigned_licenses=0,
                    renewal_quantity=10,
                )
            ],
        )
        self.assertEqual(
            [candidate.sku_title for candidate in estate.pending_lines[0].candidates],
            ["Office 365 E1"],
        )
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = WhatsAppWebhookService(
            client,  # type: ignore[arg-type]
            self.orchestrator,
            ServiceConfiguration(
                frozenset(),
                10 * 1024 * 1024,
                allow_all_sellers=True,
                workflow_mode="simple_pricing",
            ),
        )

        await service._process_captured_estate(sender, estate)
        initial_text = "\n".join(
            message.text.body  # type: ignore[attr-defined]
            for message in client.messages
            if getattr(message, "text", None) is not None
        )
        self.assertIn("one close catalogue match", initial_text)
        self.assertNotIn("more than one possible match", initial_text)
        self.assertIn("1. Office 365 E1", initial_text)

        await service._handle_text(sender, "I don't know")
        unsure_text = client.messages[-2].text.body  # type: ignore[attr-defined]
        self.assertIn("No problem — I can help", unsure_text)
        self.assertIn("Office 365 E1", unsure_text)
        self.assertIn("10 licences", unsure_text)

        await service._handle_text(sender, "Office 365 E1")
        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.estate is not None
        self.assertEqual(len(session.estate.lines), 1)
        self.assertEqual(session.estate.lines[0].match_method, "seller_confirmed")
        self.assertEqual(session.estate.lines[0].display_title, "Office 365 E1")
        self.assertEqual(session.estate.lines[0].renewal_quantity, 10)
        self.assertEqual(session.stage, WorkflowStage.AWAITING_INITIAL_VALIDATION)

    async def test_remove_pending_line_and_start_fresh_do_not_enter_scenario_editing(self) -> None:
        class RemoveInterpreter:
            async def interpret(self, *_: object) -> object:
                return SimpleNamespace(
                    action="set_disposition",
                    disposition="remove",
                    line_id="L1",
                )

        sender = "remove-pending-requirement-seller"
        await self.orchestrator.analyze_extracted(
            sender=sender,
            source_file="seller-message",
            rows=[
                ParsedLicenseRow(
                    row_number=2,
                    product_title="E1",
                    total_licenses=10,
                    expired_licenses=0,
                    assigned_licenses=0,
                    renewal_quantity=10,
                )
            ],
        )
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = WhatsAppWebhookService(
            client,  # type: ignore[arg-type]
            self.orchestrator,
            ServiceConfiguration(
                frozenset(),
                10 * 1024 * 1024,
                allow_all_sellers=True,
                workflow_mode="simple_pricing",
            ),
            intent_interpreter=RemoveInterpreter(),  # type: ignore[arg-type]
        )

        await service._handle_text(sender, "Remove that L1")
        session = await self.orchestrator.get_session(sender)
        assert session is not None
        self.assertIsNone(session.estate)
        self.assertEqual(session.stage, WorkflowStage.AWAITING_UPLOAD)
        response = client.messages[-1].text.body  # type: ignore[attr-defined]
        self.assertIn("requirement draft is now empty", response)
        self.assertNotIn("Select a scenario", response)

        await self.orchestrator.analyze_extracted(
            sender=sender,
            source_file="replacement-draft",
            rows=[
                ParsedLicenseRow(
                    row_number=2,
                    product_title="Power BI Pro",
                    total_licenses=7,
                    expired_licenses=0,
                    assigned_licenses=0,
                    renewal_quantity=7,
                )
            ],
        )
        await service._handle_text(sender, "Just start from fresh")
        session = await self.orchestrator.get_session(sender)
        assert session is not None
        self.assertIsNone(session.estate)
        self.assertEqual(session.capture_messages, [])
        self.assertIn("cleared the previous draft", client.messages[-1].text.body)  # type: ignore[attr-defined]

    async def test_fragmented_addition_preserves_existing_requirement(self) -> None:
        class CaptureInterpreter:
            async def interpret(self, *_: object) -> object:
                return SimpleNamespace(action="capture_requirement")

        sender = "fragmented-addition-seller"
        await self.orchestrator.analyze_extracted(
            sender=sender,
            source_file="initial-requirement",
            rows=[
                ParsedLicenseRow(
                    row_number=2,
                    product_title="Power BI Pro",
                    total_licenses=25,
                    expired_licenses=0,
                    assigned_licenses=0,
                    renewal_quantity=25,
                )
            ],
        )
        extractor = ContextAwareFragmentExtractor()
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = WhatsAppWebhookService(
            client,  # type: ignore[arg-type]
            self.orchestrator,
            ServiceConfiguration(
                frozenset(),
                10 * 1024 * 1024,
                allow_all_sellers=True,
                workflow_mode="simple_pricing",
            ),
            intent_interpreter=CaptureInterpreter(),  # type: ignore[arg-type]
            requirement_extractor=extractor,  # type: ignore[arg-type]
        )

        await service._handle_text(sender, "Also include Office 365 E1")
        await service._handle_text(sender, "10 licences")

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.estate is not None
        self.assertEqual(
            [(line.display_title, line.renewal_quantity) for line in session.estate.lines],
            [("Power BI Pro", 25), ("Office 365 E1", 10)],
        )
        self.assertEqual(session.capture_messages, [])

    async def test_live_style_dialogue_retains_slots_blocks_premature_yes_and_compares_four(
        self,
    ) -> None:
        class ClarifyingInterpreter:
            def __init__(self) -> None:
                self.messages: list[str] = []

            async def interpret(self, message: str, *_: object) -> object:
                self.messages.append(message)
                if "confirm the complete requirement" in message.casefold():
                    return SimpleNamespace(action="confirm_validation")
                return SimpleNamespace(
                    action="clarify",
                    clarification="How many licences should I include?",
                )

            async def close(self) -> None:
                return None

        sender = "ceo-transcript-regression"
        interpreter = ClarifyingInterpreter()
        extractor = TranscriptRequirementExtractor()
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = WhatsAppWebhookService(
            client,  # type: ignore[arg-type]
            self.orchestrator,
            ServiceConfiguration(
                frozenset(),
                10 * 1024 * 1024,
                allow_all_sellers=True,
                workflow_mode="simple_pricing",
            ),
            intent_interpreter=interpreter,  # type: ignore[arg-type]
            requirement_extractor=extractor,
        )

        await service._handle_text(sender, "I want E1 licence")
        await service._handle_text(sender, "10")
        await service._handle_text(sender, "Office 365 E1")

        await service._handle_text(sender, "Let's add Power BI licence now")
        await service._handle_text(sender, "15")
        await service._handle_text(sender, "Power BI Pro")

        await service._handle_text(sender, "I want Copilot licence also")
        await service._handle_text(sender, "Yes")
        session = await self.orchestrator.get_session(sender)
        assert session is not None
        self.assertIsNone(session.confirmed_as_is)
        self.assertTrue(session.capture_messages)
        self.assertEqual(session.stage, WorkflowStage.AWAITING_INITIAL_VALIDATION)

        await service._handle_text(sender, "5")
        await service._handle_text(sender, "Microsoft 365 Copilot")
        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.estate is not None
        self.assertEqual(
            [(line.display_title, line.renewal_quantity) for line in session.estate.lines],
            [
                ("Office 365 E1", 10),
                ("Power BI Pro", 15),
                ("Microsoft 365 Copilot", 5),
            ],
        )
        self.assertIsNone(session.confirmed_as_is)

        await service._handle_text(
            sender,
            "Confirm the complete requirement and calculate Renew As-Is",
        )
        session = await self.orchestrator.get_session(sender)
        assert session is not None
        self.assertIsNotNone(session.confirmed_as_is)

        calls_before_comparison = len(interpreter.messages)
        await service._handle_text(sender, "Compare with other 4")
        session = await self.orchestrator.get_session(sender)
        assert session is not None
        self.assertEqual(set(session.scenarios), set(ScenarioType))
        self.assertEqual(len(interpreter.messages), calls_before_comparison)
        self.assertTrue(
            any(
                item["filename"] == "annual-licensing-comparison.pdf"
                for item in client.documents
            )
        )

    async def test_capture_stage_sku_choice_applies_and_displays_product_id(self) -> None:
        sender = "capture-sku-choice-seller"
        await self.orchestrator.analyze_extracted(
            sender=sender,
            source_file="seller-message",
            rows=[
                ParsedLicenseRow(
                    row_number=2,
                    product_title="Office 365 E1",
                    total_licenses=45,
                    expired_licenses=0,
                    assigned_licenses=0,
                    renewal_quantity=45,
                    term_duration="P1Y",
                    billing_plan="Annual",
                )
            ],
        )
        await self.orchestrator.request_requirement_validation(sender)
        pending_result = await self.orchestrator.add_requirement_sku(
            sender,
            "Copilot",
            30,
        )
        assert pending_result.confirmation is not None
        selected = pending_result.confirmation.candidates[1]
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = WhatsAppWebhookService(
            client,  # type: ignore[arg-type]
            self.orchestrator,
            ServiceConfiguration(
                frozenset(),
                10 * 1024 * 1024,
                allow_all_sellers=True,
                workflow_mode="simple_pricing",
            ),
        )

        await service._send_sku_change_result(sender, pending_result)
        option_text = client.messages[-2].text.body  # type: ignore[attr-defined]
        option_list = client.messages[-1].interactive  # type: ignore[attr-defined]
        self.assertIn(f"Product ID: {selected.product_id}", option_text)
        self.assertIn(
            f"Product ID {selected.product_id}",
            option_list.action.sections[0].rows[1].description,
        )

        await service._handle_interactive(
            sender,
            f"licensing|sku_confirm|{pending_result.confirmation.id}|2",
        )

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.estate is not None
        self.assertEqual(session.stage, WorkflowStage.AWAITING_INITIAL_VALIDATION)
        self.assertIsNone(session.pending_sku_change)
        self.assertIn(
            (selected.sku_title, 30),
            [
                (line.display_title, line.renewal_quantity)
                for line in session.estate.lines
            ],
        )

    async def test_initial_match_and_premium_candidates_are_seller_safe(self) -> None:
        catalog = await self.provider.get()
        premium = catalog.candidates("Power BI Premium", limit=3)
        self.assertTrue(premium)
        self.assertTrue(all("Premium" in item.sku_title for item in premium))
        self.assertNotIn("Power BI Pro", [item.sku_title for item in premium])

        estate = await self.orchestrator.analyze_extracted(
            sender="identifier-match-seller",
            source_file="seller-message",
            rows=[
                ParsedLicenseRow(
                    row_number=2,
                    product_title="E3",
                    total_licenses=15,
                    expired_licenses=0,
                    assigned_licenses=0,
                    renewal_quantity=15,
                )
            ],
        )
        rendered = format_pending_matches(estate)
        for candidate in estate.pending_lines[0].candidates:
            self.assertIn(f"Product ID: {candidate.product_id}", rendered)

    async def test_greeting_exposes_saved_draft_and_prevents_silent_quantity_merge(
        self,
    ) -> None:
        sender = "saved-draft-seller"
        await self.orchestrator.analyze_extracted(
            sender=sender,
            source_file="previous-session",
            rows=[
                ParsedLicenseRow(
                    row_number=2,
                    product_title="Office 365 E1",
                    total_licenses=20,
                    expired_licenses=0,
                    assigned_licenses=0,
                    renewal_quantity=20,
                    term_duration="P1Y",
                    billing_plan="Annual",
                )
            ],
        )
        await self.orchestrator.request_requirement_validation(sender)
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = WhatsAppWebhookService(
            client,  # type: ignore[arg-type]
            self.orchestrator,
            ServiceConfiguration(
                frozenset(),
                10 * 1024 * 1024,
                allow_all_sellers=True,
                workflow_mode="simple_pricing",
            ),
        )

        await service._handle_text(sender, "Hi")
        response = client.messages[-1].text.body  # type: ignore[attr-defined]
        self.assertIn("active saved draft", response)
        self.assertIn("Resume", response)
        self.assertIn("Start fresh", response)

        await service._handle_text(sender, "I want E1 licence")
        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.estate is not None
        self.assertEqual(session.estate.total_renewal_quantity, 20)
        self.assertIsNotNone(session.pending_dialogue)
        self.assertIn("will not merge", client.messages[-1].text.body)  # type: ignore[attr-defined]

        await service._handle_text(sender, "Start fresh")
        session = await self.orchestrator.get_session(sender)
        assert session is not None
        self.assertIsNone(session.estate)
        self.assertIsNone(session.pending_dialogue)

    async def test_missing_price_reopens_confirmation_instead_of_saving_zero_cost(
        self,
    ) -> None:
        catalog = await self.provider.get()
        item = next(
            row
            for row in catalog.items
            if row.marketplace_price == 0
            and row.term_duration == "P1Y"
            and row.billing_plan == "Annual"
            and (row.segment or "").casefold() == "commercial"
        )
        sender = "missing-price-seller"
        await self.orchestrator.analyze_extracted(
            sender=sender,
            source_file="seller-message",
            rows=[
                ParsedLicenseRow(
                    row_number=2,
                    product_title=item.sku_title,
                    product_id=item.product_id,
                    sku_id=item.sku_id,
                    total_licenses=10,
                    expired_licenses=0,
                    assigned_licenses=0,
                    renewal_quantity=10,
                    term_duration="P1Y",
                    billing_plan="Annual",
                )
            ],
        )
        await self.orchestrator.request_requirement_validation(sender)
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = WhatsAppWebhookService(
            client,  # type: ignore[arg-type]
            self.orchestrator,
            ServiceConfiguration(
                frozenset(),
                10 * 1024 * 1024,
                allow_all_sellers=True,
                workflow_mode="simple_pricing",
            ),
        )

        await service._confirm_validation(sender)

        session = await self.orchestrator.get_session(sender)
        assert session is not None
        self.assertEqual(session.stage, WorkflowStage.AWAITING_INITIAL_VALIDATION)
        self.assertIsNone(session.confirmed_as_is)
        self.assertEqual(session.scenarios, {})
        response = client.messages[-1].text.body  # type: ignore[attr-defined]
        self.assertIn("Pricing clarification required", response)
        self.assertIn(item.sku_title, response)
        self.assertNotIn("INR 0", response)

    async def test_seller_details_are_included_in_pdf_only_when_supplied(self) -> None:
        sender = "proposal-detail-seller"
        estate = await self.orchestrator.analyze_document(
            sender=sender,
            filename=CUSTOMER.name,
            content=CUSTOMER.read_bytes(),
        )
        await self.orchestrator.request_requirement_validation(sender)
        await self.orchestrator.confirm_requirement(sender)
        scenario = await self.orchestrator.build_scenario(
            sender,
            ScenarioType.RENEW_AS_IS,
            promo_eligible=False,
        )
        await self.orchestrator.save_confirmed_as_is(sender, scenario)

        without_details = render_simple_commercial_pdf(estate, scenario)
        without_text = "\n".join(
            page.extract_text() or ""
            for page in PdfReader(BytesIO(without_details)).pages
        )
        self.assertNotIn("Proposal details", without_text)

        estate = await self.orchestrator.set_requirement_detail(
            sender,
            label="Customer name",
            value="Contoso UAT Ltd",
        )
        with_details = render_simple_commercial_pdf(estate, scenario)
        with_text = "\n".join(
            page.extract_text() or ""
            for page in PdfReader(BytesIO(with_details)).pages
        )
        self.assertIn("Proposal details", with_text)
        self.assertIn("Customer name", with_text)
        self.assertIn("Contoso UAT Ltd", with_text)

    async def test_recommendation_is_same_family_and_requires_selection(self) -> None:
        sender = "recommendation-seller"
        await self.orchestrator.analyze_extracted(
            sender=sender,
            source_file="seller-message",
            rows=[
                ParsedLicenseRow(
                    row_number=2,
                    product_title="Microsoft 365 E3",
                    total_licenses=120,
                    expired_licenses=0,
                    assigned_licenses=0,
                    renewal_quantity=120,
                    term_duration="P1Y",
                    billing_plan="Annual",
                )
            ],
        )
        await self.orchestrator.request_requirement_validation(sender)
        await self.orchestrator.confirm_requirement(sender)
        baseline = await self.orchestrator.build_scenario(
            sender,
            ScenarioType.RENEW_AS_IS,
            promo_eligible=False,
        )
        await self.orchestrator.save_confirmed_as_is(sender, baseline)

        result = await self.orchestrator.recommend_higher_tier(
            sender,
            line_id="L1",
        )

        self.assertEqual(result.state, "confirmation_required")
        assert result.confirmation is not None
        self.assertTrue(result.confirmation.candidates)
        self.assertTrue(
            all(
                candidate.sku_title.startswith("Microsoft 365 E")
                for candidate in result.confirmation.candidates
            )
        )
        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.active_scenario is not None
        unchanged = session.scenarios[session.active_scenario]
        self.assertEqual(unchanged.id, baseline.id)
        self.assertEqual(
            next(line.sku_title for line in unchanged.lines if line.line_id == "L1"),
            next(line.sku_title for line in baseline.lines if line.line_id == "L1"),
        )

    async def test_requested_recommendation_is_officially_grounded_before_sku_choice(
        self,
    ) -> None:
        sender = "grounded-recommendation-seller"
        await self.orchestrator.analyze_extracted(
            sender=sender,
            source_file="seller-message",
            rows=[
                ParsedLicenseRow(
                    row_number=2,
                    product_title="Microsoft 365 E3",
                    total_licenses=100,
                    expired_licenses=0,
                    assigned_licenses=0,
                    renewal_quantity=100,
                    term_duration="P1Y",
                    billing_plan="Annual",
                )
            ],
        )
        await self.orchestrator.request_requirement_validation(sender)
        await self.orchestrator.confirm_requirement(sender)
        baseline = await self.orchestrator.build_scenario(
            sender,
            ScenarioType.RENEW_AS_IS,
            promo_eligible=False,
        )
        await self.orchestrator.save_confirmed_as_is(sender, baseline)
        advisor = FakeRecommendationAdvisor()
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = WhatsAppWebhookService(
            client,  # type: ignore[arg-type]
            self.orchestrator,
            ServiceConfiguration(
                frozenset(),
                10 * 1024 * 1024,
                allow_all_sellers=True,
                workflow_mode="simple_pricing",
            ),
            recommendation_advisor=advisor,
        )

        await service._execute_agent_intent(
            sender,
            SimpleNamespace(
                action="request_recommendation",
                line_id="L1",
                quantity=-1,
            ),
            original_message="Recommend a better option for advanced security",
        )

        self.assertEqual(len(advisor.calls), 1)
        bodies = [
            message.text.body  # type: ignore[attr-defined]
            for message in client.messages
            if getattr(message, "text", None) is not None
        ]
        self.assertTrue(
            any("Microsoft-documented licensing insight" in body for body in bodies)
        )
        self.assertFalse(any("learn.microsoft.com" in body for body in bodies))
        self.assertFalse(any("http://" in body or "https://" in body for body in bodies))
        interactive = [
            message.interactive  # type: ignore[attr-defined]
            for message in client.messages
            if getattr(message, "interactive", None) is not None
        ]
        self.assertEqual(len(interactive), 1)
        self.assertEqual(interactive[0].type, "list")
        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.active_scenario is not None
        self.assertEqual(
            session.scenarios[session.active_scenario].id,
            baseline.id,
        )

    async def test_blank_marketplace_price_is_flagged_and_excluded(self) -> None:
        catalog = await self.provider.get()
        item = next(
            row
            for row in catalog.items
            if row.marketplace_price == 0
            and row.term_duration == "P1Y"
            and row.billing_plan == "Annual"
            and (row.segment or "").casefold() == "commercial"
        )
        estate = LicenseEstate(
            id="zero-price",
            thread_id="zero-price",
            source_file="synthetic.csv",
            status=EstateStatus.READY,
            rate_card_version=catalog.version,
            lines=[
                NormalizedLicenseLine(
                    line_id="L1",
                    row_number=2,
                    source_product_title=item.sku_title,
                    product_id=item.product_id,
                    sku_id=item.sku_id,
                    sku_title=item.sku_title,
                    total_licenses=10,
                    expired_licenses=0,
                    assigned_licenses=0,
                    renewal_quantity=10,
                    term_duration="P1Y",
                    billing_plan="Annual",
                    match_confidence=100,
                    match_method="exact",
                )
            ],
        )
        scenario = ScenarioEngine(
            apply_bundle_rules=False,
            price_basis="marketplace",
        ).build(
            estate=estate,
            scenario_type=ScenarioType.RENEW_AS_IS,
            catalog=catalog,
            term_duration="P1Y",
            billing_plan="Annual",
            segment="Commercial",
        )

        self.assertTrue(scenario.lines[0].price_unavailable)
        self.assertEqual(scenario.total_value, Decimal("0.00"))
        self.assertIn("current licence price", scenario.unresolved_decisions[0])

    async def test_help_is_professional_natural_language_first_and_hides_internal_sources(
        self,
    ) -> None:
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = WhatsAppWebhookService(
            client,  # type: ignore[arg-type]
            self.orchestrator,
            ServiceConfiguration(
                frozenset(),
                10 * 1024 * 1024,
                workflow_mode="simple_pricing",
            ),
        )

        await service._handle_text("help-seller", "/help")
        await service._handle_text("help-seller", "Hi")
        await service._handle_text("help-seller", "What you do?")

        self.assertIn("SkySecure Microsoft Licensing Advisor", HELP_TEXT)
        self.assertIn("No commands are required", HELP_TEXT)
        self.assertIn("voice note", HELP_TEXT)
        self.assertIn("seller confirmation", HELP_TEXT)
        self.assertIn("customer-ready pdfs", HELP_TEXT.lower())
        self.assertNotIn("You can speak naturally", HELP_TEXT)
        self.assertNotIn("Change L2", HELP_TEXT)
        self.assertNotIn("Price on Marketplace", HELP_TEXT)
        self.assertNotIn("pricebook", HELP_TEXT.casefold())
        self.assertEqual(len(client.messages), 3)
        self.assertEqual(client.messages[0].text.body, client.messages[1].text.body)  # type: ignore[attr-defined]
        self.assertEqual(client.messages[0].text.body, client.messages[2].text.body)  # type: ignore[attr-defined]

    async def test_preupload_capability_question_is_not_treated_as_requirement_data(
        self,
    ) -> None:
        class HelpInterpreter:
            async def interpret(self, *_: object) -> object:
                return SimpleNamespace(action="help")

            async def close(self) -> None:
                return None

        extractor = FakeRequirementExtractor()
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = WhatsAppWebhookService(
            client,  # type: ignore[arg-type]
            self.orchestrator,
            ServiceConfiguration(
                frozenset(),
                10 * 1024 * 1024,
                allow_all_sellers=True,
                workflow_mode="simple_pricing",
            ),
            intent_interpreter=HelpInterpreter(),  # type: ignore[arg-type]
            requirement_extractor=extractor,
        )

        await service._handle_text("new-seller", "What can this agent do?")

        self.assertEqual(extractor.calls, [])
        self.assertEqual(client.messages[-1].text.body, HELP_TEXT)  # type: ignore[attr-defined]

    async def test_specific_question_is_answered_and_sku_text_starts_capture(self) -> None:
        class MutableInterpreter:
            action = "answer_question"

            async def interpret(self, *_: object) -> object:
                if self.action == "capture_requirement":
                    return SimpleNamespace(action="capture_requirement")
                return SimpleNamespace(
                    action="answer_question",
                    response_text=(
                        "Yes. Send the PDF here, and I will extract the licensing "
                        "requirement for your confirmation."
                    ),
                )

            async def close(self) -> None:
                return None

        interpreter = MutableInterpreter()
        extractor = FakeRequirementExtractor()
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = WhatsAppWebhookService(
            client,  # type: ignore[arg-type]
            self.orchestrator,
            ServiceConfiguration(
                frozenset(),
                10 * 1024 * 1024,
                allow_all_sellers=True,
                workflow_mode="simple_pricing",
            ),
            intent_interpreter=interpreter,  # type: ignore[arg-type]
            requirement_extractor=extractor,
        )

        await service._handle_text("question-first", "Can I upload a PDF?")

        self.assertEqual(extractor.calls, [])
        self.assertEqual(
            client.messages[-1].text.body,  # type: ignore[attr-defined]
            "Yes. Send the PDF here, and I will extract the licensing requirement for "
            "your confirmation.",
        )

        interpreter.action = "capture_requirement"
        await service._handle_text(
            "requirement-first",
            "25 Power BI Pro licences for one year",
        )

        self.assertEqual(extractor.calls, ["text"])
        session = await self.orchestrator.get_session("requirement-first")
        assert session is not None
        self.assertEqual(session.stage, WorkflowStage.AWAITING_INITIAL_VALIDATION)

    async def test_sequential_text_lines_accumulate_until_full_list_confirmation(
        self,
    ) -> None:
        class CaptureInterpreter:
            async def interpret(self, *_: object) -> object:
                return SimpleNamespace(action="capture_requirement")

            async def close(self) -> None:
                return None

        extractor = FakeRequirementExtractor()
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = WhatsAppWebhookService(
            client,  # type: ignore[arg-type]
            self.orchestrator,
            ServiceConfiguration(
                frozenset(),
                10 * 1024 * 1024,
                allow_all_sellers=True,
                workflow_mode="simple_pricing",
            ),
            intent_interpreter=CaptureInterpreter(),  # type: ignore[arg-type]
            requirement_extractor=extractor,
        )

        await service._handle_text(
            "sequential-seller",
            "25 Power BI Pro licences",
        )
        extractor.capture = CapturedRequirement(
            extraction=RequirementExtraction(
                lines=[
                    ExtractedRequirementLine(
                        sku_name="Microsoft 365 E3",
                        quantity=10,
                        term_duration="P1Y",
                        billing_plan="Annual",
                        product_id="",
                        sku_id="",
                        expiration_date="",
                        renewal_date="",
                    )
                ],
                warnings=[],
                needs_clarification=False,
                clarification="",
            )
        )

        await service._handle_text(
            "sequential-seller",
            "Also 10 Microsoft 365 E3 licences",
        )

        session = await self.orchestrator.get_session("sequential-seller")
        assert session is not None and session.estate is not None
        self.assertEqual(session.stage, WorkflowStage.AWAITING_INITIAL_VALIDATION)
        self.assertEqual(
            [(line.display_title, line.renewal_quantity) for line in session.estate.lines],
            [("Power BI Pro", 25), ("Microsoft 365 E3", 10)],
        )
        self.assertEqual(session.scenarios, {})
        self.assertIsNone(session.confirmed_as_is)
        self.assertFalse(
            any(item["filename"] == "as-is-commercial.pdf" for item in client.documents)
        )
        text_bodies = [
            message.text.body  # type: ignore[attr-defined]
            for message in client.messages
            if getattr(message, "text", None) is not None
        ]
        self.assertTrue(any("pricing remains paused" in body for body in text_bodies))
        self.assertFalse(
            any(getattr(message, "interactive", None) is not None for message in client.messages)
        )
        self.assertTrue(
            any("confirm that I should calculate" in body for body in text_bodies)
        )

        await service._handle_text("sequential-seller", "/validate")
        session = await self.orchestrator.get_session("sequential-seller")
        assert session is not None
        self.assertIsNotNone(session.confirmed_as_is)

    async def test_second_spreadsheet_appends_to_the_unconfirmed_draft(self) -> None:
        first = b"Product Title,Total Licenses,Expired Licenses,Assigned licenses\nPower BI Pro,5,0,0\n"
        second = b"Product Title,Total Licenses,Expired Licenses,Assigned licenses\nMicrosoft 365 E3,7,0,0\n"
        client = FakeWhatsAppClient(WhatsAppMedia(first, "first.csv", "text/csv"))
        service = WhatsAppWebhookService(
            client,  # type: ignore[arg-type]
            self.orchestrator,
            ServiceConfiguration(
                frozenset(),
                10 * 1024 * 1024,
                allow_all_sellers=True,
                workflow_mode="simple_pricing",
            ),
        )

        await service.handle(
            _webhook(
                {
                    "id": "first-sheet",
                    "from": "sheet-seller",
                    "type": "document",
                    "document": {
                        "id": "first-media",
                        "filename": "first.csv",
                        "mime_type": "text/csv",
                    },
                }
            )
        )
        client.media = WhatsAppMedia(second, "second.csv", "text/csv")
        await service.handle(
            _webhook(
                {
                    "id": "second-sheet",
                    "from": "sheet-seller",
                    "type": "document",
                    "document": {
                        "id": "second-media",
                        "filename": "second.csv",
                        "mime_type": "text/csv",
                    },
                }
            )
        )

        session = await self.orchestrator.get_session("sheet-seller")
        assert session is not None and session.estate is not None
        self.assertEqual(
            [(line.display_title, line.renewal_quantity) for line in session.estate.lines],
            [("Power BI Pro", 5), ("Microsoft 365 E3", 7)],
        )
        self.assertEqual(session.stage, WorkflowStage.AWAITING_INITIAL_VALIDATION)
        self.assertIsNone(session.confirmed_as_is)

    async def test_explicit_enterprise_comparison_remains_available_on_request(self) -> None:
        sender = "enterprise-comparison-seller"
        await self.orchestrator.analyze_document(
            sender=sender,
            filename=CUSTOMER.name,
            content=CUSTOMER.read_bytes(),
        )
        await self.orchestrator.request_requirement_validation(sender)
        await self.orchestrator.confirm_requirement(sender)
        renew = await self.orchestrator.build_scenario(
            sender,
            ScenarioType.RENEW_AS_IS,
            promo_eligible=False,
        )
        await self.orchestrator.save_confirmed_as_is(sender, renew)
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = WhatsAppWebhookService(
            client,  # type: ignore[arg-type]
            self.orchestrator,
            ServiceConfiguration(
                frozenset(),
                10 * 1024 * 1024,
                allow_all_sellers=True,
                workflow_mode="simple_pricing",
            ),
        )

        await service._send_enterprise_comparison(sender)

        session = await self.orchestrator.get_session(sender)
        assert session is not None
        self.assertEqual(set(session.scenarios), set(ScenarioType))
        self.assertTrue(
            any(
                item["filename"] == "annual-licensing-comparison.pdf"
                for item in client.documents
            )
        )
        comparison_message = next(
            message.text.body  # type: ignore[attr-defined]
            for message in client.messages
            if getattr(message, "text", None) is not None
            and "Seller-requested enterprise comparison" in message.text.body  # type: ignore[attr-defined]
        )
        self.assertIn("no feature, entitlement, migration", comparison_message)

    async def test_edit_after_four_way_comparison_requires_and_honors_scenario_target(
        self,
    ) -> None:
        sender = "scenario-target-seller"
        await self.orchestrator.analyze_document(
            sender=sender,
            filename=CUSTOMER.name,
            content=CUSTOMER.read_bytes(),
        )
        await self.orchestrator.request_requirement_validation(sender)
        await self.orchestrator.confirm_requirement(sender)
        renew = await self.orchestrator.build_scenario(
            sender,
            ScenarioType.RENEW_AS_IS,
            promo_eligible=False,
        )
        await self.orchestrator.save_confirmed_as_is(sender, renew)
        await self.orchestrator.comparison(sender)
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = WhatsAppWebhookService(
            client,  # type: ignore[arg-type]
            self.orchestrator,
            ServiceConfiguration(
                frozenset(),
                10 * 1024 * 1024,
                allow_all_sellers=True,
                workflow_mode="simple_pricing",
            ),
        )
        intent = SimpleNamespace(
            action="set_disposition",
            scenario="none",
            line_id="L2",
            disposition="remove",
        )

        await service._execute_agent_intent(
            sender,
            intent,  # type: ignore[arg-type]
            original_message="Delete the Power BI licence within that",
        )

        session = await self.orchestrator.get_session(sender)
        assert session is not None
        self.assertIsNotNone(session.pending_dialogue)
        self.assertIn("Which proposal should I update", client.messages[-1].text.body)  # type: ignore[attr-defined]
        self.assertEqual(
            session.scenarios[ScenarioType.RENEW_AS_IS].revision,
            renew.revision,
        )

        targeted_intent = SimpleNamespace(
            action="set_disposition",
            scenario="me5_copilot",
            line_id="L2",
            disposition="remove",
        )
        await self.orchestrator.clear_pending_dialogue(sender)
        await service._execute_agent_intent(
            sender,
            targeted_intent,  # type: ignore[arg-type]
            original_message="Remove L2 from ME5",
        )

        session = await self.orchestrator.get_session(sender)
        assert session is not None
        self.assertEqual(session.active_scenario, ScenarioType.ME5_COPILOT)
        self.assertGreater(session.scenarios[ScenarioType.ME5_COPILOT].revision, 1)
        self.assertEqual(
            session.scenarios[ScenarioType.RENEW_AS_IS].revision,
            renew.revision,
        )

    async def test_combined_replacement_cancel_does_not_emit_zero_difference_comparison(
        self,
    ) -> None:
        sender = "cancel-and-compare-seller"
        await self.orchestrator.analyze_document(
            sender=sender,
            filename=CUSTOMER.name,
            content=CUSTOMER.read_bytes(),
        )
        await self.orchestrator.request_requirement_validation(sender)
        await self.orchestrator.confirm_requirement(sender)
        renew = await self.orchestrator.build_scenario(
            sender,
            ScenarioType.RENEW_AS_IS,
            promo_eligible=False,
        )
        await self.orchestrator.save_confirmed_as_is(sender, renew)
        pending = await self.orchestrator.replace_sku(sender, "L1", "E5", 10)
        self.assertEqual(pending.state, "confirmation_required")
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = WhatsAppWebhookService(
            client,  # type: ignore[arg-type]
            self.orchestrator,
            ServiceConfiguration(
                frozenset(),
                10 * 1024 * 1024,
                allow_all_sellers=True,
                workflow_mode="simple_pricing",
            ),
        )

        await service._handle_text(
            sender,
            "Wait, let's not replace it; compare with another proposal",
        )

        session = await self.orchestrator.get_session(sender)
        assert session is not None
        self.assertIsNone(session.pending_sku_change)
        self.assertIsNotNone(session.pending_dialogue)
        self.assertIn("Which options should I compare", client.messages[-1].text.body)  # type: ignore[attr-defined]
        self.assertFalse(client.documents)

    async def test_recommendation_line_question_is_persisted_for_short_reply(self) -> None:
        sender = "recommendation-context-seller"
        await self.orchestrator.analyze_document(
            sender=sender,
            filename=CUSTOMER.name,
            content=CUSTOMER.read_bytes(),
        )
        await self.orchestrator.request_requirement_validation(sender)
        await self.orchestrator.confirm_requirement(sender)
        renew = await self.orchestrator.build_scenario(
            sender,
            ScenarioType.RENEW_AS_IS,
            promo_eligible=False,
        )
        await self.orchestrator.save_confirmed_as_is(sender, renew)
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = WhatsAppWebhookService(
            client,  # type: ignore[arg-type]
            self.orchestrator,
            ServiceConfiguration(
                frozenset(),
                10 * 1024 * 1024,
                allow_all_sellers=True,
                workflow_mode="simple_pricing",
            ),
        )

        await service._execute_agent_intent(
            sender,
            SimpleNamespace(
                action="request_recommendation",
                line_id="",
                quantity=-1,
            ),  # type: ignore[arg-type]
            original_message="Can I upgrade to ME5 or ME7?",
        )

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.pending_dialogue is not None
        self.assertEqual(
            session.pending_dialogue.context_message,
            "Can I upgrade to ME5 or ME7?",
        )
        self.assertIn("Which current line should I evaluate", client.messages[-1].text.body)  # type: ignore[attr-defined]

    async def test_out_of_scope_request_receives_a_professional_boundary(self) -> None:
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = WhatsAppWebhookService(
            client,  # type: ignore[arg-type]
            self.orchestrator,
            ServiceConfiguration(
                frozenset(),
                10 * 1024 * 1024,
                allow_all_sellers=True,
                workflow_mode="simple_pricing",
            ),
        )

        await service._execute_agent_intent(  # type: ignore[arg-type]
            "scope-seller",
            SimpleNamespace(action="out_of_scope", response_text=""),
        )

        response = client.messages[-1].text.body  # type: ignore[attr-defined]
        self.assertIn("outside this licensing advisor", response)
        self.assertIn("Microsoft licensing requirements", response)

    async def test_questions_are_direct_and_monthly_or_restricted_pricing_is_not_applied(
        self,
    ) -> None:
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = WhatsAppWebhookService(
            client,  # type: ignore[arg-type]
            self.orchestrator,
            ServiceConfiguration(
                frozenset(),
                10 * 1024 * 1024,
                workflow_mode="simple_pricing",
            ),
        )

        question = "Would you like to confirm the ME5 proposal or make another change?"
        await service._execute_agent_intent(  # type: ignore[arg-type]
            "question-seller",
            SimpleNamespace(action="clarify", clarification=question),
        )
        self.assertEqual(client.messages[-1].text.body, question)  # type: ignore[attr-defined]

        await service._handle_text("question-seller", "Change the billing plan to monthly")
        monthly = client.messages[-1].text.body  # type: ignore[attr-defined]
        self.assertIn("Monthly billing is not available", monthly)
        self.assertIn("annual billing", monthly)

        await service._handle_text(
            "question-seller",
            "Apply the partner best price and promotion",
        )
        restricted = client.messages[-1].text.body  # type: ignore[attr-defined]
        self.assertIn("not applied", restricted)
        self.assertIn("remains unchanged", restricted)

        await service._handle_text(
            "question-seller",
            "Can you add a discount or any adjustments?",
        )
        commercial_control = client.messages[-1].text.body  # type: ignore[attr-defined]
        self.assertIn("not seller-editable", commercial_control)
        self.assertIn("remains unchanged", commercial_control)
        self.assertNotIn("What discount percentage", commercial_control)

        await service._execute_agent_intent(  # type: ignore[arg-type]
            "language-seller",
            SimpleNamespace(
                action="clarify",
                clarification="Which proposal should I review? கர்",
            ),
        )
        sanitized = client.messages[-1].text.body  # type: ignore[attr-defined]
        self.assertEqual(sanitized, "Which proposal should I review?")

    async def test_final_validation_labels_an_edited_baseline_as_revised(self) -> None:
        sender = "revised-final-label-seller"
        await self.orchestrator.analyze_document(
            sender=sender,
            filename=CUSTOMER.name,
            content=CUSTOMER.read_bytes(),
        )
        await self.orchestrator.request_requirement_validation(sender)
        await self.orchestrator.confirm_requirement(sender)
        baseline = await self.orchestrator.build_scenario(
            sender,
            ScenarioType.RENEW_AS_IS,
            promo_eligible=False,
        )
        await self.orchestrator.save_confirmed_as_is(sender, baseline)
        changed = await self.orchestrator.edit_quantity(sender, "L1", 50)
        self.assertGreater(changed.revision, baseline.revision)
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = WhatsAppWebhookService(
            client,  # type: ignore[arg-type]
            self.orchestrator,
            ServiceConfiguration(
                frozenset(),
                10 * 1024 * 1024,
                allow_all_sellers=True,
                workflow_mode="simple_pricing",
            ),
        )

        await service._request_finalization(sender)

        prompt = client.messages[-1].text.body  # type: ignore[attr-defined]
        self.assertIn("Proposal: Revised annual configuration", prompt)
        self.assertNotIn("Proposal: Renew As-Is\n", prompt)

        await service._confirm_finalization_and_send(sender)
        finalized_message = next(
            message.text.body  # type: ignore[attr-defined]
            for message in client.messages
            if getattr(message, "text", None) is not None
            and "Finalized proposal:" in message.text.body  # type: ignore[attr-defined]
        )
        self.assertIn("Finalized proposal: Revised annual configuration", finalized_message)

    async def test_finalization_does_not_relabel_unchanged_renew_as_is(self) -> None:
        sender = "unchanged-final-label-seller"
        await self.orchestrator.analyze_document(
            sender=sender,
            filename=CUSTOMER.name,
            content=CUSTOMER.read_bytes(),
        )
        await self.orchestrator.request_requirement_validation(sender)
        await self.orchestrator.confirm_requirement(sender)
        baseline = await self.orchestrator.build_scenario(
            sender,
            ScenarioType.RENEW_AS_IS,
            promo_eligible=False,
        )
        await self.orchestrator.save_confirmed_as_is(sender, baseline)
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = WhatsAppWebhookService(
            client,  # type: ignore[arg-type]
            self.orchestrator,
            ServiceConfiguration(
                frozenset(),
                10 * 1024 * 1024,
                allow_all_sellers=True,
                workflow_mode="simple_pricing",
            ),
        )

        await service._request_finalization(sender)
        self.assertIn("Proposal: Renew As-Is", client.messages[-1].text.body)  # type: ignore[attr-defined]
        await service._confirm_finalization_and_send(sender)
        finalized_message = next(
            message.text.body  # type: ignore[attr-defined]
            for message in client.messages
            if getattr(message, "text", None) is not None
            and "Finalized proposal:" in message.text.body  # type: ignore[attr-defined]
        )
        self.assertIn("Finalized proposal: Renew As-Is", finalized_message)
        self.assertNotIn("Revised annual configuration", finalized_message)

    async def test_four_option_comparison_preserves_each_saved_proposal_revision(
        self,
    ) -> None:
        sender = "revision-seller"
        await self.orchestrator.analyze_document(
            sender=sender,
            filename=CUSTOMER.name,
            content=CUSTOMER.read_bytes(),
        )
        await self.orchestrator.request_requirement_validation(sender)
        await self.orchestrator.confirm_requirement(sender)
        renew = await self.orchestrator.build_scenario(
            sender,
            ScenarioType.RENEW_AS_IS,
            promo_eligible=False,
        )
        await self.orchestrator.save_confirmed_as_is(sender, renew)
        renew = await self.orchestrator.edit_quantity(sender, "L1", 50)

        me5 = await self.orchestrator.build_scenario(
            sender,
            ScenarioType.ME5_COPILOT,
            copilot_quantity=25,
        )
        me5 = await self.orchestrator.edit_quantity(sender, "COPILOT", 30)
        await self.orchestrator.build_scenario(sender, ScenarioType.RENEW_AS_IS)
        restored_me5 = await self.orchestrator.build_scenario(
            sender,
            ScenarioType.ME5_COPILOT,
        )

        self.assertEqual(restored_me5.id, me5.id)
        self.assertEqual(
            next(
                line.proposed_quantity
                for line in restored_me5.lines
                if line.line_id == "COPILOT"
            ),
            30,
        )
        _estate, scenarios, comparison = await self.orchestrator.comparison(sender)
        self.assertEqual([item.scenario_type for item in scenarios], list(ScenarioType))
        self.assertEqual(len(comparison.rows), 4)
        self.assertEqual(scenarios[0].id, renew.id)
        self.assertEqual(scenarios[2].id, restored_me5.id)
        self.assertGreater(scenarios[0].revision, 1)
        self.assertGreater(scenarios[2].revision, 1)
        self.assertEqual(scenarios[1].revision, 1)
        self.assertEqual(scenarios[3].revision, 1)

    async def test_recommendation_prompt_uses_natural_language_without_buttons(
        self,
    ) -> None:
        normal_client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        normal_service = WhatsAppWebhookService(
            normal_client,  # type: ignore[arg-type]
            self.orchestrator,
            ServiceConfiguration(
                frozenset(),
                10 * 1024 * 1024,
                workflow_mode="simple_pricing",
            ),
        )
        await normal_service._send_recommendation_prompt("prompt-seller")
        prompt = normal_client.messages[0].text.body  # type: ignore[attr-defined]
        self.assertIn("Would you like me to evaluate", prompt)
        self.assertIn("Describe the business need", prompt)
        self.assertFalse(
            any(
                getattr(message, "interactive", None) is not None
                for message in normal_client.messages
            )
        )

    async def test_manual_image_audio_and_word_capture_route_to_one_schema(self) -> None:
        extractor = FakeRequirementExtractor()
        client = FakeWhatsAppClient(
            WhatsAppMedia(b"synthetic", "requirement.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        )
        service = WhatsAppWebhookService(
            client,  # type: ignore[arg-type]
            self.orchestrator,
            ServiceConfiguration(
                frozenset(),
                10 * 1024 * 1024,
                allow_all_sellers=True,
                workflow_mode="simple_pricing",
            ),
            requirement_extractor=extractor,
        )

        await service._handle_text("text-seller", "25 Power BI Pro licences, annual")
        text_session = await self.orchestrator.get_session("text-seller")
        assert text_session is not None and text_session.estate is not None
        self.assertEqual(text_session.estate.lines[0].renewal_quantity, 25)
        self.assertEqual(text_session.stage, WorkflowStage.AWAITING_INITIAL_VALIDATION)

        await service.handle(
            _webhook(
                {
                    "id": "word-upload",
                    "from": "word-seller",
                    "type": "document",
                    "document": {
                        "id": "word-media",
                        "filename": "requirement.docx",
                        "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    },
                }
            )
        )
        client.media = WhatsAppMedia(b"image", "image.png", "image/png")
        await service.handle(
            _webhook(
                {
                    "id": "image-upload",
                    "from": "image-seller",
                    "type": "image",
                    "image": {"id": "image-media", "mime_type": "image/png"},
                }
            )
        )
        client.media = WhatsAppMedia(b"audio", "voice.ogg", "audio/ogg; codecs=opus")
        await service.handle(
            _webhook(
                {
                    "id": "audio-upload",
                    "from": "audio-seller",
                    "type": "audio",
                    "audio": {
                        "id": "audio-media",
                        "mime_type": "audio/ogg; codecs=opus",
                        "voice": True,
                    },
                }
            )
        )

        self.assertEqual(extractor.calls, ["text", "file", "image", "audio"])
        for sender in ("word-seller", "image-seller", "audio-seller"):
            session = await self.orchestrator.get_session(sender)
            assert session is not None
            self.assertEqual(session.stage, WorkflowStage.AWAITING_INITIAL_VALIDATION)


if __name__ == "__main__":
    unittest.main()
