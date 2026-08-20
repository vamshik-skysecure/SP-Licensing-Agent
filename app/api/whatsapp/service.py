from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

from app.config import get_logger, opaque_identifier
from app.core.licensing.analysis import LicenseAnalysisError
from app.core.licensing.capture import (
    CapturedRequirement,
    RequirementCaptureError,
    RequirementExtractor,
)
from app.core.licensing.agent import (
    AgentIntent,
    IntentInterpretationError,
    IntentInterpreter,
    RecommendationAdvisor,
)
from app.core.licensing.models import (
    LicenseEstate,
    MigrationDisposition,
    PendingDialogue,
    ScenarioStatus,
    ScenarioType,
    SellerProvidedDetail,
    SkuChangeResult,
    WorkflowSession,
    WorkflowStage,
)
from app.core.licensing.mobile_tables import (
    render_comparison_table_images,
    render_estate_table_images,
    render_scenario_table_images,
    render_simple_pricing_table_images,
)
from app.core.licensing.orchestrator import LicensingOrchestrator
from app.core.licensing.rate_card import normalize_product_title
from app.core.licensing.renderer import (
    format_comparison,
    format_estate,
    format_money,
    format_pending_matches,
    format_scenario,
    format_sku_candidate,
    render_comparison_pdf,
    render_estate_pdf,
    render_proposal_pdf,
    render_simple_commercial_pdf,
    sku_clarification_question,
)
from app.core.licensing.scenarios import ScenarioError
from app.core.licensing.store import WorkflowConflictError
from app.core.whatsapp import WhatsAppAPIError, WhatsAppClient
from app.schema.whatsapp import (
    IncomingWhatsAppDocument,
    IncomingWhatsAppAudio,
    IncomingWhatsAppImage,
    IncomingWhatsAppMessage,
    InteractiveList,
    InteractiveListAction,
    InteractiveRow,
    InteractiveSection,
    InteractiveText,
    TextContent,
    WhatsAppInteractiveMessage,
    WhatsAppTextMessage,
    WhatsAppWebhookPayload,
)

logger = get_logger(__name__)
RESPONSIVE_MESSAGE_LIMIT = 900


HELP_TEXT = (
    "*SkySecure Microsoft Licensing Advisor*\n\n"
    "Hello — I’m ready to help you prepare a clear annual Microsoft licensing proposal.\n\n"
    "Send Excel/CSV, Word/PDF, an image or screenshot, a voice note, or type the requirement "
    "here. You may give the details in one message or one at a time; I will keep the context "
    "and ask only for information that is still missing.\n\n"
    "I will first show the complete captured requirement for your approval. Only after you "
    "confirm it will I calculate the Renew As-Is cost. I can then evaluate requested quantity "
    "changes, additions, replacements, or higher-tier options and prepare customer-ready PDFs "
    "after final seller confirmation.\n\n"
    "Send the first requirement whenever you are ready. No commands are required."
)


RESET_REQUESTS = {
    "start fresh",
    "start from fresh",
    "start again",
    "reset",
    "reset everything",
    "clear everything",
    "clear the requirement",
    "new requirement",
    "begin a new requirement",
}

RESUME_REQUESTS = {
    "resume",
    "resume it",
    "resume draft",
    "resume the draft",
    "continue",
    "continue it",
    "continue draft",
    "continue the draft",
    "show draft",
    "show the draft",
}

UNCERTAIN_REPLIES = {
    "i don't know",
    "i dont know",
    "don't know",
    "dont know",
    "not sure",
    "i am not sure",
    "i'm not sure",
    "no idea",
}

AFFIRMATIVE_REPLIES = {
    "yes",
    "yes use it",
    "use it",
    "use that",
    "go with it",
    "that one",
    "correct",
    "yes correct",
}

REQUIREMENT_CONFIRMATION_REPLIES = {
    "approve",
    "approved",
    "confirm",
    "confirmed",
    "confirm for pricing",
    "confirm requirement",
    "confirm the requirement",
    "i approve",
    "i confirm",
    "it is correct",
    "proceed with pricing",
    "yes confirm",
    "yes confirm for pricing",
    "yes it is correct",
    "yes proceed",
}

CANCEL_REPLIES = {
    "cancel",
    "cancel that",
    "i don't want that",
    "i dont want that",
    "don't want that",
    "dont want that",
    "remove it",
    "remove that",
}

SCENARIO_EDIT_ACTIONS = {
    "set_quantity",
    "set_copilot",
    "set_disposition",
    "add_sku",
    "replace_sku",
    "set_term",
    "set_billing",
    "set_segment",
    "add_comment",
}

SIMPLE_PRICING_RESTRICTED_TERMS = (
    "promo",
    "promotion",
    "discount",
    "adjustment",
    "adjust ",
    "margin",
    "partner best",
    "partner-best",
    "best offer",
    "best price",
)


SCENARIO_ALIASES = {
    "renew": ScenarioType.RENEW_AS_IS,
    "renew_as_is": ScenarioType.RENEW_AS_IS,
    "me3": ScenarioType.ME3_COPILOT,
    "me3_copilot": ScenarioType.ME3_COPILOT,
    "me5": ScenarioType.ME5_COPILOT,
    "me5_copilot": ScenarioType.ME5_COPILOT,
    "me7": ScenarioType.ME7,
}


@dataclass(frozen=True)
class ServiceConfiguration:
    seller_allowlist: frozenset[str]
    max_document_bytes: int
    allow_all_sellers: bool = False
    max_image_bytes: int = 8 * 1024 * 1024
    max_audio_bytes: int = 10 * 1024 * 1024
    currency: str = "INR"
    workflow_mode: Literal[
        "simple_pricing",
        "renewal_only",
        "upgrade_comparison",
        "scenario_comparison",
    ] = "scenario_comparison"


class WhatsAppWebhookService:
    def __init__(
        self,
        whatsapp_client: WhatsAppClient,
        orchestrator: LicensingOrchestrator,
        configuration: ServiceConfiguration,
        *,
        intent_interpreter: IntentInterpreter | None = None,
        requirement_extractor: RequirementExtractor | None = None,
        recommendation_advisor: RecommendationAdvisor | None = None,
    ) -> None:
        self._whatsapp_client = whatsapp_client
        self._orchestrator = orchestrator
        self._configuration = configuration
        self._intent_interpreter = intent_interpreter
        self._requirement_extractor = requirement_extractor
        self._recommendation_advisor = recommendation_advisor

    async def handle(self, webhook: WhatsAppWebhookPayload) -> None:
        for entry in webhook.entry:
            for change in entry.changes:
                for message in change.value.messages:
                    await self._handle_message(message)

    async def _handle_message(self, message: IncomingWhatsAppMessage) -> None:
        sender = message.sender.lstrip("+")
        message_ref = opaque_identifier(message.id)
        if (
            not self._configuration.allow_all_sellers
            and sender not in self._configuration.seller_allowlist
        ):
            logger.warning("Unauthorized WhatsApp sender rejected")
            await self._send_text(sender, "This WhatsApp number is not authorized.")
            return
        expired = await self._orchestrator.reset_expired_session(sender)
        if expired:
            await self._send_text(
                sender,
                "Your previous licensing session expired after five minutes of inactivity. "
                "I have started a new requirement and will not reuse any earlier proposal "
                "details.",
            )
        if await self._orchestrator.has_processed(sender, message.id):
            logger.info("Duplicate WhatsApp message ignored message_ref=%s", message_ref)
            return

        try:
            if message.type == "text" and message.text is not None:
                await self._handle_text(sender, message.text.body)
            elif message.type == "document" and message.document is not None:
                await self._handle_document(sender, message.document)
            elif message.type == "image" and message.image is not None:
                await self._handle_image(sender, message.image)
            elif message.type == "audio" and message.audio is not None:
                await self._handle_audio(sender, message.audio)
            elif message.type == "interactive" and message.interactive is not None:
                reply = message.interactive.list_reply or message.interactive.button_reply
                if reply is None:
                    raise ValueError("The interactive response was empty.")
                await self._handle_interactive(sender, reply.id)
            else:
                await self._send_text(
                    sender,
                    "Send a requirement as text, voice, image, or a supported document.",
                )
            await self._orchestrator.mark_processed(sender, message.id)
        except (
            LicenseAnalysisError,
            RequirementCaptureError,
            ScenarioError,
            ValueError,
        ) as error:
            logger.info("User-correctable workflow error type=%s", type(error).__name__)
            await self._send_text(sender, str(error))
            await self._orchestrator.mark_processed(sender, message.id)
        except WorkflowConflictError:
            logger.warning("Workflow concurrency conflict message_ref=%s", message_ref)
            await self._send_text(sender, "The proposal changed concurrently. Please retry.")
        except Exception:
            logger.exception("Unexpected workflow failure message_ref=%s", message_ref)
            await self._send_text(
                sender,
                "I could not complete that operation. It is safe to retry the same message.",
            )
            raise

    async def _handle_document(
        self,
        sender: str,
        document: IncomingWhatsAppDocument,
    ) -> None:
        append_to_draft = await self._has_open_requirement_draft(sender)
        suffix = document.filename.lower().rsplit(".", 1)[-1]
        supported_files = {
            "csv",
            "tsv",
            "xls",
            "xlsx",
            "xlsm",
            "xla",
            "xlb",
            "xlc",
            "xlm",
            "xlt",
            "xlw",
            "iif",
            "pdf",
            "doc",
            "docx",
            "rtf",
            "odt",
            "txt",
        }
        if suffix not in supported_files:
            raise LicenseAnalysisError(
                "Send CSV, TSV, Excel, Word, PDF, RTF, ODT, or plain text."
            )
        media = await self._whatsapp_client.download_media(
            media_id=document.id,
            filename=document.filename,
            content_type=document.mime_type,
        )
        if len(media.content) > self._configuration.max_document_bytes:
            raise LicenseAnalysisError(
                f"The file exceeds the {self._configuration.max_document_bytes // 1048576} MB limit."
            )
        estate = None
        if suffix in {"csv", "xlsx", "xlsm"}:
            try:
                estate = await (
                    self._orchestrator.append_document(
                        sender=sender,
                        filename=media.filename,
                        content=media.content,
                    )
                    if append_to_draft
                    else self._orchestrator.analyze_document(
                        sender=sender,
                        filename=media.filename,
                        content=media.content,
                    )
                )
            except LicenseAnalysisError:
                if self._requirement_extractor is None:
                    raise
                logger.info(
                    "Deterministic spreadsheet parser could not map the layout; "
                    "using structured multimodal extraction"
                )
        if estate is None:
            extractor = self._require_extractor(
                "This file format or layout requires OpenAI requirement capture."
            )
            captured = await extractor.extract_file(
                media.content,
                filename=media.filename,
                mime_type=media.content_type,
            )
            estate = await self._analyze_captured(
                sender,
                media.filename,
                captured,
                append=append_to_draft,
            )
        await self._process_captured_estate(
            sender,
            estate,
            appended=append_to_draft,
        )

    async def _handle_image(
        self,
        sender: str,
        incoming: IncomingWhatsAppImage,
    ) -> None:
        append_to_draft = await self._has_open_requirement_draft(sender)
        media = await self._whatsapp_client.download_media(
            media_id=incoming.id,
            filename="licensing-requirement-image",
            content_type=incoming.mime_type,
        )
        if len(media.content) > self._configuration.max_image_bytes:
            raise RequirementCaptureError(
                f"The image exceeds the {self._configuration.max_image_bytes // 1048576} MB limit."
            )
        suffix = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "image/gif": ".gif",
        }.get(media.content_type.split(";", 1)[0].casefold(), ".jpg")
        filename = f"licensing-requirement{suffix}"
        extractor = self._require_extractor("Image capture requires OpenAI.")
        captured = await extractor.extract_image(
            media.content,
            filename=filename,
            mime_type=media.content_type,
        )
        estate = await self._analyze_captured(
            sender,
            filename,
            captured,
            append=append_to_draft,
        )
        await self._process_captured_estate(
            sender,
            estate,
            appended=append_to_draft,
        )

    async def _handle_audio(
        self,
        sender: str,
        incoming: IncomingWhatsAppAudio,
    ) -> None:
        append_to_draft = await self._has_open_requirement_draft(sender)
        media = await self._whatsapp_client.download_media(
            media_id=incoming.id,
            filename="licensing-requirement-voice.ogg",
            content_type=incoming.mime_type,
        )
        if len(media.content) > self._configuration.max_audio_bytes:
            raise RequirementCaptureError(
                f"The voice note exceeds the {self._configuration.max_audio_bytes // 1048576} MB limit."
            )
        extractor = self._require_extractor("Voice capture requires OpenAI transcription.")
        captured = await extractor.extract_audio(
            media.content,
            filename=media.filename,
            mime_type=media.content_type,
        )
        if captured.transcript:
            await self._send_text(
                sender,
                "*Voice transcript used for extraction*\n" + captured.transcript[:3000],
            )
        estate = await self._analyze_captured(
            sender,
            media.filename,
            captured,
            append=append_to_draft,
        )
        await self._process_captured_estate(
            sender,
            estate,
            appended=append_to_draft,
        )

    async def _analyze_captured(
        self,
        sender: str,
        source_name: str,
        captured: CapturedRequirement,
        *,
        append: bool = False,
    ):
        if captured.extraction.warnings:
            await self._send_text(
                sender,
                "*Extraction notes*\n"
                + "\n".join(f"- {item}" for item in captured.extraction.warnings[:10]),
            )
        if captured.extraction.needs_clarification:
            partial_context = captured.transcript.strip()
            if not partial_context:
                facts: list[str] = []
                for line in captured.extraction.lines:
                    values = []
                    if line.sku_name.strip():
                        values.append(f"product {line.sku_name.strip()}")
                    if line.quantity > 0:
                        values.append(f"quantity {line.quantity}")
                    if line.term_duration.strip():
                        values.append(f"term {line.term_duration.strip()}")
                    if line.billing_plan.strip():
                        values.append(f"billing {line.billing_plan.strip()}")
                    if values:
                        facts.append(", ".join(values))
                partial_context = "; ".join(facts)
            if partial_context:
                await self._orchestrator.remember_capture_message(
                    sender,
                    partial_context[:2000],
                )
            raise RequirementCaptureError(
                captured.extraction.clarification.strip()
                or "Which product or quantity is still missing?"
            )
        operation = (
            self._orchestrator.append_extracted
            if append
            else self._orchestrator.analyze_extracted
        )
        return await operation(
            sender=sender,
            source_file=source_name,
            rows=captured.extraction.to_parsed_rows(),
            seller_details=[
                SellerProvidedDetail(
                    label=item.label.strip(),
                    value=item.value.strip(),
                )
                for item in captured.extraction.seller_details
                if item.label.strip() and item.value.strip()
            ][:12],
        )

    async def _process_captured_estate(
        self,
        sender: str,
        estate,
        *,
        appended: bool = False,
    ) -> None:
        if appended:
            await self._send_text(
                sender,
                "I added the newly supplied licence information to the current draft. "
                "Review the complete list below; pricing remains paused until you confirm "
                "that the renewal requirement is complete.",
            )
        await self._send_estate_table(sender, estate)
        await self._send_estate_report(sender, estate)
        if estate.pending_lines:
            await self._send_pending_match_requests(sender, estate)
        else:
            await self._continue_after_estate(sender)

    async def _has_open_requirement_draft(self, sender: str) -> bool:
        if self._configuration.workflow_mode != "simple_pricing":
            return False
        session = await self._orchestrator.get_session(sender)
        return bool(
            session is not None
            and session.estate is not None
            and session.confirmed_as_is is None
            and session.stage
            in {
                WorkflowStage.AWAITING_MATCH_CONFIRMATION,
                WorkflowStage.AWAITING_INITIAL_VALIDATION,
                WorkflowStage.AWAITING_SCENARIO,
            }
        )

    async def _capture_typed_requirement(self, sender: str, message: str) -> None:
        extractor = self._require_extractor(
            "Manual natural-language capture requires OpenAI. Send the standard CSV/XLSX "
            "template when language capture is unavailable."
        )
        messages = await self._orchestrator.remember_capture_message(sender, message)
        if len(messages) == 1:
            capture_text = messages[0]
        else:
            capture_text = (
                "The seller supplied these consecutive messages for one licensing "
                "requirement. Combine details across the messages and do not ask again for "
                "a product or quantity already supplied:\n"
                + "\n".join(
                    f"Seller message {index}: {value}"
                    for index, value in enumerate(messages, start=1)
                )
            )
        captured = await extractor.extract_text(
            capture_text,
            source_name="whatsapp-message.txt",
        )
        if captured.extraction.needs_clarification:
            question = (
                captured.extraction.clarification.strip()
                or "Which product or quantity is still missing?"
            )
            supplied_product = next(
                (
                    line.sku_name.strip()
                    for line in captured.extraction.lines
                    if line.sku_name.strip()
                ),
                "",
            )
            supplied_quantity = next(
                (
                    line.quantity
                    for line in captured.extraction.lines
                    if line.quantity > 0
                ),
                0,
            )
            if supplied_product and supplied_quantity:
                acknowledgement = (
                    f"Got it — I’ve retained {supplied_product} and quantity "
                    f"{supplied_quantity:,}. "
                )
            elif supplied_product:
                acknowledgement = f"Got it — I’ve noted {supplied_product}. "
                if "product" in question.casefold() or "sku" in question.casefold():
                    question = "How many licences should I include?"
            elif supplied_quantity:
                acknowledgement = f"Got it — I’ve noted quantity {supplied_quantity:,}. "
                if "quantity" in question.casefold():
                    question = "Which Microsoft product or plan should I use?"
            else:
                acknowledgement = "Got it — I’ve kept the details you already provided. "
            await self._send_text(sender, acknowledgement + question)
            return
        session = await self._orchestrator.get_session(sender)
        append_to_draft = bool(
            session is not None
            and session.estate is not None
            and session.confirmed_as_is is None
        )
        estate = await self._analyze_captured(
            sender,
            "whatsapp-message.txt",
            captured,
            append=append_to_draft,
        )
        await self._process_captured_estate(
            sender,
            estate,
            appended=append_to_draft,
        )

    @staticmethod
    def _requests_fresh_start(reply: str) -> bool:
        return reply in RESET_REQUESTS or any(
            phrase in reply
            for phrase in (
                "start fresh",
                "start from fresh",
                "reset everything",
                "clear everything",
                "new requirement",
            )
        )

    @staticmethod
    def _requests_enterprise_comparison(reply: str) -> bool:
        if "compare" not in reply:
            return False
        compact = " ".join(reply.split())
        if re.search(r"\b(?:all|other|the)?\s*(?:4|four)\b", compact):
            return True
        return all(value in compact for value in ("me3", "me5", "me7"))

    @staticmethod
    def _requests_pending_change_cancel(reply: str) -> bool:
        return reply in CANCEL_REPLIES or any(
            phrase in reply
            for phrase in (
                "cancel the change",
                "cancel that change",
                "do not replace",
                "don't replace",
                "dont replace",
                "not replace",
                "leave it unchanged",
                "keep it unchanged",
            )
        )

    @staticmethod
    def _professional_agent_text(value: str) -> str:
        """Keep model-authored seller copy in professional English."""

        cleaned: list[str] = []
        for character in unicodedata.normalize("NFC", value):
            if ord(character) > 127:
                name = unicodedata.name(character, "")
                category = unicodedata.category(character)
                if category.startswith("L") and "LATIN" not in name:
                    continue
                if category.startswith("M") and "COMBINING" not in name:
                    continue
            cleaned.append(character)
        return " ".join("".join(cleaned).split())

    @staticmethod
    def _scenario_from_request(intent: AgentIntent, message: str | None) -> ScenarioType | None:
        stated = getattr(intent, "scenario", "none")
        if stated != "none":
            return ScenarioType(stated)
        normalized = " ".join((message or "").casefold().replace("-", " ").split())
        matches: list[ScenarioType] = []
        if re.search(r"\bme3\b", normalized):
            matches.append(ScenarioType.ME3_COPILOT)
        if re.search(r"\bme5\b", normalized):
            matches.append(ScenarioType.ME5_COPILOT)
        if re.search(r"\bme7\b", normalized):
            matches.append(ScenarioType.ME7)
        if "renew as is" in normalized or "renewal as is" in normalized:
            matches.append(ScenarioType.RENEW_AS_IS)
        return matches[0] if len(set(matches)) == 1 else None

    async def _select_or_request_scenario_target(
        self,
        sender: str,
        intent: AgentIntent,
        original_message: str | None,
    ) -> bool:
        """Select an explicit option or pause an ambiguous edit after a four-way review."""

        if (
            self._configuration.workflow_mode != "simple_pricing"
            or intent.action not in SCENARIO_EDIT_ACTIONS
        ):
            return False
        session = await self._orchestrator.get_session(sender)
        if session is None or len(session.scenarios) <= 1:
            return False
        target = self._scenario_from_request(intent, original_message)
        if target is not None:
            await self._orchestrator.build_scenario(sender, target)
            return False
        available = ", ".join(item.label for item in ScenarioType if item in session.scenarios)
        question = f"Which proposal should I update: {available}?"
        await self._orchestrator.set_pending_dialogue(
            sender,
            PendingDialogue(
                kind="agent_clarification",
                question=question,
                context_message=(original_message or "")[:2000],
            ),
        )
        await self._send_text(sender, question)
        return True

    @staticmethod
    def _looks_like_requirement_fragment(message: str) -> bool:
        """Conservative fallback when the intent model asks instead of starting capture."""

        value = " ".join(message.casefold().strip(" ?!.,").split())
        if any(
            phrase in value
            for phrase in (
                "how much",
                "what is the price",
                "what does it cost",
                "compare",
                "finalize",
                "what is",
                "what are",
                "who is",
                "who are",
                "which licence did",
                "which license did",
                "where should",
                "why does",
                "explain",
            )
        ):
            return False
        product_markers = (
            "licence",
            "license",
            "sku",
            "subscription",
            "copilot",
            "power bi",
            "microsoft 365",
            "office 365",
            "teams",
            "defender",
            "intune",
            "entra",
            "visio",
            "project plan",
            "dynamics 365",
            "windows 11",
        )
        request_markers = (
            "want",
            "need",
            "require",
            "add",
            "include",
            "also",
            "consider",
            "give me",
            "go with",
            "order",
            "renew",
            "use",
        )
        has_product = any(marker in value for marker in product_markers) or bool(
            re.search(r"\b(?:m?e[1357]|o365|m365)\b", value)
        )
        return has_product and (
            any(marker in value for marker in request_markers)
            or bool(re.search(r"\b\d+\b", value))
            or bool(re.fullmatch(r"(?:m?e[1357]|o365|m365)", value))
        )

    @classmethod
    def _is_clear_non_requirement_turn(cls, message: str) -> bool:
        """Catch obvious questions/small talk before they can contaminate SKU capture."""

        value = " ".join(message.casefold().strip(" ?!.,").split())
        if value in {
            "i like you",
            "i love you",
            "how are you",
            "how is it going",
        }:
            return True
        question = re.match(
            r"^(?:who|what|which|where|when|why|how|did|does|is|are|can|could|would)\b",
            value,
        )
        if not question:
            return False
        specific_product = any(
            marker in value
            for marker in (
                "copilot",
                "power bi",
                "microsoft 365",
                "office 365",
                "teams",
                "defender",
                "intune",
                "entra",
                "visio",
                "project plan",
                "dynamics 365",
                "windows 11",
            )
        ) or bool(re.search(r"\b(?:m?e[1357]|o365|m365)\b", value))
        licensing_action = bool(
            re.search(
                r"\b(?:add|include|replace|remove|change|upgrade|renew|quote|price|"
                r"want|need|require|use|select|compare)\b",
                value,
            )
        )
        # "Which licence did Virat Kohli buy?" contains licensing vocabulary but
        # is not a seller requirement. Conversely, "Can you add ME7?" is a real
        # licensing instruction expressed as a question.
        return not (specific_product and licensing_action)

    @staticmethod
    def _is_gratitude_turn(message: str) -> bool:
        """Recognize a standalone thank-you without consuming a pending licence turn."""

        value = " ".join(message.casefold().strip(" ?!.,").split())
        return bool(
            re.fullmatch(
                r"(?:ok(?:ay)?\s+)?(?:thanks?|thank\s+you|thankyou|thanx|thx|"
                r"thaks?)(?:\s+(?:so much|very much|bro|sir))?",
                value,
            )
        )

    async def _send_gratitude_reply(
        self,
        sender: str,
        session: WorkflowSession | None,
    ) -> None:
        if session is not None and session.capture_messages:
            response = (
                "You’re welcome. I’ve kept the unfinished licence details. Continue with "
                "the missing product or quantity whenever you are ready."
            )
        elif session is not None and session.estate is not None:
            response = (
                "You’re welcome. Your current licensing draft remains saved and unchanged. "
                "Continue whenever you are ready."
            )
        else:
            response = (
                "You’re welcome. Send a Microsoft licensing requirement whenever you are "
                "ready."
            )
        await self._send_text(sender, response)

    async def _send_non_requirement_boundary(self, sender: str) -> None:
        await self._send_text(
            sender,
            "I have not added that message to the licensing requirement. I can help with "
            "Microsoft licence capture, SKU clarification, annual pricing, proposal changes, "
            "and comparisons. Continue with the missing product or quantity whenever you are "
            "ready.",
        )

    @classmethod
    def _is_explicit_title_choice(cls, message: str) -> bool:
        """Require selection language before accepting a title embedded in a sentence."""

        if cls._is_clear_non_requirement_turn(message):
            return False
        value = " ".join(message.casefold().strip(" ?!.,").split())
        return bool(
            re.search(
                r"\b(?:choose|chosen|select|selected|use|confirm|confirmed|"
                r"go with|pick|picked|that is|this is)\b",
                value,
            )
        )

    async def _interpret_pending_message(
        self,
        message: str,
        session: WorkflowSession,
    ) -> AgentIntent | None:
        """Interpret a turn that may interrupt, answer, or replace a pending request.

        Deterministic option-number and exact-title confirmation is always attempted before
        this method. The language model may classify the seller's new intent, but it never
        selects a catalogue SKU or mutates commercial state.
        """

        if self._intent_interpreter is None:
            return None
        try:
            return await self._intent_interpreter.interpret(message, session)
        except IntentInterpretationError:
            logger.warning("Pending-turn intent interpretation failed")
            return None

    async def _handle_capture_interruption(
        self,
        sender: str,
        message: str,
        session: WorkflowSession,
    ) -> bool:
        """Let a seller interrupt an incomplete multi-turn capture without losing it."""

        intent = await self._interpret_pending_message(message, session)
        if intent is None:
            return False
        if self._is_clear_non_requirement_turn(message):
            await self._send_non_requirement_boundary(sender)
            return True
        if self._looks_like_requirement_fragment(message):
            return False
        if intent.action == "capture_requirement":
            return False
        if intent.action == "clarify":
            if self._looks_like_requirement_fragment(message) or re.fullmatch(
                r"(?:(?:about|around|approximately|approx|maybe)\s+)?"
                r"\d+\s*(?:(?:licence|license)s?|users?|seats?|quantity)?",
                " ".join(message.casefold().strip(" ?!.,").split()),
            ):
                return False
            clarification = self._professional_agent_text(intent.clarification)
            await self._send_text(
                sender,
                clarification[:500]
                if clarification
                else (
                    "I can continue the unfinished licence requirement when you are ready. "
                    "Send the missing product or quantity, or ask another licensing question."
                ),
            )
            return True
        await self._execute_agent_intent(
            sender,
            intent,
            original_message=message,
        )
        return True

    async def _extract_single_turn_requirement(
        self,
        message: str,
    ) -> tuple[str, int, str] | None:
        """Return one explicitly supplied product, quantity, and clarification question."""

        if self._requirement_extractor is None:
            return None
        captured = await self._requirement_extractor.extract_text(
            message,
            source_name="whatsapp-message.txt",
        )
        if len(captured.extraction.lines) != 1:
            return None
        line = captured.extraction.lines[0]
        return (
            line.sku_name.strip(),
            line.quantity,
            captured.extraction.clarification.strip(),
        )

    @staticmethod
    def _explicitly_adds_another_line(message: str, intent: AgentIntent) -> bool:
        if intent.action == "add_sku":
            return True
        value = " ".join(message.casefold().strip(" ?!.,").split())
        return bool(
            re.search(
                r"\b(?:add|also add|include|also include|another|new sku|new licence|new license)\b",
                value,
            )
        )

    async def _apply_revised_pending_requirement(
        self,
        sender: str,
        message: str,
        session: WorkflowSession,
        intent: AgentIntent,
    ) -> bool:
        """Apply a complete new product statement instead of replaying stale choices."""

        if session.estate is None or not session.estate.pending_lines:
            return False
        extracted = await self._extract_single_turn_requirement(message)
        product = str(getattr(intent, "product_query", "") or "").strip()
        quantity = int(getattr(intent, "quantity", -1) or -1)
        clarification = ""
        if extracted is not None:
            extracted_product, extracted_quantity, clarification = extracted
            product = product or extracted_product
            if quantity <= 0:
                quantity = extracted_quantity
        if not product:
            return False

        add_another = self._explicitly_adds_another_line(message, intent)
        if add_another and quantity <= 0:
            await self._send_text(
                sender,
                clarification
                or f"How many {product} licences should I add to the requirement?",
            )
            return True

        target = session.estate.pending_lines[0]
        if quantity <= 0:
            quantity = target.renewal_quantity
        if add_another:
            result = await self._orchestrator.add_requirement_sku(
                sender,
                product,
                quantity,
            )
        else:
            result = await self._orchestrator.replace_requirement_sku(
                sender,
                target.line_id,
                product,
                quantity,
            )
        await self._send_sku_change_result(sender, result)
        return True

    async def _supersede_pending_sku_change(
        self,
        sender: str,
        message: str,
        pending,
        intent: AgentIntent,
    ) -> bool:
        """Replace an unconfirmed add/replace request with the seller's newer wording."""

        extracted = await self._extract_single_turn_requirement(message)
        product = str(getattr(intent, "product_query", "") or "").strip()
        quantity = int(getattr(intent, "quantity", -1) or -1)
        if extracted is not None:
            extracted_product, extracted_quantity, _clarification = extracted
            product = product or extracted_product
            if quantity <= 0:
                quantity = extracted_quantity
        if not product:
            return False
        if quantity <= 0:
            quantity = pending.quantity

        if pending.scope == "requirement":
            if pending.action == "add":
                result = await self._orchestrator.add_requirement_sku(
                    sender, product, quantity
                )
            else:
                result = await self._orchestrator.replace_requirement_sku(
                    sender,
                    pending.source_line_id or "",
                    product,
                    quantity,
                )
        elif pending.action == "add":
            result = await self._orchestrator.add_sku(sender, product, quantity)
        else:
            result = await self._orchestrator.replace_sku(
                sender,
                pending.source_line_id or "",
                product,
                quantity,
            )
        await self._send_sku_change_result(sender, result)
        return True

    async def _show_saved_session_choice(
        self,
        sender: str,
        session: WorkflowSession,
    ) -> None:
        assert session.estate is not None
        question = (
            "Would you like to resume this saved draft, or start a fresh requirement?"
        )
        await self._orchestrator.set_pending_dialogue(
            sender,
            PendingDialogue(
                kind="resume_session",
                question=question,
            ),
        )
        await self._send_text(
            sender,
            "*SkySecure Microsoft Licensing Advisor*\n\n"
            "Welcome back. I found an active saved draft containing "
            f"{len(session.estate.lines)} SKU line(s) and "
            f"{session.estate.total_renewal_quantity:,} licences. I have not added, removed, "
            "or repriced anything.\n\n"
            f"{question} Reply *Resume* or *Start fresh*.",
        )

    async def _resume_saved_session(
        self,
        sender: str,
        session: WorkflowSession,
    ) -> None:
        assert session.estate is not None
        await self._orchestrator.clear_pending_dialogue(sender)
        await self._send_text(
            sender,
            "Saved draft resumed. Review the complete requirement below before continuing.",
        )
        await self._send_estate_table(sender, session.estate)
        if session.estate.pending_lines:
            await self._send_pending_match_requests(sender, session.estate)
            return
        if session.confirmed_as_is is None:
            await self._continue_after_estate(sender)
            return
        await self._send_text(
            sender,
            "The Renew As-Is baseline is already confirmed. Describe the next change, request "
            "a four-option comparison, or ask me to finalize the active proposal.",
        )

    async def _resolve_pending_dialogue(
        self,
        sender: str,
        message: str,
        session: WorkflowSession,
    ) -> bool:
        pending = session.pending_dialogue
        if pending is None:
            return False
        reply = " ".join(message.casefold().strip(" ?!.,").split())
        if pending.kind == "resume_session":
            if reply in RESUME_REQUESTS:
                await self._resume_saved_session(sender, session)
                return True
            intent = await self._interpret_pending_message(message, session)
            if intent is not None and intent.action in {
                "help",
                "answer_question",
                "out_of_scope",
            }:
                await self._execute_agent_intent(
                    sender,
                    intent,
                    original_message=message,
                )
                return True
            await self._send_text(
                sender,
                "I found an existing saved draft and will not merge a new requirement into it "
                "without your approval. Reply *Resume* to continue it or *Start fresh* to "
                "clear it and begin again.",
            )
            return True
        if reply in CANCEL_REPLIES:
            await self._orchestrator.clear_pending_dialogue(sender)
            await self._send_text(
                sender,
                "Okay — I cancelled that question. The saved requirement and proposal are "
                "unchanged. What would you like to do next?",
            )
            return True
        if self._intent_interpreter is None:
            await self._send_text(sender, pending.question)
            return True

        seller_context = "\n".join(
            value
            for value in (pending.context_message.strip(), message.strip())
            if value
        )
        interpretation_input = (
            "Resolve the seller's latest answer in the context of the preceding exchange.\n"
            f"Earlier seller request: {pending.context_message or '(none)'}\n"
            f"Advisor question: {pending.question}\n"
            f"Seller answer: {message}"
        )
        try:
            intent = await self._intent_interpreter.interpret(
                interpretation_input,
                session,
            )
        except IntentInterpretationError:
            await self._send_text(sender, pending.question)
            return True

        if intent.action == "confirm_validation":
            if (
                session.stage == WorkflowStage.AWAITING_INITIAL_VALIDATION
                and session.estate is not None
                and not session.estate.pending_lines
                and not session.capture_messages
                and self._pending_confirms_complete_requirement(pending)
            ):
                await self._orchestrator.clear_pending_dialogue(sender)
                await self._confirm_validation(sender)
                return True
            await self._send_text(
                sender,
                "That reply relates to the pending question, so I have not confirmed the "
                f"complete requirement. {pending.question}",
            )
            return True
        if intent.action in {"help", "answer_question", "out_of_scope"}:
            await self._execute_agent_intent(
                sender,
                intent,
                original_message=message,
            )
            return True
        await self._orchestrator.clear_pending_dialogue(sender)
        if intent.action == "capture_requirement" and seller_context:
            if pending.context_message.strip():
                await self._orchestrator.remember_capture_message(
                    sender,
                    pending.context_message,
                )
            await self._capture_typed_requirement(sender, message)
            return True
        await self._execute_agent_intent(
            sender,
            intent,
            original_message=seller_context or message,
        )
        return True

    @staticmethod
    def _is_requirement_confirmation_reply(message: str) -> bool:
        reply = " ".join(message.casefold().strip(" ?!.,").split())
        if reply in AFFIRMATIVE_REPLIES or reply in REQUIREMENT_CONFIRMATION_REPLIES:
            return True
        negative_markers = {
            "cancel",
            "change",
            "don't",
            "dont",
            "incorrect",
            "no",
            "not",
            "remove",
            "wrong",
        }
        words = set(reply.split())
        if words & negative_markers:
            return False
        return any(
            marker in words
            for marker in {"approve", "approved", "confirm", "confirmed", "correct"}
        )

    @staticmethod
    def _pending_confirms_complete_requirement(pending: PendingDialogue) -> bool:
        if pending.kind != "agent_clarification":
            return False
        question = " ".join(pending.question.casefold().split())
        identifies_requirement = any(
            marker in question
            for marker in {
                "captured requirement",
                "complete requirement",
                "current requirement",
                "requirement for",
            }
        )
        requests_approval = any(
            marker in question
            for marker in {"confirm", "correct", "approve"}
        )
        return identifies_requirement and requests_approval

    async def _try_handle_pending_requirement_match(
        self,
        sender: str,
        message: str,
        session: WorkflowSession,
    ) -> bool:
        if session.estate is None or not session.estate.pending_lines:
            return False
        pending_lines = session.estate.pending_lines
        reply = " ".join(message.casefold().strip(" ?!.,").split())

        remove_line = re.fullmatch(
            r"(?:please\s+)?remove(?:\s+that)?\s+(l\d+)(?:\s+.*)?",
            reply,
        )
        if remove_line is not None:
            await self._remove_requirement_line(sender, remove_line.group(1))
            return True
        if reply in UNCERTAIN_REPLIES:
            await self._send_uncertain_requirement_match(sender, pending_lines[0])
            return True
        if reply in CANCEL_REPLIES:
            if len(pending_lines) == 1:
                await self._remove_requirement_line(sender, pending_lines[0].line_id)
            else:
                await self._send_text(
                    sender,
                    "Which pending line should I remove? Reply with the line ID shown in "
                    "the requirement table.",
                )
            return True

        line_id, candidate_number = self._candidate_selection_from_reply(
            message,
            pending_lines,
        )
        selected_line = next(
            (line for line in pending_lines if line.line_id == line_id),
            None,
        )
        if (
            selected_line is not None
            and candidate_number is not None
            and 1 <= candidate_number <= len(selected_line.candidates)
        ):
            estate = await self._confirm_requirement_candidate(
                sender,
                line_id=line_id,
                candidate_number=candidate_number,
            )
            await self._after_requirement_match_confirmation(sender, estate)
            return True

        normalized_message = normalize_product_title(message)
        title_matches = [
            (line, index)
            for line in pending_lines
            for index, candidate in enumerate(line.candidates, start=1)
            if (
                normalized_message == normalize_product_title(candidate.sku_title)
                or (
                    normalize_product_title(candidate.sku_title) in normalized_message
                    and self._is_explicit_title_choice(message)
                )
            )
        ]
        if len(title_matches) == 1:
            line, index = title_matches[0]
            estate = await self._confirm_requirement_candidate(
                sender,
                line_id=line.line_id,
                candidate_number=index,
            )
            await self._after_requirement_match_confirmation(sender, estate)
            return True

        if (
            reply in AFFIRMATIVE_REPLIES
            and len(pending_lines) == 1
            and len(pending_lines[0].candidates) == 1
        ):
            estate = await self._confirm_requirement_candidate(
                sender,
                line_id=pending_lines[0].line_id,
                candidate_number=1,
            )
            await self._after_requirement_match_confirmation(sender, estate)
            return True

        intent = await self._interpret_pending_message(message, session)
        if intent is not None:
            if intent.action in {
                "help",
                "answer_question",
                "out_of_scope",
                "reset_requirement",
            }:
                await self._execute_agent_intent(
                    sender,
                    intent,
                    original_message=message,
                )
                return True
            if intent.action in {
                "capture_requirement",
                "add_sku",
                "replace_sku",
            } or self._looks_like_requirement_fragment(message):
                if self._is_clear_non_requirement_turn(message):
                    await self._send_non_requirement_boundary(sender)
                    return True
                if await self._apply_revised_pending_requirement(
                    sender,
                    message,
                    session,
                    intent,
                ):
                    return True
            elif intent.action != "clarify":
                await self._execute_agent_intent(
                    sender,
                    intent,
                    original_message=message,
                )
                return True

        if len(pending_lines) == 1:
            source = normalize_product_title(pending_lines[0].source_product_title)
            quantity_only = re.fullmatch(
                r"(?:(?:about|around|approximately|approx|maybe)\s+)?"
                r"\d+\s*(?:(?:licence|license)s?|users?|seats?|quantity)?",
                reply,
            )
            if (
                quantity_only is not None
                or (source and source in normalized_message)
            ):
                await self._send_uncertain_requirement_match(sender, pending_lines[0])
                return True
        # An unresolved catalogue choice is a hard workflow gate. Do not pass free
        # text to the intent model, where a plan number such as "Plan 2" could be
        # mistaken for "Option 2". Keep showing the complete deterministic choices
        # until the seller makes an explicit numbered, interactive, or exact-title
        # selection.
        if len(pending_lines) == 1:
            await self._send_uncertain_requirement_match(sender, pending_lines[0])
        else:
            await self._send_text(
                sender,
                "Please identify the pending line first, then choose one of its exact "
                "catalogue products. No product has been selected.",
            )
            await self._send_pending_match_requests(sender, session.estate)
        return True

    async def _try_handle_pending_sku_change_reply(
        self,
        sender: str,
        message: str,
        session: WorkflowSession,
    ) -> bool:
        pending = session.pending_sku_change
        if pending is None:
            return False
        reply = " ".join(message.casefold().strip(" ?!.,").split())
        if self._requests_pending_change_cancel(reply):
            await self._orchestrator.cancel_sku_change(sender)
            await self._send_text(
                sender,
                "Okay — I cancelled that product change. The proposal remains unchanged.",
            )
            if self._requests_enterprise_comparison(reply):
                await self._send_enterprise_comparison(sender)
            elif "compare" in reply or "comparison" in reply:
                question = (
                    "Which options should I compare with Renew As-Is: ME3, ME5, ME7, "
                    "or all three?"
                )
                await self._orchestrator.set_pending_dialogue(
                    sender,
                    PendingDialogue(
                        kind="agent_clarification",
                        question=question,
                        context_message="Compare the approved Renew As-Is proposal.",
                    ),
                )
                await self._send_text(sender, question)
            return True
        if reply in UNCERTAIN_REPLIES:
            await self._send_text(
                sender,
                "No problem — I will not guess. Review the exact product names below. If "
                "none is familiar, send the invoice name, a screenshot, or the business "
                "capability you need and I will help narrow it down.",
            )
            await self._send_sku_change_result(
                sender,
                SkuChangeResult(
                    state="confirmation_required",
                    confirmation=pending,
                ),
            )
            return True

        number = self._single_candidate_number(message)
        normalized_message = normalize_product_title(message)
        title_numbers = [
            index
            for index, candidate in enumerate(pending.candidates, start=1)
            if (
                normalized_message == normalize_product_title(candidate.sku_title)
                or (
                    normalize_product_title(candidate.sku_title) in normalized_message
                    and self._is_explicit_title_choice(message)
                )
            )
        ]
        if len(title_numbers) == 1:
            number = title_numbers[0]
        elif reply in AFFIRMATIVE_REPLIES and len(pending.candidates) == 1:
            number = 1
        if number is None or number < 1 or number > len(pending.candidates):
            intent = await self._interpret_pending_message(message, session)
            if intent is not None:
                if intent.action in {
                    "help",
                    "answer_question",
                    "out_of_scope",
                    "reset_requirement",
                }:
                    await self._execute_agent_intent(
                        sender,
                        intent,
                        original_message=message,
                    )
                    return True
                if intent.action in {
                    "capture_requirement",
                    "add_sku",
                    "replace_sku",
                } or self._looks_like_requirement_fragment(message):
                    if self._is_clear_non_requirement_turn(message):
                        await self._send_non_requirement_boundary(sender)
                        return True
                    if await self._supersede_pending_sku_change(
                        sender,
                        message,
                        pending,
                        intent,
                    ):
                        return True
                elif intent.action != "clarify":
                    await self._orchestrator.cancel_sku_change(sender)
                    await self._execute_agent_intent(
                        sender,
                        intent,
                        original_message=message,
                    )
                    return True
            # The deterministic gate remains the final fallback. A later language-model
            # pass must never turn the "2" in a plan name into option 2.
            await self._send_sku_change_result(
                sender,
                SkuChangeResult(
                    state="confirmation_required",
                    confirmation=pending,
                ),
            )
            return True
        result = await self._orchestrator.confirm_sku_change(sender, number)
        await self._send_sku_change_result(sender, result)
        return True

    @staticmethod
    def _single_candidate_number(message: str) -> int | None:
        reply = " ".join(message.casefold().strip(" ?!.,").split())
        match = re.fullmatch(
            r"(?:(?:choose|select|use)\s+)?(?:option\s+|number\s+)?(\d+)",
            reply,
        )
        return int(match.group(1)) if match else None

    def _candidate_selection_from_reply(
        self,
        message: str,
        pending_lines: list,
    ) -> tuple[str | None, int | None]:
        reply = " ".join(message.casefold().strip(" ?!.,").split())
        line_match = re.search(r"\b(l\d+)\b", reply)
        line_id = line_match.group(1).upper() if line_match else None
        number_match = re.search(
            r"(?:option|number|choose|select|use)\s*(\d+)\b",
            reply,
        )
        number = int(number_match.group(1)) if number_match else None
        if number is None:
            bare = self._single_candidate_number(message)
            if (
                bare is not None
                and len(pending_lines) == 1
                and bare <= len(pending_lines[0].candidates)
            ):
                number = bare
        if line_id is None and len(pending_lines) == 1:
            line_id = pending_lines[0].line_id
        if line_id is not None and number is None and line_match is not None:
            remainder = reply[line_match.end() :]
            trailing = re.search(r"\b(\d+)\b", remainder)
            if trailing:
                number = int(trailing.group(1))
        return line_id, number

    async def _confirm_requirement_candidate(
        self,
        sender: str,
        *,
        line_id: str,
        candidate_number: int,
    ) -> LicenseEstate:
        session = await self._orchestrator.get_session(sender)
        if session is None or session.estate is None:
            raise ValueError("There is no requirement awaiting product confirmation.")
        line = next(
            (
                item
                for item in session.estate.pending_lines
                if item.line_id == line_id.upper()
            ),
            None,
        )
        if line is None:
            raise ValueError(f"{line_id.upper()} is not awaiting product confirmation.")
        index = candidate_number - 1
        if index < 0 or index >= len(line.candidates):
            raise ValueError(
                f"Choose an option from 1 to {len(line.candidates)} for {line.line_id}."
            )
        candidate = line.candidates[index]
        return await self._orchestrator.confirm_matches(
            sender,
            {line.line_id: (candidate.product_id, candidate.sku_id)},
        )

    async def _after_requirement_match_confirmation(
        self,
        sender: str,
        estate: LicenseEstate,
    ) -> None:
        if estate.pending_lines:
            await self._send_text(
                sender,
                "Thank you — that product is confirmed. Let’s resolve the next unclear "
                "line; all quantities and other captured details remain unchanged.",
            )
            await self._send_pending_match_requests(sender, estate)
            return
        await self._send_text(
            sender,
            "Thank you — the exact product is confirmed. I retained the quantity and other "
            "details you already supplied.",
        )
        await self._send_estate_table(sender, estate)
        await self._send_estate_report(sender, estate)
        await self._continue_after_estate(sender)

    async def _send_pending_match_requests(
        self,
        sender: str,
        estate: LicenseEstate,
    ) -> None:
        await self._send_text_chunks(sender, format_pending_matches(estate))
        await self._send_pending_match_lists(sender, estate.pending_lines)

    async def _send_uncertain_requirement_match(self, sender: str, line) -> None:
        if len(line.candidates) == 1:
            candidate = line.candidates[0]
            await self._send_text(
                sender,
                "No problem — I can help. Based on the wording you supplied, the only "
                f"close catalogue match is *{format_sku_candidate(candidate)}*. I will not "
                "select it "
                f"without your approval. Shall I use it for {line.renewal_quantity:,} "
                "licences? If not, send the product name from the invoice or a screenshot.",
            )
        else:
            options = "\n".join(
                f"{index}. {format_sku_candidate(candidate)}"
                for index, candidate in enumerate(line.candidates, start=1)
            )
            await self._send_text(
                sender,
                "No problem — I will help narrow it down. These are the actual available "
                f"matches for {line.line_id}:\n{options}\n\nTell me which product family "
                "appears on the customer’s invoice. If that is unavailable, send a "
                "screenshot or describe the required business capability.",
            )
        if line.candidates:
            await self._send_pending_match_lists(sender, [line])

    async def _send_pending_match_lists(
        self,
        sender: str,
        pending_lines: list,
    ) -> None:
        for line in pending_lines:
            total = len(line.candidates)
            for offset in range(0, total, 10):
                page = line.candidates[offset : offset + 10]
                rows = [
                    InteractiveRow(
                        id=(
                            f"licensing|match_confirm|{line.line_id}|"
                            f"{offset + page_index}"
                        ),
                        title=f"{line.line_id} · Option {offset + page_index}",
                        description=self._candidate_row_description(candidate),
                    )
                    for page_index, candidate in enumerate(page, start=1)
                ]
                first = offset + 1
                last = offset + len(page)
                await self._send_interactive(
                    sender,
                    InteractiveList(
                        body=InteractiveText(
                            text=(
                                f"Select the exact product for {line.line_id}. "
                                f"Options {first}-{last} of {total}; full names are shown "
                                "in the preceding message."
                            )
                        ),
                        action=InteractiveListAction(
                            button="Choose product",
                            sections=[
                                InteractiveSection(
                                    title=f"Options {first}-{last}",
                                    rows=rows,
                                )
                            ],
                        ),
                    ),
                )

    async def _remove_requirement_line(self, sender: str, line_id: str) -> None:
        normalized_id = line_id.strip().upper()
        session = await self._orchestrator.get_session(sender)
        if session is None or session.estate is None:
            raise ValueError("There is no captured requirement to edit.")
        line = next(
            (item for item in session.estate.lines if item.line_id == normalized_id),
            None,
        )
        if line is None:
            raise ValueError(f"I could not find {normalized_id} in the current requirement.")
        if len(session.estate.lines) == 1:
            await self._orchestrator.reset_session(sender)
            await self._send_text(
                sender,
                f"Removed {line.line_id} — {line.display_title}. The requirement draft is "
                "now empty. What licence would you like to start with?",
            )
            return
        estate = await self._orchestrator.remove_requirement_line(sender, normalized_id)
        await self._send_updated_requirement(sender, estate)

    def _require_extractor(self, message: str) -> RequirementExtractor:
        if self._requirement_extractor is None:
            raise RequirementCaptureError(message)
        return self._requirement_extractor

    async def _send_estate_report(self, sender: str, estate) -> None:
        comparison_mode = self._configuration.workflow_mode == "scenario_comparison"
        simple_mode = self._configuration.workflow_mode == "simple_pricing"
        pdf = render_estate_pdf(
            estate,
            include_migration_review=comparison_mode,
            report_title=(
                "Captured Licensing Requirement"
                if simple_mode
                else "Customer Licence Estate"
            ),
        )
        try:
            await self._whatsapp_client.send_document(
                to=sender,
                content=pdf,
                filename=(
                    "captured-licensing-requirement.pdf"
                    if simple_mode
                    else "customer-licence-estate.pdf"
                ),
                content_type="application/pdf",
                caption=(
                    (
                        "Captured licensing requirement grouped by product family, with "
                        "term, billing, and "
                        if simple_mode
                        else "Customer licence estate grouped by product family, with "
                        "expiry and "
                    )
                    + ("migration-review flags" if comparison_mode else "SKU-match flags")
                ),
            )
        except WhatsAppAPIError as error:
            logger.error(
                "Unable to send estate PDF status=%s network_error=%s",
                error.status_code,
                error.network_error,
            )
            raise

    async def _send_estate_table(self, sender: str, estate) -> None:
        try:
            simple_mode = self._configuration.workflow_mode == "simple_pricing"
            images = render_estate_table_images(
                estate,
                title=(
                    "Captured licensing requirement"
                    if simple_mode
                    else "Customer licence estate"
                ),
            )
            for index, content in enumerate(images, start=1):
                await self._whatsapp_client.send_image(
                    to=sender,
                    content=content,
                    filename=f"licence-estate-table-{index}.png",
                    content_type="image/png",
                    caption=(
                        f"{'Requirement' if simple_mode else 'Licence estate'} table • "
                        f"{len(estate.lines)} SKU lines • "
                        f"Page {index}/{len(images)}"
                    ),
                )
            return
        except Exception:
            logger.exception("Unable to render or send estate table image; using text fallback")
        await self._send_text_chunks(
            sender,
            format_estate(
                estate,
                include_migration_review=(
                    self._configuration.workflow_mode == "scenario_comparison"
                ),
            ),
            limit=RESPONSIVE_MESSAGE_LIMIT,
        )

    async def _handle_text(self, sender: str, body: str) -> None:
        command = body.strip()
        if not command:
            await self._send_text(sender, "What licensing requirement would you like to review?")
            return
        lowered = command.casefold()
        intro_request = " ".join(lowered.strip(" ?!.,").split())
        session = await self._orchestrator.get_session(sender)
        if self._is_gratitude_turn(command):
            await self._send_gratitude_reply(sender, session)
            return
        if lowered in {"/help", "/start", "/about", "/analyze"} or intro_request in {
            "hi",
            "hello",
            "hey",
            "help",
            "start",
            "good morning",
            "good afternoon",
            "good evening",
            "what do you do",
            "what you do",
            "what does this agent do",
            "what can you do",
            "who are you",
            "how can you help",
            "tell me about yourself",
            "how do you work",
            "how does this work",
        }:
            if (
                self._configuration.workflow_mode == "simple_pricing"
                and session is not None
                and session.estate is not None
            ):
                await self._show_saved_session_choice(sender, session)
            else:
                await self._send_text(sender, HELP_TEXT)
            return
        if self._requests_fresh_start(intro_request):
            await self._orchestrator.reset_session(sender)
            await self._send_text(
                sender,
                "Done — I cleared the previous draft. Send the first licence and quantity "
                "when you are ready; you can provide them together or one detail at a time.",
            )
            return
        if self._requests_monthly_billing(lowered):
            await self._send_text(
                sender,
                "Monthly billing is not available in this release. Every proposal uses "
                "a one-year term with annual billing.",
            )
            return
        if (
            self._configuration.workflow_mode == "simple_pricing"
            and any(term in lowered for term in SIMPLE_PRICING_RESTRICTED_TERMS)
        ):
            await self._send_text(
                sender,
                "That pricing request was not applied. Discounts, promotions, margins, and "
                "manual commercial adjustments are not seller-editable in this release; the "
                "proposal remains unchanged.",
            )
            return
        if self._requests_restricted_pricing(lowered):
            await self._send_text(
                sender,
                "That pricing request is not applied in this release. The saved proposal "
                "remains unchanged.",
            )
            return
        session = await self._orchestrator.get_session(sender)
        if self._configuration.workflow_mode == "simple_pricing" and session is not None:
            if (
                session.pending_dialogue is not None
                and session.pending_dialogue.kind == "resume_session"
                and await self._resolve_pending_dialogue(sender, command, session)
            ):
                return
            if await self._try_handle_pending_sku_change_reply(
                sender,
                command,
                session,
            ):
                return
            if await self._try_handle_pending_requirement_match(
                sender,
                command,
                session,
            ):
                return
            if (
                session.capture_messages
                and intro_request in CANCEL_REPLIES
            ):
                if session.estate is None:
                    await self._orchestrator.reset_session(sender)
                    response = (
                        "Okay — I cleared the incomplete requirement. What licence would "
                        "you like to start with?"
                    )
                else:
                    await self._orchestrator.clear_capture_messages(sender)
                    response = (
                        "Okay — I cancelled that incomplete addition. The licences already "
                        "shown in your draft are unchanged."
                    )
                await self._send_text(sender, response)
                return
            if (
                session.capture_messages
                and not lowered.startswith("/")
            ):
                if await self._handle_capture_interruption(
                    sender,
                    command,
                    session,
                ):
                    return
                await self._capture_typed_requirement(sender, command)
                return
            if (
                session.stage == WorkflowStage.AWAITING_INITIAL_VALIDATION
                and session.estate is not None
                and not session.estate.pending_lines
                and not session.capture_messages
                and self._is_requirement_confirmation_reply(command)
                and (
                    session.pending_dialogue is None
                    or self._pending_confirms_complete_requirement(
                        session.pending_dialogue
                    )
                )
            ):
                if session.pending_dialogue is not None:
                    await self._orchestrator.clear_pending_dialogue(sender)
                await self._confirm_validation(sender)
                return
            if await self._resolve_pending_dialogue(sender, command, session):
                return
            if self._requests_enterprise_comparison(intro_request):
                await self._ensure_operation_allowed(
                    sender,
                    agent_action="compare_enterprise_options",
                )
                await self._send_enterprise_comparison(sender)
                return
        if (
            self._configuration.workflow_mode == "simple_pricing"
            and not lowered.startswith("/")
        ):
            if session is None or session.estate is None:
                if (
                    self._looks_like_requirement_fragment(command)
                    and not re.match(
                        r"^(?:who|what|which|where|when|why|how|did|does|is|are|"
                        r"can|could|would)\b",
                        intro_request,
                    )
                ):
                    await self._capture_typed_requirement(sender, command)
                    return
                if self._intent_interpreter is not None:
                    try:
                        intent = await self._intent_interpreter.interpret(command, session)
                    except IntentInterpretationError:
                        logger.warning("Pre-upload intent interpretation failed")
                        await self._send_text(
                            sender,
                            "I could not determine whether that is a licensing requirement "
                            "or a question. Are you providing SKUs and quantities, or asking "
                            "about the process?",
                        )
                        return
                    if intent.action != "capture_requirement":
                        await self._execute_agent_intent(
                            sender,
                            intent,
                            original_message=command,
                        )
                        return
                    if self._is_clear_non_requirement_turn(command):
                        await self._send_non_requirement_boundary(sender)
                        return
                await self._capture_typed_requirement(sender, command)
                return
        if lowered in {"/validate", "/confirm-details"}:
            await self._confirm_validation(sender)
            return
        if lowered == "/confirm-finalize":
            await self._confirm_finalization_and_send(sender)
            return
        if lowered == "/cancel-finalize":
            cancelled = await self._orchestrator.cancel_finalization(sender)
            await self._send_text(
                sender,
                "Finalization cancelled. Continue editing the proposal."
                if cancelled
                else "There is no finalization awaiting confirmation.",
            )
            return
        if lowered.startswith("/confirm "):
            selections = self._parse_confirmations(command[9:])
            estate = await self._orchestrator.confirm_matches(sender, selections)
            await self._after_requirement_match_confirmation(sender, estate)
            return
        if lowered.startswith("/confirm-sku "):
            value = command[13:].strip()
            if not value.isdigit():
                raise ValueError("Use /confirm-sku NUMBER.")
            result = await self._orchestrator.confirm_sku_change(sender, int(value))
            await self._send_sku_change_result(sender, result)
            return
        if lowered == "/cancel-sku":
            cancelled = await self._orchestrator.cancel_sku_change(sender)
            await self._send_text(
                sender,
                "Pending SKU change cancelled."
                if cancelled
                else "There is no pending SKU change to cancel.",
            )
            return
        session = await self._orchestrator.get_session(sender)
        if (
            self._configuration.workflow_mode == "simple_pricing"
            and session is not None
            and session.stage
            in {
                WorkflowStage.AWAITING_INITIAL_VALIDATION,
                WorkflowStage.AWAITING_MATCH_CONFIRMATION,
            }
        ):
            if lowered.startswith("/set "):
                line_id, quantity = self._line_quantity(command[5:])
                estate = await self._orchestrator.edit_requirement_quantity(
                    sender, line_id, quantity
                )
                await self._send_updated_requirement(sender, estate)
                return
        if self._configuration.workflow_mode == "simple_pricing" and lowered.startswith(
            ("/promo ", "/discount ", "/adjust ")
        ):
            raise ValueError(
                "Discount, margin, adjustment, and promotion controls are not seller-facing "
                "in this release."
            )
        if (
            self._configuration.workflow_mode == "simple_pricing"
            and session is not None
            and session.stage
            in {
                WorkflowStage.AWAITING_INITIAL_VALIDATION,
                WorkflowStage.AWAITING_MATCH_CONFIRMATION,
            }
        ):
            if lowered.startswith("/add "):
                product, quantity = self._product_quantity(command[5:])
                result = await self._orchestrator.add_requirement_sku(
                    sender, product, quantity
                )
                await self._send_sku_change_result(sender, result)
                return
            if lowered.startswith("/replace "):
                line_id, product, quantity = self._replacement(command[9:])
                result = await self._orchestrator.replace_requirement_sku(
                    sender, line_id, product, quantity
                )
                await self._send_sku_change_result(sender, result)
                return
            if lowered.startswith("/remove "):
                line_id = command[8:].strip()
                await self._remove_requirement_line(sender, line_id)
                return
            if lowered.startswith("/term "):
                term_duration = self._required_text(command[6:], "contract term")
                self._validate_annual_contract(term_duration=term_duration)
                estate = await self._orchestrator.edit_requirement_contract(
                    sender,
                    term_duration=term_duration,
                )
                await self._send_updated_requirement(sender, estate)
                return
            if lowered.startswith("/billing "):
                billing_plan = self._required_text(command[9:], "billing plan")
                self._validate_annual_contract(billing_plan=billing_plan)
                estate = await self._orchestrator.edit_requirement_contract(
                    sender,
                    billing_plan=billing_plan,
                )
                await self._send_updated_requirement(sender, estate)
                return
        if lowered.startswith("/"):
            await self._ensure_operation_allowed(sender, direct_command=lowered)
        if lowered.startswith("/scenario "):
            scenario_type, base, copilot = self._parse_scenario(command[10:])
            if (
                self._configuration.workflow_mode == "renewal_only"
                and scenario_type != ScenarioType.RENEW_AS_IS
            ):
                raise ValueError(
                    "Prebuilt bundle scenarios are not used in this release. Name the "
                    "existing line and target SKU so I can evaluate the exact replacement."
                )
            scenario = await self._orchestrator.build_scenario(
                sender,
                scenario_type,
                base_quantity=base,
                copilot_quantity=copilot,
            )
            if self._configuration.workflow_mode == "simple_pricing":
                await self._send_simple_revised(sender, scenario)
            else:
                await self._send_scenario(sender, scenario)
            return
        if lowered.startswith("/set "):
            line_id, quantity = self._line_quantity(command[5:])
            scenario = await self._orchestrator.edit_quantity(sender, line_id, quantity)
            if self._configuration.workflow_mode == "simple_pricing":
                await self._send_simple_revised(sender, scenario)
            else:
                await self._send_scenario(sender, scenario)
            return
        if lowered.startswith("/copilot "):
            quantity = self._positive_or_zero(command[9:].strip(), "Copilot quantity")
            scenario = await self._orchestrator.edit_quantity(sender, "COPILOT", quantity)
            if self._configuration.workflow_mode == "simple_pricing":
                await self._send_simple_revised(sender, scenario)
            else:
                await self._send_scenario(sender, scenario)
            return
        if lowered.startswith("/promo "):
            value = command[7:].strip().casefold()
            if value not in {"on", "off"}:
                raise ValueError("Use /promo on or /promo off.")
            scenario = await self._orchestrator.reconfigure_pricing(
                sender,
                promo_eligible=value == "on",
            )
            await self._send_scenario(sender, scenario)
            return
        if lowered.startswith("/discount "):
            percentage = self._decimal_value(command[10:], "Discount percentage")
            scenario = await self._orchestrator.set_discount(sender, percentage)
            await self._send_scenario(sender, scenario)
            return
        if lowered.startswith("/adjust "):
            amount = self._decimal_value(command[8:], "Adjustment amount")
            scenario = await self._orchestrator.set_adjustment(sender, amount)
            await self._send_scenario(sender, scenario)
            return
        if lowered.startswith("/term "):
            term_duration = self._required_text(command[6:], "contract term")
            self._validate_annual_contract(term_duration=term_duration)
            scenario = await self._orchestrator.reconfigure_pricing(
                sender,
                term_duration=term_duration,
            )
            await self._send_scenario(sender, scenario)
            return
        if lowered.startswith("/billing "):
            billing_plan = self._required_text(command[9:], "billing plan")
            self._validate_annual_contract(billing_plan=billing_plan)
            scenario = await self._orchestrator.reconfigure_pricing(
                sender,
                billing_plan=billing_plan,
            )
            await self._send_scenario(sender, scenario)
            return
        if lowered.startswith("/segment "):
            scenario = await self._orchestrator.reconfigure_pricing(
                sender,
                segment=self._required_text(command[9:], "segment"),
            )
            await self._send_scenario(sender, scenario)
            return
        if lowered.startswith("/currency "):
            requested = self._required_text(command[10:], "currency").upper()
            configured = self._configuration.currency.upper()
            if requested != configured:
                raise ValueError(
                    "Currency conversion is unavailable because the current pricing data has no "
                    f"Currency or FX-rate column. Current currency: {configured}."
                )
            await self._send_text(
                sender,
                f"Currency remains {configured}; no conversion was applied.",
            )
            return
        disposition_commands = {
            "/retain ": MigrationDisposition.RETAIN,
            "/remove ": MigrationDisposition.REMOVE,
            "/migrate ": MigrationDisposition.MIGRATE,
            "/included ": MigrationDisposition.INCLUDED,
        }
        for prefix, disposition in disposition_commands.items():
            if lowered.startswith(prefix):
                if (
                    self._configuration.workflow_mode == "renewal_only"
                    and disposition
                    in {MigrationDisposition.MIGRATE, MigrationDisposition.INCLUDED}
                ):
                    raise ValueError(
                        "Migration and included-in-bundle actions are disabled until "
                        "authoritative bundling rules are supplied."
                    )
                line_id = command[len(prefix) :].strip()
                if not line_id:
                    raise ValueError(f"Use {prefix.strip()} LINE_ID.")
                scenario = await self._orchestrator.set_disposition(
                    sender, line_id, disposition
                )
                await self._send_scenario(sender, scenario)
                return
        if lowered.startswith("/add "):
            product, quantity = self._product_quantity(command[5:])
            result = await self._orchestrator.add_sku(sender, product, quantity)
            await self._send_sku_change_result(sender, result)
            return
        if lowered.startswith("/replace "):
            line_id, product, quantity = self._replacement(command[9:])
            result = await self._orchestrator.replace_sku(
                sender,
                line_id,
                product,
                quantity,
            )
            await self._send_sku_change_result(sender, result)
            return
        if lowered.startswith("/comment "):
            scenario = await self._orchestrator.add_comment(sender, command[9:])
            await self._send_scenario(sender, scenario)
            return
        if lowered == "/finalize":
            await self._request_finalization(sender)
            return
        if lowered in {
            "/compare enterprise",
            "/compare me3 me5 me7",
            "/compare tiers",
        }:
            await self._send_enterprise_comparison(sender)
            return
        if lowered == "/compare":
            await self._send_comparison(sender)
            return

        if self._intent_interpreter is not None:
            session = await self._orchestrator.get_session(sender)
            try:
                intent = await self._intent_interpreter.interpret(command, session)
            except IntentInterpretationError:
                logger.warning("Natural-language intent interpretation failed")
                await self._send_text(
                    sender,
                    "I could not interpret that request safely. Please rephrase the "
                    "licensing change and include the product or line and quantity where "
                    "relevant.",
                )
                return
            await self._execute_agent_intent(
                sender,
                intent,
                original_message=command,
            )
            return

        session = await self._orchestrator.get_session(sender)
        if session is None or session.estate is None:
            await self._send_text(sender, HELP_TEXT)
            return
        await self._send_text(
            sender,
            "Natural-language assistance is unavailable in this environment. Please try "
            "again when the language service is enabled.",
        )

    async def _execute_agent_intent(
        self,
        sender: str,
        intent: AgentIntent,
        *,
        original_message: str | None = None,
    ) -> None:
        if intent.action == "help":
            await self._send_text(sender, HELP_TEXT)
            return
        if intent.action == "reset_requirement":
            await self._orchestrator.reset_session(sender)
            await self._send_text(
                sender,
                "Done — I cleared the previous draft. Send the first licence and quantity "
                "when you are ready; you can provide them together or one detail at a time.",
            )
            return
        if intent.action == "answer_question":
            answer = self._professional_agent_text(intent.response_text)
            await self._send_text(
                sender,
                answer[:1000]
                if answer
                else "What would you like to know about the licensing review?",
            )
            return
        if intent.action == "out_of_scope":
            response = self._professional_agent_text(intent.response_text)
            await self._send_text(
                sender,
                response[:700]
                if response
                else (
                    "That request is outside this licensing advisor’s scope. I can help "
                    "capture, validate, price, revise, and compare Microsoft licensing "
                    "requirements."
                ),
            )
            return
        if intent.action == "compare_enterprise_options":
            await self._ensure_operation_allowed(
                sender,
                agent_action=intent.action,
            )
            await self._send_enterprise_comparison(sender)
            return
        if intent.action == "capture_requirement":
            if original_message and await self._has_open_requirement_draft(sender):
                await self._capture_typed_requirement(sender, original_message)
                return
            await self._send_text(
                sender,
                "The confirmed requirement is no longer in capture mode. Should this licence "
                "be added to the revised configuration, or would you like to start a new "
                "requirement?",
            )
            return
        if intent.action == "clarify":
            question = self._professional_agent_text(intent.clarification)
            clarification_session = await self._orchestrator.get_session(sender)
            capture_is_open = bool(
                clarification_session is None
                or clarification_session.estate is None
                or clarification_session.confirmed_as_is is None
            )
            if (
                capture_is_open
                and original_message
                and self._looks_like_requirement_fragment(original_message)
            ):
                await self._capture_typed_requirement(sender, original_message)
                return
            resolved_question = (
                question[:500]
                if question
                else "Which proposal, line, or quantity would you like to change?"
            )
            await self._orchestrator.set_pending_dialogue(
                sender,
                PendingDialogue(
                    kind="agent_clarification",
                    question=resolved_question,
                    context_message=(original_message or "")[:2000],
                ),
            )
            await self._send_text(
                sender,
                resolved_question,
            )
            return
        if intent.action == "confirm_validation":
            await self._confirm_validation(sender)
            return
        if intent.action == "reject_validation":
            await self._reject_validation(sender)
            return
        if intent.action == "request_recommendation":
            session = await self._orchestrator.get_session(sender)
            if session is None or session.estate is None:
                await self._send_text(
                    sender,
                    "Please provide the current licensing requirement first. I will confirm "
                    "the exact SKUs and quantities before evaluating alternatives.",
                )
                return
            if session.confirmed_as_is is None:
                await self._send_text(
                    sender,
                    "Confirm the complete current requirement first. I will then evaluate "
                    "alternatives against that approved baseline.",
                )
                return
            requested_line = intent.line_id.strip()
            if not requested_line and session.active_scenario is not None:
                active = session.scenarios.get(session.active_scenario)
                eligible = (
                    [
                        line
                        for line in active.lines
                        if line.proposed_quantity > 0 and line.source_line_id is not None
                    ]
                    if active is not None
                    else []
                )
                if len(eligible) > 1:
                    choices = ", ".join(
                        f"{line.line_id} ({line.sku_title})" for line in eligible[:8]
                    )
                    question = f"Which current line should I evaluate? {choices}"
                    await self._orchestrator.set_pending_dialogue(
                        sender,
                        PendingDialogue(
                            kind="agent_clarification",
                            question=question,
                            context_message=(
                                original_message or "Recommend a suitable alternative"
                            )[:2000],
                        ),
                    )
                    await self._send_text(sender, question)
                    return
            result = await self._orchestrator.recommend_higher_tier(
                sender,
                line_id=requested_line or None,
                quantity=(intent.quantity if intent.quantity >= 0 else None),
            )
            await self._send_official_recommendation_insight(
                sender,
                seller_request=(original_message or "Recommend a suitable alternative"),
                session=session,
                result=result,
            )
            await self._send_sku_change_result(sender, result)
            return
        if intent.action == "set_requirement_detail":
            label = intent.detail_label.strip()
            value = intent.detail_value.strip()
            estate = await self._orchestrator.set_requirement_detail(
                sender,
                label=label,
                value=value,
            )
            if value:
                response = f"Added to the proposal: {label} — {value}."
            else:
                response = f"Removed {label} from the proposal."
            if estate.status.value == "ready":
                response += " Confirm the requirement when all details are correct."
            await self._send_text(sender, response)
            return
        session = await self._orchestrator.get_session(sender)
        if (
            self._configuration.workflow_mode == "simple_pricing"
            and session is not None
            and session.stage
            in {
                WorkflowStage.AWAITING_INITIAL_VALIDATION,
                WorkflowStage.AWAITING_MATCH_CONFIRMATION,
            }
        ):
            if intent.action == "set_quantity":
                estate = await self._orchestrator.edit_requirement_quantity(
                    sender,
                    self._required_text(intent.line_id, "line ID"),
                    self._required_quantity(intent.quantity, allow_zero=False),
                )
                await self._send_updated_requirement(sender, estate)
                return
            if intent.action == "add_sku":
                result = await self._orchestrator.add_requirement_sku(
                    sender,
                    self._required_text(intent.product_query, "product"),
                    self._required_quantity(intent.quantity, allow_zero=False),
                )
                await self._send_sku_change_result(sender, result)
                return
            if intent.action == "replace_sku":
                line_id = self._required_text(intent.line_id, "line ID")
                result = await self._orchestrator.replace_requirement_sku(
                    sender,
                    line_id,
                    self._required_text(intent.product_query, "replacement product"),
                    await self._resolve_replace_quantity(
                        sender, line_id, intent.quantity
                    ),
                )
                await self._send_sku_change_result(sender, result)
                return
            if intent.action == "set_disposition" and intent.disposition == "remove":
                await self._remove_requirement_line(
                    sender,
                    self._required_text(intent.line_id, "line ID"),
                )
                return
            if intent.action == "set_term":
                term_duration = self._required_text(
                    intent.term_duration, "contract term"
                )
                self._validate_annual_contract(term_duration=term_duration)
                estate = await self._orchestrator.edit_requirement_contract(
                    sender,
                    term_duration=term_duration,
                )
                await self._send_updated_requirement(sender, estate)
                return
            if intent.action == "set_billing":
                billing_plan = self._required_text(intent.billing_plan, "billing plan")
                self._validate_annual_contract(billing_plan=billing_plan)
                estate = await self._orchestrator.edit_requirement_contract(
                    sender,
                    billing_plan=billing_plan,
                )
                await self._send_updated_requirement(sender, estate)
                return
        if await self._select_or_request_scenario_target(
            sender,
            intent,
            original_message,
        ):
            return
        await self._ensure_operation_allowed(sender, agent_action=intent.action)
        if intent.action == "build_scenario":
            if intent.scenario == "none":
                raise ValueError("Specify Renew As-Is, ME3, ME5, or ME7.")
            if (
                self._configuration.workflow_mode == "renewal_only"
                and intent.scenario != "renew_as_is"
            ):
                raise ValueError(
                    "Prebuilt bundle scenarios are not used in this release. Which existing "
                    "line and exact target SKU should I evaluate?"
                )
            scenario = await self._orchestrator.build_scenario(
                sender,
                ScenarioType(intent.scenario),
                base_quantity=self._optional_quantity(intent.quantity),
                copilot_quantity=self._optional_quantity(intent.copilot_quantity),
            )
            await self._send_scenario(sender, scenario)
            return
        if intent.action == "set_quantity":
            line_id = self._required_text(intent.line_id, "line ID")
            quantity = self._required_quantity(intent.quantity)
            scenario = await self._orchestrator.edit_quantity(
                sender, line_id, quantity
            )
            await self._send_scenario(sender, scenario)
            return
        if intent.action == "set_copilot":
            scenario = await self._orchestrator.edit_quantity(
                sender,
                "COPILOT",
                self._required_quantity(intent.copilot_quantity),
            )
            await self._send_scenario(sender, scenario)
            return
        if intent.action == "set_disposition":
            line_id = self._required_text(intent.line_id, "line ID")
            if intent.disposition == "none":
                raise ValueError("Specify retain, remove, migrate, or included.")
            if (
                self._configuration.workflow_mode == "renewal_only"
                and intent.disposition in {"migrate", "included"}
            ):
                raise ValueError(
                    "Migration and included-in-bundle actions are disabled until "
                    "authoritative bundling rules are supplied."
                )
            scenario = await self._orchestrator.set_disposition(
                sender,
                line_id,
                MigrationDisposition(intent.disposition),
            )
            await self._send_scenario(sender, scenario)
            return
        if intent.action == "add_sku":
            product = self._required_text(intent.product_query, "product")
            quantity = self._required_quantity(intent.quantity, allow_zero=False)
            result = await self._orchestrator.add_sku(sender, product, quantity)
            await self._send_sku_change_result(sender, result)
            return
        if intent.action == "replace_sku":
            line_id = self._required_text(intent.line_id, "line ID")
            product = self._required_text(intent.product_query, "replacement product")
            quantity = await self._resolve_replace_quantity(
                sender, line_id, intent.quantity
            )
            result = await self._orchestrator.replace_sku(
                sender, line_id, product, quantity
            )
            await self._send_sku_change_result(sender, result)
            return
        if intent.action == "set_promo":
            if self._configuration.workflow_mode == "simple_pricing":
                raise ValueError(
                    "Promotion eligibility is not seller-facing in this release."
                )
            if intent.boolean_value == "none":
                raise ValueError("Confirm whether the customer is promotion eligible.")
            scenario = await self._orchestrator.reconfigure_pricing(
                sender,
                promo_eligible=intent.boolean_value == "true",
            )
            await self._send_scenario(sender, scenario)
            return
        if intent.action == "set_discount":
            if self._configuration.workflow_mode == "simple_pricing":
                raise ValueError(
                    "Discount selection is not seller-facing in this release."
                )
            scenario = await self._orchestrator.set_discount(
                sender,
                Decimal(str(intent.percentage)),
            )
            await self._send_scenario(sender, scenario)
            return
        if intent.action == "set_adjustment":
            if self._configuration.workflow_mode == "simple_pricing":
                raise ValueError(
                    "Commercial adjustments are not seller-facing in this release."
                )
            scenario = await self._orchestrator.set_adjustment(
                sender,
                Decimal(str(intent.amount)),
            )
            await self._send_scenario(sender, scenario)
            return
        if intent.action == "set_term":
            term_duration = self._required_text(
                intent.term_duration,
                "contract term",
            )
            self._validate_annual_contract(term_duration=term_duration)
            scenario = await self._orchestrator.reconfigure_pricing(
                sender,
                term_duration=term_duration,
            )
            await self._send_scenario(sender, scenario)
            return
        if intent.action == "set_billing":
            billing_plan = self._required_text(intent.billing_plan, "billing plan")
            self._validate_annual_contract(billing_plan=billing_plan)
            scenario = await self._orchestrator.reconfigure_pricing(
                sender,
                billing_plan=billing_plan,
            )
            await self._send_scenario(sender, scenario)
            return
        if intent.action == "set_segment":
            scenario = await self._orchestrator.reconfigure_pricing(
                sender,
                segment=self._required_text(intent.segment, "segment"),
            )
            await self._send_scenario(sender, scenario)
            return
        if intent.action == "set_currency":
            requested = self._required_text(intent.currency, "currency").upper()
            configured = self._configuration.currency.upper()
            if requested != configured:
                raise ValueError(
                    "Currency conversion is unavailable because the current pricing data has no "
                    f"currency or FX-rate table. Current currency: {configured}."
                )
            await self._send_text(
                sender,
                f"Currency remains {configured}; no conversion was applied.",
            )
            return
        if intent.action == "confirm_matches":
            session = await self._orchestrator.get_session(sender)
            if session is None or session.estate is None:
                raise ValueError("Upload a licence file before confirming SKU matches.")
            pending = {line.line_id: line for line in session.estate.pending_lines}
            selected_ids = {item.line_id.upper() for item in intent.match_selections}
            if not selected_ids or not selected_ids.issubset(set(pending)):
                raise ValueError("Choose one of the displayed options for a pending line.")
            selections: dict[str, tuple[str, str]] = {}
            for item in intent.match_selections:
                line_id = item.line_id.upper()
                candidates = pending[line_id].candidates
                index = item.candidate_number - 1
                if index < 0 or index >= len(candidates):
                    raise ValueError(
                        f"Candidate {item.candidate_number} is invalid for {line_id}."
                    )
                candidate = candidates[index]
                selections[line_id] = (candidate.product_id, candidate.sku_id)
            estate = await self._orchestrator.confirm_matches(sender, selections)
            await self._after_requirement_match_confirmation(sender, estate)
            return
        if intent.action == "confirm_sku":
            if intent.candidate_number <= 0:
                raise ValueError("Choose one of the numbered SKU candidates.")
            result = await self._orchestrator.confirm_sku_change(
                sender,
                intent.candidate_number,
            )
            await self._send_sku_change_result(sender, result)
            return
        if intent.action == "cancel_sku":
            cancelled = await self._orchestrator.cancel_sku_change(sender)
            await self._send_text(
                sender,
                "Pending SKU change cancelled."
                if cancelled
                else "There is no pending SKU change to cancel.",
            )
            return
        if intent.action == "add_comment":
            comment = self._required_text(intent.comment, "comment")
            scenario = await self._orchestrator.add_comment(sender, comment)
            await self._send_scenario(sender, scenario)
            return
        if intent.action == "finalize":
            await self._request_finalization(sender)
            return
        if intent.action == "compare":
            await self._send_comparison(sender)
            return
        raise ValueError("The interpreted action is not supported.")

    async def _handle_interactive(self, sender: str, reply_id: str) -> None:
        parts = reply_id.split("|")
        if len(parts) < 3 or parts[0] != "licensing":
            raise ValueError("That menu selection is no longer recognized.")
        action, value = parts[1], parts[2]
        if action == "validate_initial":
            if value == "confirm":
                await self._confirm_validation(sender)
            elif value == "add_more":
                await self._send_text(
                    sender,
                    "Which additional licence or licences should I add to this renewal "
                    "requirement? Include the product name and quantity; you may also send "
                    "another file, image, or voice note.",
                )
            else:
                await self._reject_validation(sender)
            return
        if action == "validate_final":
            if value == "confirm":
                await self._confirm_finalization_and_send(sender)
            else:
                await self._reject_validation(sender)
            return
        if action == "recommend":
            if value == "yes":
                await self._send_text(
                    sender,
                    "Which change would you like me to evaluate? You can update a quantity, "
                    "add or remove a licence, replace a selected SKU, or request a "
                    "higher-tier option for a specific line. I will confirm any uncertain "
                    "SKU before recalculating the annual value.",
                )
            else:
                await self._send_text(
                    sender,
                    "No recommendation changes were applied. You can finalize the as-is "
                    "configuration whenever you are ready.",
                )
            return
        if action == "match_confirm":
            if len(parts) != 4 or not parts[3].isdigit():
                raise ValueError("That product selection is no longer valid.")
            estate = await self._confirm_requirement_candidate(
                sender,
                line_id=value,
                candidate_number=int(parts[3]),
            )
            await self._after_requirement_match_confirmation(sender, estate)
            return
        if action == "sku_confirm":
            if len(parts) != 4 or not parts[3].isdigit():
                raise ValueError("That SKU confirmation is invalid.")
            result = await self._orchestrator.confirm_sku_change(
                sender,
                int(parts[3]),
                confirmation_id=value,
            )
            await self._send_sku_change_result(sender, result)
            return
        await self._ensure_operation_allowed(sender)
        if action == "scenario":
            try:
                scenario_type = ScenarioType(value)
            except ValueError as error:
                raise ValueError("Unknown commercial scenario.") from error
            scenario = await self._orchestrator.build_scenario(sender, scenario_type)
            await self._send_scenario(sender, scenario)
            return
        if action == "compare":
            await self._send_comparison(sender)
            return
        if action == "finalize":
            await self._request_finalization(sender)
            return
        if action == "scenarios":
            await self._send_scenario_menu(sender)
            return
        raise ValueError("Unknown commercial workflow action.")

    async def _continue_after_estate(self, sender: str) -> None:
        if self._configuration.workflow_mode == "simple_pricing":
            estate = await self._orchestrator.request_requirement_validation(sender)
            await self._send_text(
                sender,
                "*Complete requirement review*\n\n"
                f"I have captured {len(estate.lines)} SKU line(s) covering "
                f"{estate.total_renewal_quantity:,} licences. Review the complete list, "
                "including quantities, annual terms, and any supplied dates. You can add "
                "another licence or describe a correction naturally. When the list is "
                "complete, confirm that I should calculate the Renew As-Is annual price. "
                "Pricing remains paused until that confirmation.",
            )
            return
        if self._configuration.workflow_mode in {
            "renewal_only",
            "upgrade_comparison",
        }:
            scenario = await self._orchestrator.build_scenario(
                sender,
                ScenarioType.RENEW_AS_IS,
                # The enterprise-suite rows are promotion-only. Pricing is
                # provisional until the explicit initial seller-validation gate below.
                promo_eligible=True,
            )
            await self._orchestrator.request_initial_validation(sender)
            await self._send_text(
                sender,
                "I prepared the initial Renew As-Is analysis and pricing. Review the "
                "SKU matches, quantities, renewal dates, prices, and annual total. "
                "Promotional pricing is provisional until you confirm customer eligibility. "
                "Seller validation is required before edits or comparisons are enabled.",
            )
            await self._send_scenario(sender, scenario)
            return
        await self._send_scenario_menu(sender)

    async def _send_scenario_menu(self, sender: str) -> None:
        await self._send_text(
            sender,
            "Which annual option would you like me to prepare: Renew As-Is, ME3, ME5, "
            "or ME7? You may also ask me to compare all four. Existing additional "
            "licences remain unchanged unless you explicitly ask to modify them.",
        )

    async def _send_updated_requirement(self, sender: str, estate) -> None:
        await self._send_text(
            sender,
            "Requirement updated. Review the refreshed table and confirm when correct.",
        )
        await self._send_estate_table(sender, estate)
        if estate.pending_lines:
            await self._send_pending_match_requests(sender, estate)
        else:
            await self._continue_after_estate(sender)

    async def _send_simple_as_is(self, sender: str, scenario) -> None:
        images = render_simple_pricing_table_images(
            scenario,
            title="Renew As-Is cost",
            currency=self._configuration.currency,
        )
        for index, content in enumerate(images, start=1):
            await self._whatsapp_client.send_image(
                to=sender,
                content=content,
                filename=f"as-is-cost-{index}.png",
                content_type="image/png",
                caption=f"Confirmed Renew As-Is cost • Page {index}/{len(images)}",
            )
        estate, current, _revised = await self._orchestrator.simple_review(sender)
        pdf = render_simple_commercial_pdf(
            estate,
            current,
            currency=self._configuration.currency,
        )
        await self._whatsapp_client.send_document(
            to=sender,
            content=pdf,
            filename="as-is-commercial.pdf",
            content_type="application/pdf",
            caption="Confirmed Renew As-Is commercial review",
        )

    async def _send_recommendation_prompt(self, sender: str) -> None:
        await self._send_text(
            sender,
            "The confirmed Renew As-Is proposal is ready. Would you like me to evaluate "
            "an upgrade, replacement, addition, removal, or quantity change? Describe the "
            "business need or requested change naturally. If no change is required, tell "
            "me to finalize the Renew As-Is proposal.",
        )

    async def _simple_proposal_label(self, sender: str, scenario) -> str:
        if scenario.scenario_type != ScenarioType.RENEW_AS_IS:
            return f"{scenario.scenario_type.label} configuration"
        session = await self._orchestrator.get_session(sender)
        baseline = session.confirmed_as_is if session is not None else None
        if baseline is not None and self._scenario_configuration_changed(
            baseline,
            scenario,
        ):
            return "Revised annual configuration"
        return "Renew As-Is"

    @staticmethod
    def _scenario_configuration_changed(baseline, scenario) -> bool:
        excluded = {"status", "revision", "created_at", "updated_at"}
        return baseline.model_dump(exclude=excluded) != scenario.model_dump(exclude=excluded)

    async def _send_simple_revised(self, sender: str, scenario) -> None:
        proposal_label = await self._simple_proposal_label(sender, scenario)
        pricing_title = (
            "Revised annual cost"
            if proposal_label == "Revised annual configuration"
            else f"{proposal_label} annual cost"
        )
        pricing_images = render_simple_pricing_table_images(
            scenario,
            title=pricing_title,
            currency=self._configuration.currency,
        )
        for index, content in enumerate(pricing_images, start=1):
            await self._whatsapp_client.send_image(
                to=sender,
                content=content,
                filename=f"revised-cost-{index}.png",
                content_type="image/png",
                caption=f"{proposal_label} • Page {index}/{len(pricing_images)}",
            )
        if scenario.status != ScenarioStatus.FINAL:
            await self._send_text(
                sender,
                f"I recalculated the {proposal_label}. You can describe another "
                "change, ask me to compare it with Renew As-Is, or ask me to finalize the "
                "proposal.",
            )

    async def _send_simple_commercial_pdf(self, sender: str) -> None:
        estate, current, revised = await self._orchestrator.simple_review(sender)
        proposal_label = await self._simple_proposal_label(sender, revised)
        pdf = render_simple_commercial_pdf(
            estate,
            current,
            revised,
            currency=self._configuration.currency,
        )
        await self._whatsapp_client.send_document(
            to=sender,
            content=pdf,
            filename="renew-as-is-vs-selected.pdf",
            content_type="application/pdf",
            caption=f"Confirmed Renew As-Is vs {proposal_label}",
        )

    async def _send_scenario(self, sender: str, scenario) -> None:
        if self._configuration.workflow_mode == "simple_pricing":
            await self._send_simple_revised(sender, scenario)
            return
        await self._send_scenario_table(sender, scenario)
        session = await self._orchestrator.get_session(sender)
        if session is not None and session.stage == WorkflowStage.AWAITING_INITIAL_VALIDATION:
            await self._send_initial_validation_prompt(sender, scenario)
            return
        if self._configuration.workflow_mode == "renewal_only":
            if scenario.status == ScenarioStatus.FINAL:
                return
            await self._send_text(
                sender,
                "The annual proposal is ready for review. Describe any required change, or "
                "confirm that you want to finalize it.",
            )
            return
        if self._configuration.workflow_mode == "upgrade_comparison":
            if scenario.status == ScenarioStatus.FINAL:
                return
            await self._send_text(
                sender,
                "This option uses a one-year term with annual billing. Existing add-ons "
                "remain unchanged unless you explicitly modify them. Ask for another annual "
                "option, a comparison, or finalization when ready.",
            )
            return
        await self._send_text(
            sender,
            "The proposal has been updated. Describe another change, request an annual "
            "comparison, or ask me to finalize it.",
        )

    async def _send_scenario_table(self, sender: str, scenario) -> None:
        try:
            images = render_scenario_table_images(
                scenario,
                currency=self._configuration.currency,
            )
            for index, content in enumerate(images, start=1):
                pending = sum(line.decision_required for line in scenario.lines)
                await self._whatsapp_client.send_image(
                    to=sender,
                    content=content,
                    filename=(
                        f"{scenario.scenario_type.value}-proposal-table-{index}.png"
                    ),
                    content_type="image/png",
                    caption=(
                        f"{scenario.scenario_type.label} • Revision {scenario.revision} • "
                        f"Annual total {self._configuration.currency} "
                        f"{scenario.total_value:,.2f} • Decisions {pending} • "
                        f"Page {index}/{len(images)}"
                    ),
                )
            return
        except Exception:
            logger.exception(
                "Unable to render or send scenario table image; using text fallback"
            )
        await self._send_text_chunks(
            sender,
            format_scenario(scenario, self._configuration.currency),
            limit=RESPONSIVE_MESSAGE_LIMIT,
        )

    async def _send_sku_change_result(
        self,
        sender: str,
        result: SkuChangeResult,
    ) -> None:
        if result.state == "applied":
            if result.estate is not None:
                await self._send_updated_requirement(sender, result.estate)
                return
            if result.scenario is not None:
                await self._send_scenario(sender, result.scenario)
                return
            raise RuntimeError("Applied SKU change returned no updated state.")
            return
        pending = result.confirmation
        if pending is None:
            raise RuntimeError("SKU confirmation state did not contain candidates.")
        lines = [
            "*I need to confirm the intended SKU*",
            "No change was made.",
            sku_clarification_question(
                pending.product_query,
                pending.candidates,
            ),
        ]
        for index, candidate in enumerate(pending.candidates, start=1):
            lines.append(f"{index}. {format_sku_candidate(candidate)}")
        lines.extend(
            [
                "",
                "Choose an option below, reply with its number, or send the complete "
                "product name. I will apply the change only after you confirm.",
            ]
        )
        await self._send_text_chunks(sender, "\n".join(lines))
        total = len(pending.candidates)
        for offset in range(0, total, 10):
            page = pending.candidates[offset : offset + 10]
            rows = [
                InteractiveRow(
                    id=f"licensing|sku_confirm|{pending.id}|{offset + page_index}",
                    title=f"Option {offset + page_index}",
                    description=self._candidate_row_description(candidate),
                )
                for page_index, candidate in enumerate(page, start=1)
            ]
            first = offset + 1
            last = offset + len(page)
            await self._send_interactive(
                sender,
                InteractiveList(
                    body=InteractiveText(
                        text=(
                            "Select the exact Microsoft product. "
                            f"Options {first}-{last} of {total}; full names are shown "
                            "in the preceding message."
                        )
                    ),
                    action=InteractiveListAction(
                        button="Choose exact SKU",
                        sections=[
                            InteractiveSection(
                                title=f"Options {first}-{last}",
                                rows=rows,
                            )
                        ],
                    ),
                ),
            )

    @staticmethod
    def _candidate_row_description(candidate: object) -> str:
        product_id = str(getattr(candidate, "product_id", "")).strip()
        sku_id = str(getattr(candidate, "sku_id", "")).strip()
        title = str(getattr(candidate, "sku_title", "")).strip()
        identity = " / ".join(value for value in (product_id, sku_id) if value)
        prefix = f"ID {identity} · " if identity else ""
        return f"{prefix}{title}"[:72]

    async def _send_official_recommendation_insight(
        self,
        sender: str,
        *,
        seller_request: str,
        session,
        result: SkuChangeResult,
    ) -> None:
        pending = result.confirmation
        if pending is None or not pending.candidates:
            return
        if self._recommendation_advisor is None:
            await self._send_text(
                sender,
                "I found catalogue alternatives in the same product family. I have not "
                "made a feature-fit claim because official Microsoft recommendation "
                "research is not enabled in this environment.",
            )
            return
        current = None
        if session.active_scenario is not None:
            scenario = session.scenarios.get(session.active_scenario)
            if scenario is not None:
                current = next(
                    (
                        line
                        for line in scenario.lines
                        if line.line_id == pending.source_line_id
                    ),
                    None,
                )
        if current is None:
            await self._send_text(
                sender,
                "Which current SKU and business capability should I evaluate?",
            )
            return
        try:
            insight = await self._recommendation_advisor.advise(
                seller_request=seller_request,
                current_sku=current.sku_title,
                quantity=pending.quantity,
                candidate_skus=[item.sku_title for item in pending.candidates],
            )
        except IntentInterpretationError:
            logger.warning("Official Microsoft recommendation research failed safely")
            await self._send_text(
                sender,
                "I can show the available same-family SKUs, but I could not verify a "
                "feature-based recommendation from official Microsoft documentation just "
                "now. No product change has been made.",
            )
            return
        if insight.clarification_question.strip():
            clarification = self._professional_agent_text(
                insight.clarification_question
            )[:500]
            if clarification:
                await self._send_text(sender, clarification)
        if not insight.suggested_candidate_numbers:
            return
        selected = ", ".join(
            pending.candidates[number - 1].sku_title
            for number in insight.suggested_candidate_numbers
        )
        await self._send_text(
            sender,
            "*Microsoft-documented licensing insight*\n\n"
            f"{self._professional_agent_text(insight.recommendation)}\n\n"
            f"Catalogue option(s) supported for review: {selected}. No change has been "
            "applied. Confirm the exact SKU you want me to use, or describe another "
            "requirement you would like me to evaluate.",
        )

    async def _send_comparison(self, sender: str) -> None:
        if self._configuration.workflow_mode == "simple_pricing":
            _estate, current, revised = await self._orchestrator.simple_review(sender)
            revised_label = await self._simple_proposal_label(sender, revised)
            difference = revised.total_value - current.total_value
            sign = "+" if difference > 0 else ""
            await self._send_text(
                sender,
                "*Annual commercial comparison*\n\n"
                f"Renew As-Is: {self._configuration.currency} "
                f"{current.total_value:,.2f}\n"
                f"{revised_label}: {self._configuration.currency} "
                f"{revised.total_value:,.2f}\n"
                f"Difference: {sign}{self._configuration.currency} "
                f"{difference:,.2f}\n\n"
                "The PDF separates the confirmed requirement, the revised configuration, "
                "and every replacement or addition.",
            )
            await self._send_simple_commercial_pdf(sender)
            return
        if self._configuration.workflow_mode == "renewal_only":
            await self._send_text(
                sender,
                "Bundle comparison is not enabled because no authoritative bundling "
                "rules are available. I will provide the Renew As-Is proposal instead.",
            )
            await self._send_active_proposal_pdf(sender)
            return
        await self._send_enterprise_comparison(sender)

    async def _send_enterprise_comparison(self, sender: str) -> None:
        if self._configuration.workflow_mode == "renewal_only":
            raise ValueError(
                "ME3, ME5, and ME7 comparison is not enabled in this workflow."
            )
        estate, scenarios, comparison = await self._orchestrator.comparison(sender)
        await self._send_text(
            sender,
            "*Seller-requested enterprise comparison*\n\nRenew As-Is, ME3, ME5, and "
            "ME7 use their exact one-year annual catalogue SKUs. Existing additional "
            "licences are retained unless you explicitly change them; no feature, "
            "entitlement, migration, or bundle-removal assumption has been applied.",
        )
        await self._send_comparison_table(sender, comparison)
        pdf = render_comparison_pdf(
            estate,
            scenarios,
            comparison,
            currency=self._configuration.currency,
            include_internal_commercial_fields=(
                self._configuration.workflow_mode != "simple_pricing"
            ),
        )
        try:
            await self._whatsapp_client.send_document(
                to=sender,
                content=pdf,
                filename="annual-licensing-comparison.pdf",
                content_type="application/pdf",
                caption="Seller-requested annual enterprise comparison",
            )
        except WhatsAppAPIError as error:
            logger.error(
                "Unable to send comparison PDF status=%s network_error=%s",
                error.status_code,
                error.network_error,
            )
            raise

    async def _send_comparison_table(self, sender: str, comparison) -> None:
        try:
            images = render_comparison_table_images(
                comparison,
                currency=self._configuration.currency,
            )
            for index, content in enumerate(images, start=1):
                await self._whatsapp_client.send_image(
                    to=sender,
                    content=content,
                    filename=f"annual-comparison-table-{index}.png",
                    content_type="image/png",
                    caption=(
                        f"Annual comparison • Recommended: "
                        f"{comparison.recommended_scenario.label} • "
                        f"Page {index}/{len(images)}"
                    ),
                )
            return
        except Exception:
            logger.exception(
                "Unable to render or send comparison table image; using text fallback"
            )
        await self._send_text_chunks(
            sender,
            format_comparison(comparison, self._configuration.currency),
            limit=RESPONSIVE_MESSAGE_LIMIT,
        )

    async def _send_initial_validation_prompt(self, sender: str, scenario) -> None:
        unresolved = bool(
            scenario.unresolved_decisions
            or any(line.decision_required for line in scenario.lines)
        )
        body = (
            "*Seller validation required*\n\n"
            "Review the uploaded SKUs, quantities, renewal dates, pricing, and annual "
            "total. By confirming, you also attest that the customer is eligible for "
            "the displayed new-to-Microsoft promotional pricing. "
            + (
                "Resolve every item marked as requiring eligibility or a seller decision "
                "before confirming. "
                if unresolved
                else "No unresolved pricing decisions remain. "
            )
            + "Confirm only when the analysis and promotion eligibility are correct."
        )
        await self._send_text(
            sender,
            body
            + " Reply naturally to confirm the details, or describe the correction that "
            "should be made.",
        )

    async def _confirm_validation(self, sender: str) -> None:
        session = await self._orchestrator.get_session(sender)
        if session is None:
            raise ValueError("Upload a licence file before confirming validation.")
        if session.stage == WorkflowStage.AWAITING_INITIAL_VALIDATION:
            if self._configuration.workflow_mode == "simple_pricing":
                await self._orchestrator.confirm_requirement(sender)
                scenario = await self._orchestrator.build_scenario(
                    sender,
                    ScenarioType.RENEW_AS_IS,
                    promo_eligible=False,
                )
                unavailable = [line for line in scenario.lines if line.price_unavailable]
                if unavailable:
                    await self._orchestrator.reopen_requirement_validation(sender)
                    names = ", ".join(
                        f"{line.line_id} ({line.sku_title})" for line in unavailable[:5]
                    )
                    await self._send_text(
                        sender,
                        "*Pricing clarification required*\n\n"
                        "The captured SKU and quantity details are clear, but I cannot "
                        "calculate a complete "
                        f"annual value because no applicable price is available for {names}. "
                        "Please confirm a different exact SKU, or provide the missing SKU "
                        "identity before I continue.",
                    )
                    return
                await self._orchestrator.save_confirmed_as_is(sender, scenario)
                await self._send_text(
                    sender,
                    "Requirement confirmed. I calculated the Renew As-Is annual cost "
                    "from the approved SKU configuration.",
                )
                await self._send_simple_as_is(sender, scenario)
                await self._send_recommendation_prompt(sender)
                return
            scenario = await self._orchestrator.confirm_initial_validation(sender)
            await self._send_text(
                sender,
                "Seller validation recorded for the uploaded estate and initial "
                "Renew As-Is pricing, including promotion eligibility. Edits and annual "
                "comparisons are now enabled.",
            )
            await self._send_scenario(sender, scenario)
            return
        if session.stage == WorkflowStage.AWAITING_FINAL_VALIDATION:
            await self._confirm_finalization_and_send(sender)
            return
        raise ValueError("There is no seller validation currently awaiting confirmation.")

    async def _reject_validation(self, sender: str) -> None:
        session = await self._orchestrator.get_session(sender)
        if session is None:
            raise ValueError("Upload a licence file before responding to validation.")
        if session.stage == WorkflowStage.AWAITING_INITIAL_VALIDATION:
            if self._configuration.workflow_mode == "simple_pricing":
                await self._send_text(
                    sender,
                    "Requirement confirmation was not recorded. Please specify the SKU or "
                    "quantity corrections you would like me to apply. I will validate the "
                    "changes and present an updated confirmation table before pricing.",
                )
                return
            await self._send_text(
                sender,
                "Initial validation was not recorded. Re-upload a corrected customer "
                "file if SKU, quantity, or renewal data is wrong. If pricing requires "
                "promotion eligibility, confirm or reject that eligibility explicitly, "
                "then validate the refreshed proposal.",
            )
            return
        if session.stage == WorkflowStage.AWAITING_FINAL_VALIDATION:
            await self._orchestrator.cancel_finalization(sender)
            await self._send_text(
                sender,
                "Finalization cancelled. The proposal remains editable and was not "
                "marked final.",
            )
            return
        raise ValueError("There is no seller validation currently awaiting a response.")

    async def _request_finalization(self, sender: str) -> None:
        scenario = await self._orchestrator.request_finalization(sender)
        if self._configuration.workflow_mode == "simple_pricing":
            session = await self._orchestrator.get_session(sender)
            proposal_label = await self._simple_proposal_label(sender, scenario)
            detail_count = (
                len(session.estate.seller_details)
                if session is not None and session.estate is not None
                else 0
            )
            detail_status = (
                f"The proposal includes {detail_count} seller-provided detail(s)."
                if detail_count
                else "No optional seller details were supplied, so none were added."
            )
            await self._send_text(
                sender,
                "*Final seller validation required*\n\n"
                f"Proposal: {proposal_label}\n"
                f"Annual value: {format_money(scenario.total_value, self._configuration.currency)}\n\n"
                f"{detail_status}\n\n"
                "Confirm naturally that the SKU configuration, quantities, annual terms, "
                "and commercial value are correct and should be finalized. If anything "
                "needs to change, describe the correction instead.",
            )
            return
        discount_amount = (
            scenario.subtotal * scenario.discount_percentage / Decimal("100")
        )
        await self._send_text(
            sender,
            "*Final seller validation required*\n\n"
            f"Option: {scenario.scenario_type.label}\n"
            f"Subtotal: {format_money(scenario.subtotal, self._configuration.currency)}\n"
            f"Discount: {scenario.discount_percentage:,.2f}% "
            f"(-{format_money(discount_amount, self._configuration.currency)})\n"
            f"Adjustment: {format_money(scenario.adjustment_amount, self._configuration.currency)}\n"
            f"Final annual total: {format_money(scenario.total_value, self._configuration.currency)}\n\n"
            "Confirm naturally that the configuration and commercial values are correct. "
            "No final PDF will be issued until you approve. Describe any correction instead "
            "of confirming.",
        )

    async def _confirm_finalization_and_send(self, sender: str) -> None:
        scenario = await self._orchestrator.confirm_finalization(sender)
        proposal_label = (
            await self._simple_proposal_label(sender, scenario)
            if self._configuration.workflow_mode == "simple_pricing"
            else scenario.scenario_type.label
        )
        await self._send_text(
            sender,
            f"Final seller validation recorded. Finalized proposal: {proposal_label}.",
        )
        if self._configuration.workflow_mode == "simple_pricing":
            await self._send_simple_revised(sender, scenario)
            await self._send_simple_commercial_pdf(sender)
            return
        await self._send_scenario(sender, scenario)
        if self._configuration.workflow_mode in {
            "renewal_only",
            "upgrade_comparison",
        }:
            await self._send_active_proposal_pdf(sender)

    async def _ensure_operation_allowed(
        self,
        sender: str,
        *,
        direct_command: str | None = None,
        agent_action: str | None = None,
    ) -> None:
        session = await self._orchestrator.get_session(sender)
        if session is None:
            return
        if session.stage == WorkflowStage.AWAITING_INITIAL_VALIDATION:
            promo_resolution = (
                bool(direct_command and direct_command.startswith("/promo "))
                or agent_action == "set_promo"
            )
            if not promo_resolution:
                raise ValueError(
                    "Seller validation is required before edits or comparisons. Review "
                    "the estate and Renew As-Is pricing, resolve any eligibility item, "
                    "then confirm naturally that the analysis and pricing are correct."
                )
        if session.stage == WorkflowStage.AWAITING_FINAL_VALIDATION:
            raise ValueError(
                "Final seller validation is pending. Confirm finalization naturally, or "
                "describe the correction you want before sending another operation."
            )
        if session.stage == WorkflowStage.FINALIZED:
            raise ValueError(
                "This proposal is finalized. Upload a new customer file to start a new review."
            )

    def _validate_annual_contract(
        self,
        *,
        term_duration: str | None = None,
        billing_plan: str | None = None,
    ) -> None:
        if term_duration is not None and term_duration.casefold() != "p1y":
            raise ValueError(
                "This workflow is fixed to a one-year term (P1Y) for every option."
            )
        if billing_plan is not None and billing_plan.casefold() != "annual":
            raise ValueError(
                "This workflow is fixed to annual billing for every option."
            )

    @staticmethod
    def _requests_monthly_billing(message: str) -> bool:
        monthly_terms = ("monthly", "month-to-month", "per month", "p1m")
        return any(value in message for value in monthly_terms)

    @staticmethod
    def _requests_restricted_pricing(message: str) -> bool:
        restricted_terms = (
            "promo",
            "promotion",
            "promotional",
            "partner best",
            "partner-best",
            "best offer",
            "best price",
            "distributor margin",
        )
        return any(value in message for value in restricted_terms)

    async def _send_active_proposal_pdf(self, sender: str) -> None:
        session = await self._orchestrator.get_session(sender)
        if session is None or session.estate is None or session.active_scenario is None:
            raise ValueError("Upload a licence file and prepare a proposal first.")
        scenario = session.scenarios.get(session.active_scenario)
        if scenario is None:
            raise ValueError("The active proposal could not be found.")
        pdf = render_proposal_pdf(
            session.estate,
            scenario,
            currency=self._configuration.currency,
        )
        try:
            proposal_name = {
                ScenarioType.RENEW_AS_IS: "renewal",
                ScenarioType.ME3_COPILOT: "me3",
                ScenarioType.ME5_COPILOT: "me5",
                ScenarioType.ME7: "me7",
            }[scenario.scenario_type]
            await self._whatsapp_client.send_document(
                to=sender,
                content=pdf,
                filename=f"licensing-{proposal_name}-proposal.pdf",
                content_type="application/pdf",
                caption=(
                    f"Customer-ready {scenario.scenario_type.label} licensing proposal"
                ),
            )
        except WhatsAppAPIError as error:
            logger.error(
                "Unable to send renewal proposal PDF status=%s network_error=%s",
                error.status_code,
                error.network_error,
            )
            raise

    async def _resolve_replace_quantity(
        self,
        sender: str,
        line_id: str,
        requested: int,
    ) -> int:
        if requested >= 0:
            return self._required_quantity(requested, allow_zero=False)
        session = await self._orchestrator.get_session(sender)
        if session is None:
            raise ValueError("Provide a replacement quantity.")
        normalized_id = line_id.upper()
        if session.stage == WorkflowStage.AWAITING_INITIAL_VALIDATION and session.estate:
            line = next(
                (item for item in session.estate.lines if item.line_id == normalized_id),
                None,
            )
            if line is not None:
                return line.renewal_quantity
        if session.active_scenario is not None:
            scenario = session.scenarios.get(session.active_scenario)
            if scenario is not None:
                line = next(
                    (item for item in scenario.lines if item.line_id == normalized_id),
                    None,
                )
                if line is not None:
                    return line.proposed_quantity or line.existing_quantity
        raise ValueError(
            f"Line {normalized_id} was not found; provide an explicit replacement quantity."
        )

    async def _send_text(self, sender: str, body: str) -> None:
        try:
            await self._whatsapp_client.send_message(
                WhatsAppTextMessage(to=sender, text=TextContent(body=body))
            )
        except WhatsAppAPIError as error:
            logger.error(
                "Unable to send WhatsApp text status=%s network_error=%s",
                error.status_code,
                error.network_error,
            )
            raise

    async def _send_text_chunks(
        self,
        sender: str,
        body: str,
        *,
        limit: int = 4096,
    ) -> None:
        for chunk in self._text_chunks(body, limit=limit):
            await self._send_text(sender, chunk)

    async def _send_interactive(
        self,
        sender: str,
        interactive: InteractiveList,
    ) -> bool:
        try:
            await self._whatsapp_client.send_message(
                WhatsAppInteractiveMessage(to=sender, interactive=interactive)
            )
            return True
        except WhatsAppAPIError as error:
            logger.error(
                "Unable to send WhatsApp interactive message status=%s network_error=%s",
                error.status_code,
                error.network_error,
            )
            raise

    @staticmethod
    def _parse_confirmations(value: str) -> dict[str, tuple[str, str]]:
        result: dict[str, tuple[str, str]] = {}
        for selection in value.split(";"):
            if "=" not in selection:
                raise ValueError("Use LINE=PRODUCT_ID,SKU_ID for every pending line.")
            line_id, identifiers = selection.split("=", 1)
            parts = [item.strip() for item in identifiers.split(",")]
            if len(parts) != 2 or not all(parts):
                raise ValueError("Use LINE=PRODUCT_ID,SKU_ID for every pending line.")
            normalized_id = line_id.strip().upper()
            if normalized_id in result:
                raise ValueError(f"Duplicate confirmation for {normalized_id}.")
            result[normalized_id] = (parts[0], parts[1])
        if not result:
            raise ValueError("No SKU confirmations were provided.")
        return result

    @staticmethod
    def _parse_scenario(value: str) -> tuple[ScenarioType, int | None, int | None]:
        parts = [item.strip() for item in value.split("|")]
        scenario = SCENARIO_ALIASES.get(parts[0].casefold())
        if scenario is None:
            raise ValueError("Scenario must be renew, me3, me5, or me7.")
        base = (
            WhatsAppWebhookService._positive_or_zero(parts[1], "Base quantity")
            if len(parts) > 1 and parts[1]
            else None
        )
        copilot = (
            WhatsAppWebhookService._positive_or_zero(parts[2], "Copilot quantity")
            if len(parts) > 2 and parts[2]
            else None
        )
        return scenario, base, copilot

    @staticmethod
    def _line_quantity(value: str) -> tuple[str, int]:
        parts = value.split()
        if len(parts) != 2:
            raise ValueError("Use /set LINE_ID QUANTITY.")
        return parts[0].upper(), WhatsAppWebhookService._positive_or_zero(
            parts[1], "Quantity"
        )

    @staticmethod
    def _product_quantity(value: str) -> tuple[str, int]:
        parts = [item.strip() for item in value.split("|")]
        if len(parts) != 2 or not parts[0]:
            raise ValueError("Use /add PRODUCT TITLE | QUANTITY.")
        quantity = WhatsAppWebhookService._positive_or_zero(parts[1], "Quantity")
        if quantity == 0:
            raise ValueError("Added SKU quantity must be greater than zero.")
        return parts[0], quantity

    @staticmethod
    def _replacement(value: str) -> tuple[str, str, int]:
        parts = [item.strip() for item in value.split("|")]
        if len(parts) != 3 or not parts[0] or not parts[1]:
            raise ValueError("Use /replace LINE_ID | PRODUCT TITLE | QUANTITY.")
        quantity = WhatsAppWebhookService._positive_or_zero(parts[2], "Quantity")
        if quantity == 0:
            raise ValueError("Replacement quantity must be greater than zero.")
        return parts[0].upper(), parts[1], quantity

    @staticmethod
    def _positive_or_zero(value: str, name: str) -> int:
        try:
            result = int(value)
        except ValueError as error:
            raise ValueError(f"{name} must be a whole number.") from error
        if result < 0:
            raise ValueError(f"{name} cannot be negative.")
        return result

    @staticmethod
    def _optional_quantity(value: int) -> int | None:
        if value == -1:
            return None
        if value < 0:
            raise ValueError("Quantity cannot be negative.")
        return value

    @staticmethod
    def _required_quantity(value: int, *, allow_zero: bool = True) -> int:
        if value < 0 or (not allow_zero and value == 0):
            qualifier = "zero or greater" if allow_zero else "greater than zero"
            raise ValueError(f"Provide a whole-number quantity {qualifier}.")
        return value

    @staticmethod
    def _required_text(value: str, name: str) -> str:
        result = value.strip()
        if not result or result.casefold() == "none":
            raise ValueError(f"Provide the {name}.")
        return result

    @staticmethod
    def _decimal_value(value: str, name: str) -> Decimal:
        try:
            result = Decimal(value.strip().replace(",", ""))
        except (InvalidOperation, ValueError) as error:
            raise ValueError(f"{name} must be a number.") from error
        if not result.is_finite():
            raise ValueError(f"{name} must be a finite number.")
        return result

    @staticmethod
    def _text_chunks(body: str, limit: int = 4096) -> list[str]:
        chunks: list[str] = []
        remaining = body
        while len(remaining) > limit:
            safe_limit = limit - 4  # reserve room to close an open code block
            split_at = remaining.rfind("\n\n", 0, safe_limit + 1)
            if split_at <= 0:
                split_at = remaining.rfind("\n", 0, safe_limit + 1)
            if split_at <= 0:
                split_at = safe_limit
            chunk = remaining[:split_at].rstrip()
            remaining = remaining[split_at:].lstrip()
            if chunk.count("```") % 2:
                chunk += "\n```"
                remaining = "```\n" + remaining
            chunks.append(chunk)
        if remaining:
            chunks.append(remaining)
        return chunks
