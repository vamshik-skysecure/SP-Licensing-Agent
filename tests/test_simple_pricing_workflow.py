import unittest
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import wave

from pypdf import PdfReader

from app.api.whatsapp.service import HELP_TEXT, ServiceConfiguration, WhatsAppWebhookService
from app.core.licensing.analysis import LicenseAnalyzer
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
    ScenarioType,
    WorkflowStage,
)
from app.core.licensing.orchestrator import LicensingOrchestrator
from app.core.licensing.rate_card import LocalRateCardSource, RateCardProvider
from app.core.licensing.renderer import render_proposal_pdf
from app.core.licensing.scenarios import ScenarioEngine
from app.core.licensing.store import InMemoryWorkflowStore
from app.core.whatsapp import WhatsAppAPIError, WhatsAppMedia
from app.schema.whatsapp import InteractiveFooter, WhatsAppWebhookPayload


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


class RejectingInteractiveClient(FakeWhatsAppClient):
    async def send_message(self, message: object) -> dict[str, object]:
        if getattr(message, "type", None) == "interactive":
            raise WhatsAppAPIError(
                "Meta rejected the interactive message.",
                status_code=400,
            )
        return await super().send_message(message)


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
        self.assertIn("annual-licensing-comparison.pdf", filenames)
        self.assertTrue(any(name.startswith("as-is-cost-") for name in [item["filename"] for item in client.images]))
        self.assertTrue(any(name.startswith("annual-comparison-table-") for name in [item["filename"] for item in client.images]))
        final_pdf = next(
            item["content"]
            for item in client.documents
            if item["filename"] == "annual-licensing-comparison.pdf"
        )
        reader = PdfReader(BytesIO(final_pdf))  # type: ignore[arg-type]
        pdf_text = "\n".join(page.extract_text() or "" for page in reader.pages)
        for required in (
            "Proposal",
            "Renew As-Is",
            "ME3",
            "ME5",
            "ME7",
            "Licence term",
            "Unit price",
            "Overall annual value",
            "Difference vs Renew As-Is",
            "Replaced by seller with Power BI Pro",
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

        self.assertIn("SkySecure Microsoft Licensing Advisor", HELP_TEXT)
        self.assertIn("No commands are required", HELP_TEXT)
        self.assertIn("voice note", HELP_TEXT)
        self.assertIn("seller confirmation", HELP_TEXT)
        self.assertIn("customer-ready PDFs", HELP_TEXT)
        self.assertNotIn("You can speak naturally", HELP_TEXT)
        self.assertNotIn("Change L2", HELP_TEXT)
        self.assertNotIn("Price on Marketplace", HELP_TEXT)
        self.assertNotIn("pricebook", HELP_TEXT.casefold())
        self.assertEqual(len(client.messages), 1)

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

    async def test_recommendation_prompt_has_no_oversized_footer_and_falls_back_to_text(
        self,
    ) -> None:
        with self.assertRaises(ValueError):
            InteractiveFooter(text="x" * 61)

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
        interactive = normal_client.messages[0].interactive  # type: ignore[attr-defined]
        self.assertIsNone(interactive.footer)
        self.assertEqual(len(interactive.action.buttons), 3)
        await normal_service._handle_interactive(
            "prompt-seller", "licensing|recommend|yes"
        )
        revision_prompt = normal_client.messages[-1].text.body  # type: ignore[attr-defined]
        self.assertIn("Please let me know which changes", revision_prompt)
        self.assertIn("recalculated annual value", revision_prompt)
        self.assertNotIn("Tell me", revision_prompt)
        self.assertNotIn("for example", revision_prompt)

        rejecting_client = RejectingInteractiveClient(
            WhatsAppMedia(b"", "unused", "text/plain")
        )
        fallback_service = WhatsAppWebhookService(
            rejecting_client,  # type: ignore[arg-type]
            self.orchestrator,
            ServiceConfiguration(
                frozenset(),
                10 * 1024 * 1024,
                workflow_mode="simple_pricing",
            ),
        )
        await fallback_service._send_recommendation_prompt("fallback-seller")
        fallback = rejecting_client.messages[0].text.body  # type: ignore[attr-defined]
        self.assertIn("Would you like me to evaluate", fallback)
        self.assertNotIn("safe to retry", fallback)

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
