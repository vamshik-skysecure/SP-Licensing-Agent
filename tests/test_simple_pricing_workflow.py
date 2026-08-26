import re
import unittest
from unittest.mock import AsyncMock, patch
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import wave

from pypdf import PdfReader

from app.api.whatsapp.service import HELP_TEXT, ServiceConfiguration, WhatsAppWebhookService
from app.core.licensing.analysis import LicenseAnalyzer
from app.core.licensing.agent import (
    AgentIntent,
    OfficialProductAnswer,
    OfficialRecommendation,
    OpenAIIntentInterpreter,
)
from app.core.licensing.capture import (
    CapturedRequirement,
    ExtractedRequirementLine,
    RequirementCaptureError,
    RequirementExtraction,
    OpenAIRequirementExtractor,
    _audio_duration_seconds,
    _prepare_audio,
)
from app.core.licensing.models import (
    EstateStatus,
    LicenseEstate,
    NormalizedLicenseLine,
    PendingDialogue,
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
from app.core.licensing.scenarios import ScenarioEngine, ScenarioError
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


class SingleTurnRequirementExtractor:
    """Deterministic extractor for a complete correction made during a pending choice."""

    def __init__(self, product: str, quantity: int) -> None:
        self.product = product
        self.quantity = quantity
        self.inputs: list[str] = []

    async def extract_text(self, text: str, **_: object) -> CapturedRequirement:
        self.inputs.append(text)
        return CapturedRequirement(
            extraction=RequirementExtraction(
                lines=[
                    ExtractedRequirementLine(
                        sku_name=self.product,
                        quantity=self.quantity,
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

    async def answer_product_question(self, **kwargs: object) -> OfficialProductAnswer:
        self.calls.append(kwargs)
        return OfficialProductAnswer(
            answer=(
                "Microsoft Teams is included in Microsoft 365 E3, subject to the "
                "applicable regional suite offering and tenant configuration."
            ),
            clarification_question="",
            table_title="",
            table_headers=[],
            table_rows=[],
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


def _agent_intent(action: str, **updates: object) -> AgentIntent:
    """Build a complete structured intent for state-machine regression tests."""

    values: dict[str, object] = {
        "action": action,
        "scenario": "none",
        "line_id": "",
        "quantity": -1,
        "copilot_quantity": -1,
        "product_query": "",
        "disposition": "none",
        "boolean_value": "none",
        "percentage": -1.0,
        "amount": -1.0,
        "term_duration": "",
        "billing_plan": "",
        "segment": "",
        "currency": "",
        "candidate_number": -1,
        "match_selections": [],
        "comment": "",
        "detail_label": "",
        "detail_value": "",
        "response_text": "",
        "clarification": "",
    }
    values.update(updates)
    return AgentIntent.model_validate(values)


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

    async def test_model_authored_text_cannot_expose_urls_or_internal_sources(self) -> None:
        safe = WhatsAppWebhookService._professional_agent_text(
            "Review the official guidance at https://example.com/licensing."
        )
        blocked = WhatsAppWebhookService._professional_agent_text(
            "I selected that from the pricing workbook using OpenAI."
        )
        bidi_safe = WhatsAppWebhookService._professional_agent_text(
            "Confirmed \u202ereversed"
        )

        self.assertEqual(safe, "Review the official guidance at")
        self.assertEqual(blocked, "")
        self.assertEqual(bidi_safe, "Confirmed reversed")

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

    async def test_unexpected_retry_notifies_seller_only_once(self) -> None:
        class FailingService(WhatsAppWebhookService):
            async def _handle_text(self, *_: object) -> None:
                raise RuntimeError("synthetic unexpected failure")

        sender = "failure-notify-seller"
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = FailingService(
            client,  # type: ignore[arg-type]
            self.orchestrator,
            ServiceConfiguration(
                frozenset(),
                10 * 1024 * 1024,
                allow_all_sellers=True,
                workflow_mode="simple_pricing",
            ),
        )
        payload = _webhook(
            {
                "id": "unexpected-retry-once",
                "from": sender,
                "type": "text",
                "text": {"body": "Change the proposal"},
            }
        )

        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, "synthetic unexpected failure"):
                await service.handle(payload)

        self.assertEqual(len(client.messages), 1)
        self.assertIn(
            "please do not resend the same message",
            client.messages[0].text.body,  # type: ignore[attr-defined]
        )
        session = await self.orchestrator.get_session(sender)
        assert session is not None
        self.assertEqual(len(session.failure_notified_message_ids), 1)

    async def test_five_minute_inactivity_starts_a_clean_requirement(self) -> None:
        sender = "expired-session-seller"
        await self.orchestrator.analyze_document(
            sender=sender,
            filename=CUSTOMER.name,
            content=CUSTOMER.read_bytes(),
        )
        thread_id = self.orchestrator.thread_id(sender)
        session, version = await self.store.get(thread_id)
        assert session is not None and version is not None
        expired = session.model_copy(
            update={"updated_at": datetime.now(UTC) - timedelta(minutes=6)}
        )
        await self.store.save(expired, version)
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

        await service.handle(
            _webhook(
                {
                    "id": "message-after-expiry",
                    "from": sender,
                    "type": "text",
                    "text": {"body": "Hi"},
                }
            )
        )

        fresh = await self.orchestrator.get_session(sender)
        assert fresh is not None
        self.assertIsNone(fresh.estate)
        self.assertEqual(fresh.scenarios, {})
        self.assertIsNone(fresh.confirmed_as_is)
        bodies = [
            message.text.body  # type: ignore[attr-defined]
            for message in client.messages
            if getattr(message, "text", None) is not None
        ]
        self.assertIn("expired after five minutes", bodies[0])
        self.assertTrue(any("Send the first requirement" in body for body in bodies))

    async def test_complete_requirement_accepts_swathi_confirmation_phrases(
        self,
    ) -> None:
        class InterpreterThatMustNotBeCalled:
            def __init__(self) -> None:
                self.calls = 0

            async def interpret(self, *_: object, **__: object) -> object:
                self.calls += 1
                return SimpleNamespace(
                    action="clarify",
                    clarification="Please confirm whether the captured requirement is correct.",
                )

            async def close(self) -> None:
                return None

        for index, reply in enumerate(
            ("Yes", "Confirm", "yes confirm for pricing", "yes it is correct"),
            start=1,
        ):
            with self.subTest(reply=reply):
                sender = f"swathi-confirm-{index}"
                await self.orchestrator.analyze_document(
                    sender=sender,
                    filename=CUSTOMER.name,
                    content=CUSTOMER.read_bytes(),
                )
                await self.orchestrator.request_requirement_validation(sender)
                interpreter = InterpreterThatMustNotBeCalled()
                client = FakeWhatsAppClient(
                    WhatsAppMedia(b"", "unused", "text/plain")
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
                    intent_interpreter=interpreter,  # type: ignore[arg-type]
                )

                await service._handle_text(sender, reply)

                session = await self.orchestrator.get_session(sender)
                assert session is not None
                self.assertIsNotNone(session.confirmed_as_is)
                self.assertEqual(interpreter.calls, 0)
                self.assertTrue(
                    any(
                        item["filename"] == "as-is-commercial.pdf"
                        for item in client.documents
                    )
                )

    async def test_existing_requirement_confirmation_dialogue_recovers(self) -> None:
        sender = "swathi-stuck-confirmation-recovery"
        await self.orchestrator.analyze_document(
            sender=sender,
            filename=CUSTOMER.name,
            content=CUSTOMER.read_bytes(),
        )
        await self.orchestrator.request_requirement_validation(sender)
        await self.orchestrator.set_pending_dialogue(
            sender,
            PendingDialogue(
                kind="agent_clarification",
                question=(
                    "Please confirm whether the captured requirement for 1 Power BI "
                    "Premium Per User licence is correct."
                ),
            ),
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

        await service._handle_text(sender, "yes confirm for pricing")

        session = await self.orchestrator.get_session(sender)
        assert session is not None
        self.assertIsNone(session.pending_dialogue)
        self.assertIsNotNone(session.confirmed_as_is)

    async def test_yes_does_not_confirm_a_missing_detail_question(self) -> None:
        sender = "missing-detail-remains-protected"
        await self.orchestrator.analyze_document(
            sender=sender,
            filename=CUSTOMER.name,
            content=CUSTOMER.read_bytes(),
        )
        await self.orchestrator.request_requirement_validation(sender)
        await self.orchestrator.set_pending_dialogue(
            sender,
            PendingDialogue(
                kind="agent_clarification",
                question="How many Power BI Pro licences should I include?",
            ),
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

        await service._handle_text(sender, "Yes")

        session = await self.orchestrator.get_session(sender)
        assert session is not None
        self.assertIsNone(session.pending_dialogue)
        self.assertIsNone(session.confirmed_as_is)
        self.assertIn(
            "closed that prompt without changing the requirement",
            client.messages[-1].text.body,  # type: ignore[attr-defined]
        )

    async def test_explicit_confirm_supersedes_nonblocking_advisory_question(self) -> None:
        sender = "qa-explicit-confirm-recovery"
        await self.orchestrator.analyze_document(
            sender=sender,
            filename=CUSTOMER.name,
            content=CUSTOMER.read_bytes(),
        )
        await self.orchestrator.request_requirement_validation(sender)
        await self.orchestrator.set_pending_dialogue(
            sender,
            PendingDialogue(
                kind="agent_clarification",
                question="Which capability should the recommendation prioritize?",
                context_message="Which product is worth the money?",
            ),
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

        await service._handle_text(sender, "confirm")

        session = await self.orchestrator.get_session(sender)
        assert session is not None
        self.assertIsNone(session.pending_dialogue)
        self.assertIsNotNone(session.confirmed_as_is)

    async def test_remove_them_applies_all_pending_line_ids_atomically(self) -> None:
        sender = "qa-bulk-remove-lines"
        await self.orchestrator.analyze_document(
            sender=sender,
            filename=CUSTOMER.name,
            content=CUSTOMER.read_bytes(),
        )
        await self.orchestrator.request_requirement_validation(sender)
        await self.orchestrator.set_pending_dialogue(
            sender,
            PendingDialogue(
                kind="agent_clarification",
                question="What would you like to do with L1 and L5?",
                context_message="L1 and L5",
            ),
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

        await service._handle_text(sender, "REMOVE THEM")

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.estate is not None
        remaining = {line.line_id for line in session.estate.lines}
        self.assertNotIn("L1", remaining)
        self.assertNotIn("L5", remaining)
        self.assertEqual(len(remaining), 3)
        self.assertTrue(client.images)

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
        candidate_titles = [candidate.sku_title for candidate in line.candidates]
        self.assertGreater(len(candidate_titles), 3)
        self.assertEqual(
            candidate_titles[0],
            "Microsoft 365 E5 without Audio Conferencing",
        )
        self.assertIn("Office 365 E5 without Audio Conferencing", candidate_titles)
        self.assertIn("Enterprise Mobility + Security E5", candidate_titles)
        self.assertTrue(all("E5" in title for title in candidate_titles))
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

    async def test_quantity_only_reply_cannot_be_misrouted_to_line_edit(self) -> None:
        class IncorrectQuantityInterpreter:
            def __init__(self) -> None:
                self.calls: list[str] = []

            async def interpret(self, message: str, *_: object) -> object:
                self.calls.append(message)
                if message.strip().isdigit():
                    return SimpleNamespace(
                        action="set_quantity",
                        line_id="",
                        quantity=int(message),
                    )
                return SimpleNamespace(action="capture_requirement")

        sender = "defender-quantity-followup"
        extractor = ContextAwareFragmentExtractor(
            product="Microsoft Defender for Endpoint P2",
            quantity=10,
        )
        interpreter = IncorrectQuantityInterpreter()
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
            requirement_extractor=extractor,  # type: ignore[arg-type]
        )

        await service._handle_text(sender, "I want Defender Endpoint")
        await service._handle_text(sender, "10")

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.estate is not None
        self.assertEqual(session.capture_messages, [])
        self.assertEqual(session.estate.lines[0].renewal_quantity, 10)
        self.assertEqual(
            session.estate.lines[0].display_title,
            "Microsoft Defender for Endpoint P2",
        )
        self.assertNotIn("10", interpreter.calls)
        response = "\n".join(
            message.text.body  # type: ignore[attr-defined]
            for message in client.messages
            if getattr(message, "text", None) is not None
        )
        self.assertNotIn("Provide the line ID", response)
        for natural_reply in (
            "15 licences",
            "Maybe around 20",
            "Let's consider 45 licence",
            "I want 15 quantity",
        ):
            with self.subTest(reply=natural_reply):
                self.assertTrue(service._is_quantity_only_reply(natural_reply))

    async def test_missing_line_id_is_inferred_when_only_one_line_exists(self) -> None:
        sender = "single-line-edit"
        await self.orchestrator.analyze_extracted(
            sender=sender,
            source_file="seller-message",
            rows=[
                ParsedLicenseRow(
                    row_number=2,
                    product_title="Power BI Pro",
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

        await service._execute_agent_intent(
            sender,
            SimpleNamespace(
                action="set_quantity",
                line_id="",
                quantity=15,
            ),  # type: ignore[arg-type]
            original_message="Change the quantity to 15",
        )

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.estate is not None
        self.assertEqual(session.estate.lines[0].renewal_quantity, 15)
        response = "\n".join(
            message.text.body  # type: ignore[attr-defined]
            for message in client.messages
            if getattr(message, "text", None) is not None
        )
        self.assertNotIn("Provide the line ID", response)

    async def test_missing_line_id_prompts_with_real_choices_when_multiple_exist(
        self,
    ) -> None:
        sender = "multi-line-edit"
        await self.orchestrator.analyze_document(
            sender=sender,
            filename=CUSTOMER.name,
            content=CUSTOMER.read_bytes(),
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

        await service._execute_agent_intent(
            sender,
            SimpleNamespace(
                action="set_quantity",
                line_id="",
                quantity=15,
            ),  # type: ignore[arg-type]
            original_message="Change the quantity to 15",
        )

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.pending_dialogue is not None
        self.assertEqual(
            session.pending_dialogue.context_message,
            "Change the quantity to 15",
        )
        response = client.messages[-1].text.body  # type: ignore[attr-defined]
        self.assertIn("Which licence should I change?", response)
        self.assertIn("Reply with the product name", response)
        self.assertNotIn("L1 (", response)
        self.assertNotIn("Provide the line ID", response)

    async def test_product_name_resolves_line_without_seller_line_id(self) -> None:
        sender = "multi-line-product-name-edit"
        await self.orchestrator.analyze_extracted(
            sender=sender,
            source_file="seller-message",
            rows=[
                ParsedLicenseRow(
                    row_number=2,
                    product_title="Power BI Pro",
                    total_licenses=10,
                    expired_licenses=0,
                    assigned_licenses=0,
                    renewal_quantity=10,
                ),
                ParsedLicenseRow(
                    row_number=3,
                    product_title="Microsoft Defender for Endpoint P2",
                    total_licenses=20,
                    expired_licenses=0,
                    assigned_licenses=0,
                    renewal_quantity=20,
                ),
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

        await service._execute_agent_intent(
            sender,
            SimpleNamespace(
                action="set_quantity",
                line_id="",
                quantity=15,
            ),  # type: ignore[arg-type]
            original_message="Change Power BI Pro to 15 licences",
        )

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.estate is not None
        power_bi = next(
            line for line in session.estate.lines if line.display_title == "Power BI Pro"
        )
        self.assertEqual(power_bi.renewal_quantity, 15)
        self.assertIsNone(session.pending_dialogue)
        response = "\n".join(
            message.text.body  # type: ignore[attr-defined]
            for message in client.messages
            if getattr(message, "text", None) is not None
        )
        self.assertNotIn("line ID", response)

    async def test_new_me3_line_is_captured_before_unconfirmed_draft_is_priced(self) -> None:
        class IncorrectScenarioInterpreter:
            def __init__(self) -> None:
                self.calls: list[str] = []

            async def interpret(self, message: str, *_: object) -> object:
                self.calls.append(message)
                if message.strip().isdigit():
                    return SimpleNamespace(
                        action="set_quantity",
                        line_id="",
                        quantity=int(message),
                    )
                return SimpleNamespace(
                    action="build_scenario",
                    scenario="me3_copilot",
                    quantity=-1,
                    copilot_quantity=-1,
                )

        sender = "append-me3-before-pricing"
        await self.orchestrator.analyze_extracted(
            sender=sender,
            source_file="seller-message",
            rows=[
                ParsedLicenseRow(
                    row_number=2,
                    product_title="Power BI Pro",
                    total_licenses=14,
                    expired_licenses=0,
                    assigned_licenses=0,
                    renewal_quantity=14,
                    term_duration="P1Y",
                    billing_plan="Annual",
                )
            ],
        )
        await self.orchestrator.request_requirement_validation(sender)
        interpreter = IncorrectScenarioInterpreter()
        extractor = ContextAwareFragmentExtractor(product="ME3", quantity=15)
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
            requirement_extractor=extractor,  # type: ignore[arg-type]
        )

        await service._handle_text(sender, "Add ME3 within that")
        await service._handle_text(sender, "15")

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.estate is not None
        self.assertEqual(len(session.estate.lines), 2)
        self.assertEqual(session.estate.total_renewal_quantity, 29)
        self.assertEqual(session.estate.lines[1].source_product_title, "ME3")
        self.assertEqual(session.estate.lines[1].renewal_quantity, 15)
        self.assertIsNone(session.confirmed_as_is)
        self.assertEqual(session.scenarios, {})
        self.assertEqual(interpreter.calls, [])
        response = "\n".join(
            message.text.body  # type: ignore[attr-defined]
            for message in client.messages
            if getattr(message, "text", None) is not None
        )
        self.assertNotIn("Seller validation is required", response)
        self.assertNotIn("Provide the line ID", response)

    async def test_unconfirmed_replacement_is_not_mistaken_for_an_added_line(self) -> None:
        class ReplacementInterpreter:
            async def interpret(self, *_: object) -> object:
                return SimpleNamespace(
                    action="replace_sku",
                    line_id="L1",
                    product_query="Office 365 E1",
                    quantity=-1,
                )

        class ExtractorThatMustNotRun:
            def __init__(self) -> None:
                self.calls = 0

            async def extract_text(self, *_: object, **__: object) -> CapturedRequirement:
                self.calls += 1
                raise AssertionError("A replacement must not enter requirement capture")

        sender = "replace-unconfirmed-line"
        await self.orchestrator.analyze_extracted(
            sender=sender,
            source_file="seller-message",
            rows=[
                ParsedLicenseRow(
                    row_number=2,
                    product_title="Power BI Pro",
                    total_licenses=14,
                    expired_licenses=0,
                    assigned_licenses=0,
                    renewal_quantity=14,
                    term_duration="P1Y",
                    billing_plan="Annual",
                )
            ],
        )
        await self.orchestrator.request_requirement_validation(sender)
        extractor = ExtractorThatMustNotRun()
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
            intent_interpreter=ReplacementInterpreter(),  # type: ignore[arg-type]
            requirement_extractor=extractor,  # type: ignore[arg-type]
        )

        await service._handle_text(sender, "Replace L1 with Office 365 E1")

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.estate is not None
        self.assertEqual(extractor.calls, 0)
        self.assertEqual(len(session.estate.lines), 1)
        self.assertEqual(session.capture_messages, [])
        self.assertEqual(session.estate.lines[0].renewal_quantity, 14)
        self.assertEqual(session.estate.lines[0].display_title, "Office 365 E1")
        self.assertIsNone(session.confirmed_as_is)

    async def test_selection_guidance_interrupts_capture_without_losing_context(self) -> None:
        class RecommendationInterpreter:
            async def interpret(self, *_: object) -> object:
                return SimpleNamespace(
                    action="request_recommendation",
                    line_id="",
                    quantity=-1,
                    clarification=(
                        "Which business capability and user group should the licence support?"
                    ),
                )

        sender = "capture-stage-guidance"
        await self.orchestrator.analyze_extracted(
            sender=sender,
            source_file="seller-message",
            rows=[
                ParsedLicenseRow(
                    row_number=2,
                    product_title="Power BI Pro",
                    total_licenses=14,
                    expired_licenses=0,
                    assigned_licenses=0,
                    renewal_quantity=14,
                    term_duration="P1Y",
                    billing_plan="Annual",
                )
            ],
        )
        await self.orchestrator.request_requirement_validation(sender)
        await self.orchestrator.remember_capture_message(sender, "ME3")
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
            intent_interpreter=RecommendationInterpreter(),  # type: ignore[arg-type]
        )

        await service._handle_text(sender, "Can you give suggestions on picking a licence?")

        session = await self.orchestrator.get_session(sender)
        assert session is not None
        self.assertEqual(session.capture_messages, ["ME3"])
        response = client.messages[-1].text.body  # type: ignore[attr-defined]
        self.assertIn("help narrow the right licence", response)
        self.assertIn("Power BI Pro", response)
        self.assertIn("Which business capability", response)
        self.assertNotIn("I have not added that message", response)
        self.assertNotIn("Confirm the complete current requirement first", response)
        for corrupted in ("Â", "â€", "ðŸ", "�"):
            self.assertNotIn(corrupted, response)

    async def test_new_incomplete_addition_takes_priority_over_older_pending_match(
        self,
    ) -> None:
        class AddInterpreter:
            async def interpret(self, *_: object) -> object:
                return SimpleNamespace(
                    action="add_sku",
                    product_query="ME3",
                    quantity=-1,
                )

        sender = "new-addition-over-pending-match"
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
                    term_duration="P1Y",
                    billing_plan="Annual",
                )
            ],
        )
        self.assertEqual(len(estate.pending_lines), 1)
        extractor = ContextAwareFragmentExtractor(product="ME3", quantity=15)
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
            intent_interpreter=AddInterpreter(),  # type: ignore[arg-type]
            requirement_extractor=extractor,  # type: ignore[arg-type]
        )

        await service._handle_text(sender, "Add ME3 within that")
        session = await self.orchestrator.get_session(sender)
        assert session is not None
        self.assertEqual(session.capture_messages, ["Add ME3 within that"])

        await service._handle_text(sender, "15")

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.estate is not None
        self.assertEqual(session.capture_messages, [])
        self.assertEqual(len(session.estate.lines), 2)
        self.assertEqual(session.estate.lines[0].renewal_quantity, 10)
        self.assertEqual(session.estate.lines[1].source_product_title, "ME3")
        self.assertEqual(session.estate.lines[1].renewal_quantity, 15)
        response = "\n".join(
            message.text.body  # type: ignore[attr-defined]
            for message in client.messages
            if getattr(message, "text", None) is not None
        )
        self.assertNotIn("Provide the line ID", response)

    async def test_me7_quantity_is_captured_even_if_intent_model_calls_it_a_scenario(
        self,
    ) -> None:
        class ScenarioInterpreter:
            async def interpret(self, *_: object) -> object:
                return SimpleNamespace(
                    action="build_scenario",
                    scenario="me7",
                    product_query="",
                    quantity=-1,
                )

        sender = "me7-shorthand-seller"
        extractor = SingleTurnRequirementExtractor("ME7", 1)
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
            intent_interpreter=ScenarioInterpreter(),  # type: ignore[arg-type]
            requirement_extractor=extractor,  # type: ignore[arg-type]
        )

        await service._handle_text(sender, "ME7 1 qty")

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.estate is not None
        self.assertEqual(extractor.inputs, ["ME7 1 qty"])
        self.assertEqual(session.estate.lines[0].renewal_quantity, 1)
        self.assertEqual(session.estate.lines[0].source_product_title, "ME7")
        self.assertTrue(session.estate.pending_lines)
        self.assertTrue(
            all(
                candidate.sku_title.startswith("Microsoft 365 E7")
                for candidate in session.estate.pending_lines[0].candidates
            )
        )
        response = "\n".join(
            message.text.body  # type: ignore[attr-defined]
            for message in client.messages
            if getattr(message, "text", None) is not None
        )
        self.assertIn("Microsoft 365 E7 without Audio Conferencing", response)
        self.assertNotIn("Azure SQL Edge", response)

    async def test_unfinished_capture_allows_out_of_scope_interruptions_then_resumes(self) -> None:
        class IncorrectCaptureInterpreter:
            """Models the exact production failure: every turn is labelled capture."""

            async def interpret(self, _message: str, *_: object) -> object:
                return SimpleNamespace(action="capture_requirement")

            async def close(self) -> None:
                return None

        sender = "capture-interruption-seller"
        extractor = ContextAwareFragmentExtractor(product="Microsoft 365 E3", quantity=15)
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
            intent_interpreter=IncorrectCaptureInterpreter(),  # type: ignore[arg-type]
            requirement_extractor=extractor,  # type: ignore[arg-type]
        )

        await service._handle_text(sender, "Which licence did Virat Kohli buy?")
        session = await self.orchestrator.get_session(sender)
        self.assertTrue(session is None or not session.capture_messages)
        self.assertEqual(extractor.inputs, [])

        await service._handle_text(sender, "I like you")
        session = await self.orchestrator.get_session(sender)
        self.assertTrue(session is None or not session.capture_messages)
        self.assertEqual(extractor.inputs, [])

        await service._handle_text(sender, "E3")
        session = await self.orchestrator.get_session(sender)
        assert session is not None
        self.assertEqual(session.capture_messages, ["E3"])

        await service._handle_text(sender, "Who is Virat Kohli?")
        session = await self.orchestrator.get_session(sender)
        assert session is not None
        self.assertEqual(session.capture_messages, ["E3"])
        self.assertEqual(len(extractor.inputs), 1)

        await service._handle_text(sender, "15")
        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.estate is not None
        self.assertEqual(session.capture_messages, [])
        self.assertEqual(session.estate.lines[0].display_title, "Microsoft 365 E3")
        self.assertEqual(session.estate.lines[0].renewal_quantity, 15)

    async def test_pending_match_does_not_trap_unrelated_turn_when_model_is_unavailable(
        self,
    ) -> None:
        sender = "pending-match-model-outage"
        estate = await self.orchestrator.analyze_extracted(
            sender=sender,
            source_file="typed.txt",
            rows=[
                ParsedLicenseRow(
                    row_number=2,
                    product_title="E3",
                    total_licenses=10,
                    expired_licenses=0,
                    assigned_licenses=0,
                    renewal_quantity=10,
                    term_duration="P1Y",
                    billing_plan="Annual",
                )
            ],
        )
        self.assertTrue(estate.pending_lines)
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

        await service._handle_text(sender, "Who is Virat Kohli?")

        response = client.messages[-1].text.body  # type: ignore[attr-defined]
        self.assertIn("not added", response)
        self.assertIn("current licensing draft remains unchanged", response)
        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.estate is not None
        self.assertTrue(session.estate.pending_lines)

    async def test_conversational_acknowledgement_preserves_unfinished_context(self) -> None:
        class ConversationInterpreter:
            async def interpret(self, message: str, *_: object) -> object:
                if "thank" in message.casefold():
                    return SimpleNamespace(
                        action="acknowledge",
                        response_text=(
                            "You’re welcome. I’ve kept the current licensing details."
                        ),
                    )
                return SimpleNamespace(action="capture_requirement")

        sender = "acknowledgement-context-seller"
        await self.orchestrator.remember_capture_message(sender, "E3")
        extractor = ContextAwareFragmentExtractor(product="Microsoft 365 E3", quantity=15)
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
            intent_interpreter=ConversationInterpreter(),  # type: ignore[arg-type]
            requirement_extractor=extractor,  # type: ignore[arg-type]
        )

        await service._handle_text(sender, "Thank you so much")

        session = await self.orchestrator.get_session(sender)
        assert session is not None
        self.assertEqual(session.capture_messages, ["E3"])
        self.assertEqual(extractor.inputs, [])
        self.assertEqual(
            client.messages[-1].text.body,  # type: ignore[attr-defined]
            "You’re welcome. I’ve kept the current licensing details.",
        )

    async def test_complete_product_correction_supersedes_pending_match_without_loop(self) -> None:
        class CorrectionInterpreter:
            async def interpret(self, *_: object) -> object:
                return SimpleNamespace(
                    action="capture_requirement",
                    product_query="Microsoft 365 E7",
                    quantity=1,
                )

            async def close(self) -> None:
                return None

        sender = "pending-match-correction-seller"
        estate = await self.orchestrator.analyze_extracted(
            sender=sender,
            source_file="seller-message",
            rows=[
                ParsedLicenseRow(
                    row_number=2,
                    product_title="E1",
                    total_licenses=1,
                    expired_licenses=0,
                    assigned_licenses=0,
                    renewal_quantity=1,
                )
            ],
        )
        self.assertEqual(len(estate.pending_lines), 1)
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
            intent_interpreter=CorrectionInterpreter(),  # type: ignore[arg-type]
            requirement_extractor=SingleTurnRequirementExtractor(
                "Microsoft 365 E7",
                1,
            ),  # type: ignore[arg-type]
        )

        await service._handle_text(sender, "Who is Virat Kohli?")
        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.estate is not None
        self.assertEqual(len(session.estate.pending_lines), 1)
        self.assertIsNone(session.pending_sku_change)
        self.assertEqual(service._requirement_extractor.inputs, [])  # type: ignore[attr-defined]

        await service._handle_text(sender, "What is Office 365 E1?")
        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.estate is not None
        self.assertEqual(len(session.estate.pending_lines), 1)
        self.assertIsNone(session.pending_sku_change)

        await service._handle_text(
            sender,
            "This is confusing; use Microsoft 365 E7 for one licence",
        )

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.pending_sku_change is not None
        self.assertEqual(session.pending_sku_change.product_query, "Microsoft 365 E7")
        self.assertEqual(session.pending_sku_change.quantity, 1)
        response = "\n".join(
            message.text.body  # type: ignore[attr-defined]
            for message in client.messages
            if getattr(message, "text", None) is not None
        )
        self.assertIn("Microsoft 365 E7 without Audio Conferencing", response)
        self.assertNotIn("Tell me which product family appears", response)

        selected_title = session.pending_sku_change.candidates[0].sku_title
        await service._handle_text(sender, selected_title)
        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.estate is not None
        self.assertIsNone(session.pending_sku_change)
        self.assertEqual(session.estate.lines[0].display_title, selected_title)
        self.assertEqual(session.estate.lines[0].renewal_quantity, 1)
        self.assertEqual(session.stage, WorkflowStage.AWAITING_INITIAL_VALIDATION)

    async def test_new_subject_cancels_stale_pending_sku_choice(self) -> None:
        class OutOfScopeInterpreter:
            async def interpret(self, *_: object) -> object:
                return SimpleNamespace(
                    action="out_of_scope",
                    response_text=(
                        "That is outside this licensing advisor’s scope. I can help with "
                        "Microsoft licensing requirements and proposals."
                    ),
                )

            async def close(self) -> None:
                return None

        sender = "pending-sku-interruption-seller"
        await self.orchestrator.analyze_document(
            sender=sender,
            filename=CUSTOMER.name,
            content=CUSTOMER.read_bytes(),
        )
        await self.orchestrator.request_requirement_validation(sender)
        result = await self.orchestrator.replace_requirement_sku(
            sender,
            "L1",
            "Microsoft 365 E7",
            25,
        )
        self.assertEqual(result.state, "confirmation_required")
        assert result.confirmation is not None
        original_title = (await self.orchestrator.get_session(sender)).estate.lines[0].display_title  # type: ignore[union-attr]
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
            intent_interpreter=OutOfScopeInterpreter(),  # type: ignore[arg-type]
        )

        await service._handle_text(sender, "Who is Virat Kohli?")
        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.estate is not None
        self.assertIsNone(session.pending_sku_change)
        self.assertEqual(session.estate.lines[0].display_title, original_title)
        self.assertEqual(session.estate.lines[0].renewal_quantity, 45)
        self.assertIn("outside this licensing advisor", client.messages[-1].text.body)  # type: ignore[attr-defined]

    async def test_unclear_pending_sku_reply_closes_choice_without_replay(self) -> None:
        class ClarifyInterpreter:
            async def interpret(self, *_: object) -> object:
                return SimpleNamespace(
                    action="clarify",
                    clarification="Which exact product should I use?",
                )

            async def close(self) -> None:
                return None

        sender = "pending-sku-no-replay-seller"
        await self.orchestrator.analyze_document(
            sender=sender,
            filename=CUSTOMER.name,
            content=CUSTOMER.read_bytes(),
        )
        await self.orchestrator.request_requirement_validation(sender)
        result = await self.orchestrator.replace_requirement_sku(
            sender,
            "L1",
            "Microsoft 365 E7",
            25,
        )
        self.assertEqual(result.state, "confirmation_required")
        original = await self.orchestrator.get_session(sender)
        assert original is not None and original.estate is not None
        original_title = original.estate.lines[0].display_title
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
            intent_interpreter=ClarifyInterpreter(),  # type: ignore[arg-type]
        )

        await service._handle_text(sender, "That is not the product I meant")

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.estate is not None
        self.assertIsNone(session.pending_sku_change)
        self.assertEqual(session.estate.lines[0].display_title, original_title)
        response = client.messages[-1].text.body  # type: ignore[attr-defined]
        self.assertIn("proposal is unchanged", response)
        self.assertIn("repeated choice has been closed", response)
        self.assertNotIn("I need to confirm the intended SKU", response)

    async def test_new_subject_clears_stale_pending_dialogue(self) -> None:
        class OutOfScopeInterpreter:
            async def interpret(self, *_: object) -> object:
                return SimpleNamespace(
                    action="out_of_scope",
                    response_text=(
                        "That is outside this licensing advisor’s scope. I can help with "
                        "Microsoft licensing requirements and proposals."
                    ),
                )

            async def close(self) -> None:
                return None

        sender = "pending-dialogue-interruption-seller"
        await self.orchestrator.analyze_document(
            sender=sender,
            filename=CUSTOMER.name,
            content=CUSTOMER.read_bytes(),
        )
        original = PendingDialogue(
            kind="agent_clarification",
            question="Which current line should I evaluate?",
            context_message="Recommend a suitable alternative",
        )
        await self.orchestrator.set_pending_dialogue(sender, original)
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
            intent_interpreter=OutOfScopeInterpreter(),  # type: ignore[arg-type]
        )

        await service._handle_text(sender, "Which country should I visit?")

        session = await self.orchestrator.get_session(sender)
        assert session is not None
        self.assertIsNone(session.pending_dialogue)
        self.assertIn("outside this licensing advisor", client.messages[-1].text.body)  # type: ignore[attr-defined]

    async def test_incomplete_media_extraction_persists_supplied_facts_for_text_followup(self) -> None:
        sender = "media-clarification-seller"
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
        captured = CapturedRequirement(
            extraction=RequirementExtraction(
                lines=[
                    ExtractedRequirementLine(
                        sku_name="Power BI Pro",
                        quantity=0,
                        term_duration="P1Y",
                        billing_plan="Annual",
                        product_id="",
                        sku_id="",
                        expiration_date="",
                        renewal_date="",
                    )
                ],
                warnings=[],
                needs_clarification=True,
                clarification="How many Power BI Pro licences are required?",
            )
        )

        with self.assertRaisesRegex(
            RequirementCaptureError,
            "How many Power BI Pro licences are required",
        ):
            await service._analyze_captured(
                sender,
                "requirement-image.png",
                captured,
            )

        session = await self.orchestrator.get_session(sender)
        assert session is not None
        self.assertEqual(
            session.capture_messages,
            ["product Power BI Pro, term P1Y, billing Annual"],
        )

    async def test_all_e1_candidates_support_unsure_and_full_name_confirmation(self) -> None:
        sender = "all-e1-candidates-seller"
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
            [
                "Office 365 E1",
                "Office 365 E1 Plus",
                "Office 365 E1 (no Teams)",
                "Office 365 E1 Plus (No Teams)",
                "Office 365 E1 (Non-Profit Pricing)",
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
        )

        await service._process_captured_estate(sender, estate)
        initial_text = "\n".join(
            message.text.body  # type: ignore[attr-defined]
            for message in client.messages
            if getattr(message, "text", None) is not None
        )
        self.assertNotIn("one close catalogue match", initial_text)
        self.assertIn("more than one possible match", initial_text)
        self.assertIn("1. Office 365 E1", initial_text)
        self.assertIn("5. Office 365 E1 (Non-Profit Pricing)", initial_text)

        await service._handle_text(sender, "I don't know")
        unsure_text = client.messages[-1].text.body  # type: ignore[attr-defined]
        self.assertIn("paused the repeated SKU prompt", unsure_text)
        self.assertNotIn("1. Office 365 E1", unsure_text)
        session = await self.orchestrator.get_session(sender)
        assert session is not None
        self.assertTrue(session.pending_match_prompt_suspended)

        await service._handle_text(sender, "Who is Virat Kohli?")
        unrelated_text = client.messages[-1].text.body  # type: ignore[attr-defined]
        self.assertNotIn("paused the repeated SKU prompt", unrelated_text)
        self.assertNotIn("1. Office 365 E1", unrelated_text)

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
        option_text = next(
            message.text.body  # type: ignore[attr-defined]
            for message in client.messages
            if getattr(message, "text", None) is not None
        )
        option_rows = [
            row
            for message in client.messages
            if getattr(message, "interactive", None) is not None
            for section in message.interactive.action.sections  # type: ignore[attr-defined]
            for row in section.rows
        ]
        selected_row = next(row for row in option_rows if row.id.endswith("|2"))
        self.assertIn(f"Product ID: {selected.product_id}", option_text)
        self.assertIn(f"SKU ID: {selected.sku_id}", option_text)
        self.assertIn(
            f"ID {selected.product_id} / {selected.sku_id}",
            selected_row.description,
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
        class SavedDraftGreetingInterpreter:
            async def interpret(self, message: str, *_: object) -> object:
                if "e1" in message.casefold():
                    return SimpleNamespace(action="capture_requirement")
                return SimpleNamespace(
                    action="help",
                    response_text=(
                        "Welcome back. Your licensing draft contains one SKU line and 20 "
                        "licences. Would you like to resume it or start fresh?"
                    ),
                )

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
            intent_interpreter=SavedDraftGreetingInterpreter(),  # type: ignore[arg-type]
        )

        await service._handle_text(sender, "Hi")
        response = client.messages[-1].text.body  # type: ignore[attr-defined]
        self.assertIn("one SKU line and 20 licences", response)
        self.assertIn("resume", response.casefold())
        self.assertIn("start fresh", response.casefold())

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

    async def test_resume_saved_draft_takes_precedence_over_pending_sku_match(self) -> None:
        sender = "resume-ambiguous-draft-seller"
        await self.orchestrator.analyze_extracted(
            sender=sender,
            source_file="previous-session",
            rows=[
                ParsedLicenseRow(
                    row_number=2,
                    product_title="E1",
                    total_licenses=20,
                    expired_licenses=0,
                    assigned_licenses=0,
                    renewal_quantity=20,
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
        )

        await service._handle_text(sender, "Hi")
        await service._handle_text(sender, "Resume")

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.estate is not None
        self.assertIsNone(session.pending_dialogue)
        response = "\n".join(
            message.text.body  # type: ignore[attr-defined]
            for message in client.messages
            if getattr(message, "text", None) is not None
        )
        self.assertIn("Saved draft resumed", response)
        self.assertIn("Office 365 E1", response)

    async def test_saved_draft_edit_is_implicit_resume_without_prompt_loop(self) -> None:
        class SavedDraftEditInterpreter:
            async def interpret(self, message: str, *_: object) -> object:
                if message.casefold() == "hi":
                    return SimpleNamespace(
                        action="help",
                        response_text="Welcome back. A saved licensing draft is available.",
                    )
                return SimpleNamespace(
                    action="set_quantity",
                    line_id="",
                    quantity=25,
                )

        sender = "saved-draft-implicit-resume-seller"
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
            intent_interpreter=SavedDraftEditInterpreter(),  # type: ignore[arg-type]
        )

        await service._handle_text(sender, "Hi")
        await service._handle_text(sender, "Change Office 365 E1 to 25 licences")

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.estate is not None
        self.assertEqual(session.estate.lines[0].renewal_quantity, 25)
        self.assertIsNone(session.pending_dialogue)
        response = "\n".join(
            message.text.body  # type: ignore[attr-defined]
            for message in client.messages
            if getattr(message, "text", None) is not None
        )
        self.assertNotIn("will not merge", response)

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
        class DynamicHelpInterpreter:
            async def interpret(self, message: str, *_: object) -> object:
                normalized = message.casefold()
                if "what" in normalized:
                    response = (
                        "I capture Microsoft licensing needs, confirm exact SKUs, calculate "
                        "annual Renew As-Is pricing, and prepare reviewed proposals. What "
                        "licensing outcome would you like to work on?"
                    )
                elif "hi" in normalized:
                    response = (
                        "Hello. I’m the SkySecure Microsoft Licensing Advisor. How can I "
                        "help with your Microsoft licensing requirement today?"
                    )
                else:
                    response = (
                        "I’m the SkySecure Microsoft Licensing Advisor. You can describe a "
                        "requirement or share a supported file; what would you like to review?"
                    )
                return SimpleNamespace(action="help", response_text=response)

        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = WhatsAppWebhookService(
            client,  # type: ignore[arg-type]
            self.orchestrator,
            ServiceConfiguration(
                frozenset(),
                10 * 1024 * 1024,
                workflow_mode="simple_pricing",
            ),
            intent_interpreter=DynamicHelpInterpreter(),  # type: ignore[arg-type]
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
        responses = [message.text.body for message in client.messages]  # type: ignore[attr-defined]
        self.assertEqual(len(set(responses)), 3)
        self.assertTrue(all("licens" in response.casefold() for response in responses))
        self.assertTrue(all(response.rstrip().endswith("?") for response in responses))
        self.assertTrue(all(response != HELP_TEXT for response in responses))

    async def test_preupload_capability_question_is_not_treated_as_requirement_data(
        self,
    ) -> None:
        class HelpInterpreter:
            async def interpret(self, *_: object) -> object:
                return SimpleNamespace(
                    action="help",
                    response_text=(
                        "I can capture and validate Microsoft licensing requirements before "
                        "pricing. What requirement would you like help with?"
                    ),
                )

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
        self.assertIn(
            "capture and validate",
            client.messages[-1].text.body,  # type: ignore[attr-defined]
        )
        self.assertNotEqual(client.messages[-1].text.body, HELP_TEXT)  # type: ignore[attr-defined]

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

    async def test_prepricing_recommendation_question_is_persisted(self) -> None:
        sender = "draft-recommendation-context"
        await self.orchestrator.analyze_document(
            sender=sender,
            filename=CUSTOMER.name,
            content=CUSTOMER.read_bytes(),
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

        await service._execute_agent_intent(
            sender,
            SimpleNamespace(
                action="request_recommendation",
                clarification="Which business capability should it support?",
            ),  # type: ignore[arg-type]
            original_message="Can you suggest a better licence?",
        )

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.pending_dialogue is not None
        self.assertEqual(
            session.pending_dialogue.context_message,
            "Can you suggest a better licence?",
        )
        self.assertIn("Which business capability", session.pending_dialogue.question)

    async def test_recommendation_followup_uses_answer_instead_of_repeating_question(
        self,
    ) -> None:
        class RecommendationFollowupInterpreter:
            async def interpret(self, *_: object) -> object:
                return SimpleNamespace(action="request_recommendation")

            async def close(self) -> None:
                return None

        sender = "qa-recommendation-followup"
        await self.orchestrator.analyze_document(
            sender=sender,
            filename=CUSTOMER.name,
            content=CUSTOMER.read_bytes(),
        )
        await self.orchestrator.request_requirement_validation(sender)
        await self.orchestrator.set_pending_dialogue(
            sender,
            PendingDialogue(
                kind="agent_clarification",
                question="Which business capability and user group should it support?",
                context_message="Which of these products would you suggest buying?",
            ),
        )
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
            intent_interpreter=RecommendationFollowupInterpreter(),  # type: ignore[arg-type]
            recommendation_advisor=advisor,
        )

        await service._handle_text(sender, "productivity and analytics for all users")

        session = await self.orchestrator.get_session(sender)
        assert session is not None
        self.assertIsNone(session.pending_dialogue)
        self.assertEqual(len(advisor.calls), 1)
        self.assertIn(
            "productivity and analytics for all users",
            str(advisor.calls[0]["seller_question"]),
        )

    async def test_incomplete_postpricing_addition_keeps_context_for_quantity_reply(
        self,
    ) -> None:
        class AddQuantityInterpreter:
            async def interpret(self, message: str, *_: object) -> object:
                self_message = message.strip()
                return SimpleNamespace(
                    action="add_sku",
                    product_query="Visio Plan 2",
                    quantity=int(self_message),
                )

        sender = "postpricing-add-followup"
        await self.orchestrator.analyze_document(
            sender=sender,
            filename=CUSTOMER.name,
            content=CUSTOMER.read_bytes(),
        )
        await self.orchestrator.request_requirement_validation(sender)
        await self.orchestrator.confirm_requirement(sender)
        renew = await self.orchestrator.build_scenario(sender, ScenarioType.RENEW_AS_IS)
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
            intent_interpreter=AddQuantityInterpreter(),  # type: ignore[arg-type]
        )

        await service._execute_agent_intent(
            sender,
            SimpleNamespace(
                action="add_sku",
                product_query="Visio Plan 2",
                quantity=-1,
            ),  # type: ignore[arg-type]
            original_message="Add Visio Plan 2",
        )
        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.pending_dialogue is not None
        self.assertEqual(session.pending_dialogue.context_message, "Add Visio Plan 2")
        self.assertIn("How many Visio Plan 2", client.messages[-1].text.body)  # type: ignore[attr-defined]

        await service._handle_text(sender, "10")

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.active_scenario is not None
        self.assertIsNone(session.pending_dialogue)
        assert session.pending_sku_change is not None
        self.assertEqual(session.pending_sku_change.product_query, "Visio Plan 2")
        self.assertEqual(session.pending_sku_change.quantity, 10)

    async def _prepare_single_copilot_baseline(self, sender: str) -> None:
        content = (
            "Product Title,Total Licenses,Expired Licenses,Assigned licenses\n"
            "Microsoft 365 Copilot,51,0,0\n"
        ).encode()
        estate = await self.orchestrator.analyze_document(
            sender=sender,
            filename="copilot-requirement.csv",
            content=content,
        )
        self.assertFalse(estate.pending_lines)
        await self.orchestrator.request_requirement_validation(sender)
        await self.orchestrator.confirm_requirement(sender)
        renew = await self.orchestrator.build_scenario(sender, ScenarioType.RENEW_AS_IS)
        await self.orchestrator.save_confirmed_as_is(sender, renew)

    async def test_postpricing_add_product_then_quantity_survives_set_copilot_misroute(
        self,
    ) -> None:
        class MisclassifyingFollowupInterpreter:
            async def interpret(self, message: str, *_: object) -> AgentIntent:
                normalized = " ".join(message.casefold().split())
                if normalized == "add more sku":
                    return _agent_intent("add_sku")
                if normalized == "microsoft 365 copilot":
                    return _agent_intent(
                        "add_sku",
                        product_query="Microsoft 365 Copilot",
                    )
                if normalized == "51":
                    # This is the production failure captured in the CEO transcript: the
                    # stateless model sees "Copilot" in the question and misclassifies the
                    # missing add quantity as the generated-scenario Copilot control.
                    return _agent_intent("set_copilot", copilot_quantity=51)
                raise AssertionError(f"Unexpected message: {message}")

        sender = "ceo-add-copilot-followup"
        await self.orchestrator.analyze_document(
            sender=sender,
            filename=CUSTOMER.name,
            content=CUSTOMER.read_bytes(),
        )
        await self.orchestrator.request_requirement_validation(sender)
        await self.orchestrator.confirm_requirement(sender)
        renew = await self.orchestrator.build_scenario(sender, ScenarioType.RENEW_AS_IS)
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
            intent_interpreter=MisclassifyingFollowupInterpreter(),
        )

        await service._handle_text(sender, "Add more SKU")
        await service._handle_text(sender, "MICROSOFT 365 COPILOT")
        await service._handle_text(sender, "51")

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.active_scenario is not None
        if session.pending_sku_change is not None:
            self.assertEqual(session.pending_sku_change.action, "add")
            self.assertEqual(
                session.pending_sku_change.product_query,
                "Microsoft 365 Copilot",
            )
            self.assertEqual(session.pending_sku_change.quantity, 51)
        else:
            scenario = session.scenarios[session.active_scenario]
            self.assertTrue(
                any(
                    line.sku_title == "Microsoft 365 Copilot"
                    and line.proposed_quantity == 51
                    for line in scenario.lines
                )
            )
        seller_text = "\n".join(
            message.text.body  # type: ignore[attr-defined]
            for message in client.messages
            if getattr(message, "text", None) is not None
        )
        self.assertNotIn("Scenario line", seller_text)
        self.assertNotIn("'COPILOT' was not found", seller_text)

    async def test_change_sku_dimension_keeps_source_until_target_product_arrives(
        self,
    ) -> None:
        class ChangeSkuInterpreter:
            async def interpret(self, message: str, *_: object) -> AgentIntent:
                normalized = " ".join(message.casefold().split())
                if normalized in {"sku", "i want to change the sku"}:
                    return _agent_intent("replace_sku", line_id="L1")
                if normalized == "power bi pro":
                    # A product-only answer can be classified as fresh capture by the model;
                    # the deterministic pending operation must still treat it as the target.
                    return _agent_intent(
                        "capture_requirement",
                        product_query="Power BI Pro",
                    )
                raise AssertionError(f"Unexpected message: {message}")

        sender = "ceo-change-sku-followup"
        await self._prepare_single_copilot_baseline(sender)
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
            intent_interpreter=ChangeSkuInterpreter(),
        )
        await service._execute_agent_intent(
            sender,
            _agent_intent(
                "clarify",
                clarification=(
                    "What would you like to change about Microsoft 365 Copilot: its "
                    "quantity, SKU, or disposition?"
                ),
            ),
            original_message="Change Microsoft 365 Copilot",
        )

        await service._handle_text(sender, "SKU")
        await service._handle_text(sender, "I want to change the SKU")
        await service._handle_text(sender, "Power BI Pro")

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.active_scenario is not None
        self.assertIsNone(session.pending_dialogue)
        if session.pending_sku_change is not None:
            self.assertEqual(session.pending_sku_change.action, "replace")
            self.assertEqual(session.pending_sku_change.source_line_id, "L1")
            self.assertEqual(session.pending_sku_change.product_query, "Power BI Pro")
        else:
            scenario = session.scenarios[session.active_scenario]
            source = next(line for line in scenario.lines if line.line_id == "L1")
            replacement = next(
                line for line in scenario.lines if line.sku_title == "Power BI Pro"
            )
            self.assertEqual(source.proposed_quantity, 0)
            self.assertEqual(source.disposition.value, "remove")
            self.assertEqual(replacement.proposed_quantity, 51)
            self.assertEqual(replacement.disposition.value, "add")
        questions = [
            message.text.body  # type: ignore[attr-defined]
            for message in client.messages
            if getattr(message, "text", None) is not None
        ]
        self.assertFalse(
            any(
                "what would you like to change about the microsoft 365 copilot sku"
                in question.casefold()
                for question in questions
            )
        )

    async def test_set_copilot_resolves_unique_real_copilot_line_without_pseudo_id(
        self,
    ) -> None:
        sender = "real-copilot-line-quantity"
        await self._prepare_single_copilot_baseline(sender)
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
            _agent_intent("set_copilot", copilot_quantity=52),
            original_message="Change Microsoft 365 Copilot to 52 licences",
        )

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.active_scenario is not None
        scenario = session.scenarios[session.active_scenario]
        self.assertFalse(any(line.line_id == "COPILOT" for line in scenario.lines))
        actual = next(line for line in scenario.lines if line.line_id == "L1")
        self.assertEqual(actual.sku_title, "Microsoft 365 Copilot")
        self.assertEqual(actual.proposed_quantity, 52)

    async def test_set_copilot_updates_existing_zero_quantity_synthetic_line(
        self,
    ) -> None:
        sender = "zero-copilot-line-quantity"
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
        )
        await self.orchestrator.save_confirmed_as_is(sender, renew)
        initial = await self.orchestrator.build_scenario(
            sender,
            ScenarioType.ME5_COPILOT,
            copilot_quantity=0,
        )
        copilot = next(line for line in initial.lines if line.line_id == "COPILOT")
        self.assertEqual(copilot.proposed_quantity, 0)

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
            _agent_intent(
                "set_copilot",
                scenario="me5_copilot",
                copilot_quantity=30,
            ),
            original_message="Set Copilot to 30 licences",
        )

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.active_scenario is not None
        scenario = session.scenarios[session.active_scenario]
        updated = next(line for line in scenario.lines if line.line_id == "COPILOT")
        self.assertEqual(updated.proposed_quantity, 30)
        self.assertIsNone(session.pending_dialogue)

    async def test_scenario_error_is_sanitized_before_seller_delivery(self) -> None:
        class ScenarioFailureService(WhatsAppWebhookService):
            async def _handle_text(self, *_: object) -> None:
                raise ScenarioError("Scenario line 'COPILOT' was not found.")

        sender = "safe-scenario-error-seller"
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = ScenarioFailureService(
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
                    "id": "safe-scenario-error",
                    "from": sender,
                    "type": "text",
                    "text": {"body": "51"},
                }
            )
        )

        self.assertEqual(len(client.messages), 1)
        response = client.messages[0].text.body  # type: ignore[attr-defined]
        self.assertNotIn("COPILOT", response)
        self.assertNotIn("Scenario line", response)
        self.assertNotIn("was not found", response)

    async def test_plural_requirement_line_error_is_sanitized_before_delivery(
        self,
    ) -> None:
        class PluralLineFailureService(WhatsAppWebhookService):
            async def _handle_text(self, *_: object) -> None:
                raise ScenarioError("Requirement line(s) not found: L42, L99.")

        sender = "safe-plural-line-error-seller"
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = PluralLineFailureService(
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
                    "id": "safe-plural-line-error",
                    "from": sender,
                    "type": "text",
                    "text": {"body": "Remove the old lines"},
                }
            )
        )

        self.assertEqual(len(client.messages), 1)
        response = client.messages[0].text.body  # type: ignore[attr-defined]
        self.assertNotIn("Requirement line(s)", response)
        self.assertNotIn("L42", response)
        self.assertNotIn("L99", response)
        self.assertNotIn("not found", response.casefold())

    async def test_proposal_target_reply_preserves_complete_pending_add_or_replace(
        self,
    ) -> None:
        class ProposalTargetInterpreter:
            async def interpret(self, message: str, *_: object) -> AgentIntent:
                self_message = " ".join(message.casefold().split())
                if self_message != "me5":
                    raise AssertionError(f"Unexpected message: {message}")
                # Deliberately reproduce a plausible model mistake. The proposal name is
                # emitted as a fresh product instead of as the target scenario; deterministic
                # pending state must preserve the original operation slots.
                return _agent_intent(
                    "capture_requirement",
                    product_query="ME5",
                )

        cases = (
            ("add_sku", "Visio Plan 2", 17, ""),
            ("replace_sku", "Power BI Pro", 19, "L1"),
        )
        for action, product, quantity, source_line_id in cases:
            with self.subTest(action=action):
                sender = f"proposal-target-{action}"
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
                )
                await self.orchestrator.save_confirmed_as_is(sender, renew)
                await self.orchestrator.build_scenario(
                    sender,
                    ScenarioType.ME5_COPILOT,
                )
                await self.orchestrator.build_scenario(
                    sender,
                    ScenarioType.RENEW_AS_IS,
                )
                client = FakeWhatsAppClient(
                    WhatsAppMedia(b"", "unused", "text/plain")
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
                    intent_interpreter=ProposalTargetInterpreter(),
                )

                await service._execute_agent_intent(
                    sender,
                    _agent_intent(
                        action,
                        line_id=source_line_id,
                        product_query=product,
                        quantity=quantity,
                    ),
                    original_message=(
                        f"Add {quantity} {product} licences"
                        if action == "add_sku"
                        else f"Replace L1 with {product} for {quantity} licences"
                    ),
                )
                before = await self.orchestrator.get_session(sender)
                assert before is not None and before.pending_dialogue is not None
                self.assertEqual(before.pending_dialogue.operation, action)
                self.assertEqual(before.pending_dialogue.product_query, product)
                self.assertEqual(before.pending_dialogue.quantity, quantity)
                self.assertEqual(
                    before.pending_dialogue.source_line_id,
                    source_line_id,
                )

                await service._handle_text(sender, "ME5")

                after = await self.orchestrator.get_session(sender)
                assert after is not None and after.active_scenario is not None
                self.assertEqual(after.active_scenario, ScenarioType.ME5_COPILOT)
                self.assertIsNone(after.pending_dialogue)
                if after.pending_sku_change is not None:
                    expected_action = "add" if action == "add_sku" else "replace"
                    self.assertEqual(after.pending_sku_change.action, expected_action)
                    self.assertEqual(after.pending_sku_change.product_query, product)
                    self.assertEqual(after.pending_sku_change.quantity, quantity)
                    self.assertEqual(
                        after.pending_sku_change.scenario_type,
                        ScenarioType.ME5_COPILOT,
                    )
                else:
                    scenario = after.scenarios[ScenarioType.ME5_COPILOT]
                    selected = next(
                        line
                        for line in scenario.lines
                        if line.sku_title == product and line.proposed_quantity == quantity
                    )
                    self.assertEqual(selected.proposed_quantity, quantity)
                seller_text = "\n".join(
                    message.text.body  # type: ignore[attr-defined]
                    for message in client.messages
                    if getattr(message, "text", None) is not None
                )
                self.assertNotIn("How many ME5", seller_text)
                self.assertNotIn("add ME5", seller_text.casefold())

    async def test_failed_completed_pending_operation_remains_available_for_retry(
        self,
    ) -> None:
        class QuantityMisrouteInterpreter:
            async def interpret(self, message: str, *_: object) -> AgentIntent:
                if message.strip() != "10":
                    raise AssertionError(f"Unexpected message: {message}")
                return _agent_intent("set_copilot", copilot_quantity=10)

        sender = "pending-operation-safe-retry"
        await self.orchestrator.analyze_document(
            sender=sender,
            filename=CUSTOMER.name,
            content=CUSTOMER.read_bytes(),
        )
        await self.orchestrator.request_requirement_validation(sender)
        await self.orchestrator.confirm_requirement(sender)
        renew = await self.orchestrator.build_scenario(sender, ScenarioType.RENEW_AS_IS)
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
            intent_interpreter=QuantityMisrouteInterpreter(),
        )
        await service._execute_agent_intent(
            sender,
            _agent_intent(
                "add_sku",
                product_query="Visio Plan 2",
            ),
            original_message="Add Visio Plan 2",
        )
        before = await self.orchestrator.get_session(sender)
        assert before is not None and before.pending_dialogue is not None
        expected_pending = before.pending_dialogue

        failing_add = AsyncMock(
            side_effect=ScenarioError("Synthetic add failure after slot completion.")
        )
        with patch.object(self.orchestrator, "add_sku", failing_add):
            with self.assertRaises(ScenarioError):
                await service._handle_text(sender, "10")

        after = await self.orchestrator.get_session(sender)
        assert after is not None and after.pending_dialogue is not None
        self.assertEqual(after.pending_dialogue, expected_pending)
        self.assertEqual(after.pending_dialogue.operation, "add_sku")
        self.assertEqual(after.pending_dialogue.product_query, "Visio Plan 2")
        self.assertEqual(after.pending_dialogue.quantity, -1)
        failing_add.assert_awaited_once()

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

    async def test_intent_context_exposes_exact_proposal_facts_for_followup_questions(
        self,
    ) -> None:
        sender = "proposal-fact-context-seller"
        await self.orchestrator.analyze_document(
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
        session = await self.orchestrator.get_session(sender)
        interpreter = OpenAIIntentInterpreter(
            api_key="test-key",
            model="test-model",
            workflow_mode="simple_pricing",
            client=SimpleNamespace(),
        )

        context = interpreter._context(session)

        self.assertEqual(context["reference_line_count"], 5)
        self.assertEqual(context["reference_total_quantity"], 168)
        self.assertEqual(
            context["reference_products_alphabetical"],
            sorted(context["reference_products_alphabetical"], key=str.casefold),
        )
        self.assertIsNotNone(context["reference_cheapest_by_annual_unit_price"])
        self.assertIsNotNone(context["reference_costliest_by_annual_unit_price"])
        self.assertFalse(context["inventory_stock_data_available"])
        self.assertFalse(context["historical_purchase_count_available"])

    async def test_catalog_budget_question_uses_current_rate_card_without_mutation(
        self,
    ) -> None:
        sender = "catalog-budget-seller"
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = WhatsAppWebhookService(
            client,  # type: ignore[arg-type]
            self.orchestrator,
            ServiceConfiguration(
                frozenset(),
                10 * 1024 * 1024,
                allow_all_sellers=True,
                workflow_mode="simple_pricing",
                simple_price_basis="marketplace",
            ),
        )

        await service._execute_agent_intent(
            sender,
            SimpleNamespace(
                action="answer_question",
                detail_label="catalog_budget",
                amount=5000,
                product_query="",
            ),
            original_message="If I have INR 5,000, what can I buy?",
        )

        response = "\n".join(
            message.text.body  # type: ignore[attr-defined]
            for message in client.messages
            if getattr(message, "text", None) is not None
        )
        self.assertIn("within INR 5,000.00 per licence", response)
        self.assertIn("per licence/year", response)
        self.assertIn("not a feature-fit recommendation", response)
        self.assertIsNone(await self.orchestrator.get_session(sender))

    async def test_official_product_question_is_grounded_and_hides_source_urls(
        self,
    ) -> None:
        sender = "official-product-question-seller"
        await self.orchestrator.analyze_document(
            sender=sender,
            filename=CUSTOMER.name,
            content=CUSTOMER.read_bytes(),
        )
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
                action="answer_question",
                detail_label="official_product_question",
                detail_value="Can I use Teams in these products?",
                product_query="Microsoft Teams",
            ),
            original_message="Can I use Teams in these products?",
        )

        self.assertEqual(len(advisor.calls), 1)
        self.assertEqual(len(advisor.calls[0]["product_names"]), 6)
        self.assertEqual(advisor.calls[0]["product_names"][0], "Microsoft Teams")
        self.assertIn("Advanced Communications", advisor.calls[0]["product_names"])
        response = client.messages[-1].text.body  # type: ignore[attr-defined]
        self.assertIn("Microsoft Teams", response)
        self.assertNotIn("learn.microsoft.com", response)
        self.assertNotIn("https://", response)

    async def test_product_list_question_renders_mobile_table_without_mutating_draft(
        self,
    ) -> None:
        class ProductQuestionInterpreter:
            async def interpret(self, message: str, *_: object) -> object:
                return SimpleNamespace(
                    action="answer_question",
                    detail_label="official_product_question",
                    detail_value=message,
                    product_query="",
                )

            async def close(self) -> None:
                return None

        class StructuredAdvisor:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            async def answer_product_question(self, **kwargs: object) -> OfficialProductAnswer:
                self.calls.append(kwargs)
                return OfficialProductAnswer(
                    answer="Here is the requested product-by-product review.",
                    clarification_question=(
                        "Confirm the tenant type only if you want an eligibility assessment."
                    ),
                    table_title="Student and business suitability",
                    table_headers=["Product", "Student use", "Business use"],
                    table_rows=[
                        ["Agent 365 (Education Student)", "Education", "Plan dependent"],
                        ["Microsoft Defender Suite", "Not established", "Business security"],
                    ],
                    source_urls=["https://learn.microsoft.com/en-us/microsoft-365/"],
                )

            async def advise(self, **_: object) -> OfficialRecommendation:
                raise AssertionError("Recommendation mutation must not be called.")

            async def close(self) -> None:
                return None

        sender = "qa-product-list-table"
        await self.orchestrator.analyze_document(
            sender=sender,
            filename=CUSTOMER.name,
            content=CUSTOMER.read_bytes(),
        )
        await self.orchestrator.request_requirement_validation(sender)
        before = await self.orchestrator.get_session(sender)
        assert before is not None and before.estate is not None
        before_titles = [line.display_title for line in before.estate.lines]
        advisor = StructuredAdvisor()
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
            intent_interpreter=ProductQuestionInterpreter(),  # type: ignore[arg-type]
            recommendation_advisor=advisor,  # type: ignore[arg-type]
        )
        question = (
            "Microsoft Defender Suite for Microsoft 365 Business Premium\n"
            "Agent 365 (Education Student)\n"
            "Which of these products is student-friendly and suitable for business use?"
        )

        await service._handle_text(sender, question)

        after = await self.orchestrator.get_session(sender)
        assert after is not None and after.estate is not None
        self.assertEqual([line.display_title for line in after.estate.lines], before_titles)
        self.assertEqual(len(advisor.calls), 1)
        supplied_products = advisor.calls[0]["product_names"]
        self.assertIn(
            "Agent 365 (Education Student)",
            supplied_products,  # type: ignore[operator]
        )
        self.assertTrue(client.images)
        self.assertEqual(
            client.images[0]["filename"],
            "licensing-guidance-table-1.png",
        )
        self.assertIsNone(after.pending_dialogue)

    async def test_voice_question_routes_through_conversation_instead_of_capture_error(
        self,
    ) -> None:
        class VoiceQuestionExtractor:
            async def extract_audio(self, *_: object, **__: object) -> CapturedRequirement:
                return CapturedRequirement(
                    extraction=RequirementExtraction(
                        lines=[],
                        warnings=[],
                        needs_clarification=False,
                        clarification="",
                    ),
                    transcript="Is there any warranty for these products?",
                )

        class ProductQuestionInterpreter:
            async def interpret(self, message: str, *_: object) -> object:
                return SimpleNamespace(
                    action="answer_question",
                    detail_label="official_product_question",
                    detail_value=message,
                    product_query="",
                    response_text="",
                )

        sender = "voice-product-question-seller"
        await self.orchestrator.analyze_document(
            sender=sender,
            filename=CUSTOMER.name,
            content=CUSTOMER.read_bytes(),
        )
        advisor = FakeRecommendationAdvisor()
        client = FakeWhatsAppClient(
            WhatsAppMedia(b"voice", "voice.ogg", "audio/ogg; codecs=opus")
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
            intent_interpreter=ProductQuestionInterpreter(),  # type: ignore[arg-type]
            requirement_extractor=VoiceQuestionExtractor(),  # type: ignore[arg-type]
            recommendation_advisor=advisor,
        )

        await service.handle(
            _webhook(
                {
                    "id": "voice-question",
                    "from": sender,
                    "type": "audio",
                    "audio": {
                        "id": "voice-media",
                        "mime_type": "audio/ogg; codecs=opus",
                        "voice": True,
                    },
                }
            )
        )

        self.assertEqual(len(advisor.calls), 1)
        bodies = [
            message.text.body  # type: ignore[attr-defined]
            for message in client.messages
            if getattr(message, "text", None) is not None
        ]
        self.assertTrue(any("Voice transcript" in body for body in bodies))
        self.assertTrue(any("Microsoft Teams" in body for body in bodies))
        self.assertFalse(any("No licensing requirement lines found" in body for body in bodies))

    async def test_attachment_caption_is_processed_as_a_followup_question(self) -> None:
        class CaptionInterpreter:
            def __init__(self) -> None:
                self.messages: list[str] = []

            async def interpret(self, message: str, *_: object) -> object:
                self.messages.append(message)
                return SimpleNamespace(
                    action="answer_question",
                    detail_label="",
                    response_text=(
                        "I captured the attachment. Confirm the requirement before I quote "
                        "which submitted line is within the requested budget."
                    ),
                )

        interpreter = CaptionInterpreter()
        extractor = FakeRequirementExtractor()
        client = FakeWhatsAppClient(WhatsAppMedia(b"image", "image.png", "image/png"))
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

        await service.handle(
            _webhook(
                {
                    "id": "captioned-image",
                    "from": "caption-seller",
                    "type": "image",
                    "image": {
                        "id": "image-media",
                        "mime_type": "image/png",
                        "caption": "Show me products under INR 5,000",
                    },
                }
            )
        )

        self.assertIn("Show me products under INR 5,000", interpreter.messages)
        self.assertIn(
            "Confirm the requirement before I quote",
            client.messages[-1].text.body,  # type: ignore[attr-defined]
        )

    async def test_product_title_cannot_be_saved_as_requirement_metadata(self) -> None:
        sender = "product-metadata-guard-seller"
        await self.orchestrator.analyze_document(
            sender=sender,
            filename=CUSTOMER.name,
            content=CUSTOMER.read_bytes(),
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

        await service._execute_agent_intent(
            sender,
            SimpleNamespace(
                action="set_requirement_detail",
                detail_label="Advanced Communications",
                detail_value="",
            ),
            original_message="Advanced Communications",
        )

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.estate is not None
        self.assertEqual(session.estate.seller_details, [])
        self.assertIn(
            "recognized that as a Microsoft product",
            client.messages[-1].text.body,  # type: ignore[attr-defined]
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
