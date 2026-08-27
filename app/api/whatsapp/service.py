from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Literal

from app.config import get_logger, opaque_identifier
from app.core.licensing.analysis import (
    LicenseAnalysisError,
    UnsupportedLicenseLayoutError,
)
from app.core.licensing.capture import (
    CapturedRequirement,
    RequirementCaptureError,
    RequirementExtractor,
)
from app.core.licensing.candidate_policy import (
    candidate_narrowing_question,
    combine_product_qualifier,
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
    render_information_table_images,
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
from app.core.whatsapp import (
    WhatsAppAPIError,
    WhatsAppClient,
    WhatsAppMediaTooLargeError,
)
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
CONVERSATIONAL_ACTIONS = frozenset(
    {"help", "acknowledge", "answer_question", "out_of_scope"}
)


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
    "cancel this",
    "cancel the question",
    "never mind",
    "nevermind",
    "stop this change",
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

FINALIZATION_CORRECTION_ACTIONS = SCENARIO_EDIT_ACTIONS | {
    "set_requirement_detail",
    "set_currency",
    "set_promo",
    "set_discount",
    "set_adjustment",
    "request_recommendation",
    "build_scenario",
}

ASSERTIVE_COMMERCIAL_ACTIONS = FINALIZATION_CORRECTION_ACTIONS | {
    "set_copilot",
    "set_disposition",
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
    simple_price_basis: Literal["marketplace", "distributor_expected"] = "marketplace"
    workflow_mode: Literal[
        "simple_pricing",
        "renewal_only",
        "upgrade_comparison",
        "scenario_comparison",
    ] = "scenario_comparison"
    session_ttl_minutes: int = 5
    inbound_future_clock_skew_seconds: int = 5 * 60


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

    def _message_timestamp_status(
        self,
        message: IncomingWhatsAppMessage,
        *,
        now: datetime | None = None,
    ) -> Literal["current", "stale", "invalid"]:
        """Classify a signed Meta event against the conversation's usable time window.

        Meta message timestamps are Unix seconds. A missing timestamp is accepted for
        backwards compatibility with locally constructed/test payloads, while a supplied
        but malformed timestamp is rejected. Small positive clock drift is allowed; a
        far-future timestamp must not remain eligible indefinitely.
        """

        raw_timestamp = message.timestamp
        if raw_timestamp is None:
            return "current"
        try:
            timestamp = int(raw_timestamp)
            if timestamp <= 0:
                return "invalid"
            event_time = datetime.fromtimestamp(timestamp, tz=UTC)
        except (OverflowError, TypeError, ValueError):
            return "invalid"

        current_time = now or datetime.now(UTC)
        ttl = timedelta(minutes=max(1, self._configuration.session_ttl_minutes))
        future_skew = timedelta(
            seconds=max(0, self._configuration.inbound_future_clock_skew_seconds)
        )
        if event_time > current_time + future_skew:
            return "invalid"
        if event_time + ttl <= current_time:
            return "stale"
        return "current"

    def _is_stale_or_future_message(
        self,
        message: IncomingWhatsAppMessage,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Compatibility predicate for callers that only need an accept/reject result."""

        return self._message_timestamp_status(message, now=now) != "current"

    async def _handle_message(self, message: IncomingWhatsAppMessage) -> None:
        sender = message.sender.lstrip("+")
        message_ref = opaque_identifier(message.id)
        if (
            not self._configuration.allow_all_sellers
            and sender not in self._configuration.seller_allowlist
        ):
            # Meta still receives a successful webhook acknowledgement from the HTTP
            # handler, but an unknown sender must not trigger an outbound message (and
            # therefore must not create either a workflow record or WhatsApp cost).
            logger.warning("Unauthorized WhatsApp sender ignored")
            return
        timestamp_status = self._message_timestamp_status(message)
        if timestamp_status == "stale":
            # Do not apply a delayed instruction to either an expired session or a newer
            # proposal.  An authorized seller still receives one safe recovery step instead
            # of experiencing unexplained silence.  This branch intentionally runs before
            # any workflow read/write, so the saved draft and its TTL remain untouched.
            logger.warning(
                "Out-of-window WhatsApp message not applied message_ref=%s",
                message_ref,
            )
            await self._send_text(
                sender,
                "That message arrived outside the five-minute processing window, so I did "
                "not apply it to the saved requirement or proposal. Please send the current "
                "instruction again; I will process it against the latest session.",
            )
            return
        if timestamp_status == "invalid":
            # The durable dispatcher records a terminal receipt after this handler
            # returns.  Do not touch the seller's workflow ledger here: doing so would
            # refresh or reset a five-minute conversation because Meta delivered an old
            # malformed/future webhook after an outage or deployment.
            logger.warning(
                "Invalid-time WhatsApp message ignored message_ref=%s",
                message_ref,
            )
            return
        # Normalize an expired workflow before inspecting any delivery ledger. The reset
        # keeps every processed/in-flight/failure digest while clearing commercial context.
        # Mutating an expired object first would create an empty replacement session and
        # silently drop the other replay barriers.
        expired = await self._orchestrator.reset_expired_session(sender)
        if await self._orchestrator.has_processed(sender, message.id):
            logger.info("Duplicate WhatsApp message ignored message_ref=%s", message_ref)
            return
        if await self._orchestrator.has_failure_notification(sender, message.id):
            # A prior attempt may already have committed a commercial mutation before an
            # outbound delivery failed. Never execute it again. This is deliberately
            # conservative until a transactional outbox can replay only the missing output.
            await self._send_text(
                sender,
                "I stopped an automatic replay because the earlier request may already "
                "have changed the proposal. Review the latest proposal first; resend the "
                "change only if it is absent.",
            )
            await self._orchestrator.mark_processed(sender, message.id)
            return
        if expired:
            await self._send_text(
                sender,
                "Your previous licensing session expired after five minutes of inactivity. "
                "I have started a new requirement and will not reuse any earlier proposal "
                "details.",
            )

        claim = await self._orchestrator.claim_message_processing(sender, message.id)
        if claim == "processed":
            logger.info("Duplicate WhatsApp message ignored message_ref=%s", message_ref)
            return
        if claim == "inflight":
            # A previous process may have stopped after committing a proposal mutation but
            # before recording completion. Refusing automatic replay is intentionally
            # at-most-once: it prevents duplicate commercial changes and asks the seller to
            # resend only after reviewing the current state.
            await self._send_text(
                sender,
                "An earlier attempt may already have changed the proposal, so I will not "
                "repeat it automatically. Review the current proposal and resend the change "
                "only if it is absent.",
            )
            await self._orchestrator.mark_processed(sender, message.id)
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
        except ScenarioError as error:
            logger.info("User-correctable workflow error type=%s", type(error).__name__)
            await self._send_text(sender, self._seller_safe_scenario_error(error))
            await self._orchestrator.mark_processed(sender, message.id)
        except (
            LicenseAnalysisError,
            RequirementCaptureError,
            WhatsAppMediaTooLargeError,
            ValueError,
        ) as error:
            logger.info("User-correctable workflow error type=%s", type(error).__name__)
            response = (
                self._seller_safe_workflow_error(error)
                if isinstance(error, ValueError)
                else self._professional_agent_text(str(error))
            )
            await self._send_text(
                sender,
                response
                or (
                    "I could not safely read that requirement. Please resend the product "
                    "name and quantity, or upload a clearer supported file."
                ),
            )
            await self._orchestrator.mark_processed(sender, message.id)
        except WorkflowConflictError:
            # A conflict can occur while committing the business mutation *or* later while
            # recording the delivery receipt. Completion is therefore unknown. Never release
            # the in-flight claim and invite an automatic replay: that can duplicate an add,
            # replacement, or comment that already committed.
            logger.warning(
                "Workflow concurrency conflict with unknown completion message_ref=%s",
                message_ref,
            )
            try:
                await self._orchestrator.mark_failure_notified(sender, message.id)
            except WorkflowConflictError:
                logger.warning(
                    "Unable to persist conflict notification marker message_ref=%s",
                    message_ref,
                )
            await self._send_text(
                sender,
                "I could not verify whether that request completed, so I will not repeat it "
                "automatically. Review the latest proposal and resend the change only if it "
                "is absent.",
            )
            try:
                await self._orchestrator.mark_processed(sender, message.id)
            except WorkflowConflictError:
                # Keeping the original in-flight claim is the safe replay barrier.
                logger.warning(
                    "Conflict receipt remains in flight message_ref=%s",
                    message_ref,
                )
        except Exception:
            logger.exception("Unexpected workflow failure message_ref=%s", message_ref)
            if not await self._orchestrator.has_failure_notification(sender, message.id):
                # Persist the replay barrier before attempting another network send. If that
                # send also fails, the queued retry delivers only the recovery notice and
                # cannot repeat a possibly committed edit.
                await self._orchestrator.mark_failure_notified(sender, message.id)
                await self._send_text(
                    sender,
                    "I could not verify whether that request completed, so I have stopped "
                    "automatic replay to prevent a duplicate proposal change. Review the "
                    "latest proposal; resend the change only if it is absent.",
                )
            await self._orchestrator.mark_processed(sender, message.id)
        finally:
            # Active-turn TTL bypass is task-local and must end even if response delivery,
            # failure-ledger persistence, or the completion receipt raises. Persisted
            # in-flight/processed markers continue to enforce replay safety independently.
            self._orchestrator.end_message_processing_context(sender, message.id)

    async def _handle_document(
        self,
        sender: str,
        document: IncomingWhatsAppDocument,
    ) -> None:
        caption = (document.caption or "").strip()
        session = await self._orchestrator.get_session(sender)
        starts_fresh = bool(
            caption
            and self._requests_fresh_start(
                " ".join(caption.casefold().strip(" ?!.,").split())
            )
        )
        if session is not None and session.confirmed_as_is is not None:
            if not starts_fresh:
                await self._send_text(
                    sender,
                    "The current requirement is already confirmed, so I did not replace it "
                    "with this attachment. To begin a new requirement, resend the file with "
                    "the caption 'Start fresh'. To revise the proposal, describe the product "
                    "change and quantity in a message or voice note.",
                )
                return
            await self._orchestrator.reset_session(sender)
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
            max_bytes=self._configuration.max_document_bytes,
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
            except UnsupportedLicenseLayoutError:
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
        if (
            caption
            and not starts_fresh
            and self._caption_contains_post_upload_action(caption)
            and (
                not self._caption_may_duplicate_requirement(caption)
                or not self._caption_repeats_estate_line(caption, estate)
            )
        ):
            await self._handle_text(sender, caption)

    async def _handle_image(
        self,
        sender: str,
        incoming: IncomingWhatsAppImage,
    ) -> None:
        caption = (incoming.caption or "").strip()
        session = await self._orchestrator.get_session(sender)
        starts_fresh = bool(
            caption
            and self._requests_fresh_start(
                " ".join(caption.casefold().strip(" ?!.,").split())
            )
        )
        if session is not None and session.confirmed_as_is is not None:
            if not starts_fresh:
                await self._send_text(
                    sender,
                    "The current requirement is already confirmed, so I did not replace it "
                    "with this image. To begin a new requirement, resend it with the caption "
                    "'Start fresh'. To revise the proposal, describe the product change and "
                    "quantity in a message or voice note.",
                )
                return
            await self._orchestrator.reset_session(sender)
        append_to_draft = await self._has_open_requirement_draft(sender)
        media = await self._whatsapp_client.download_media(
            media_id=incoming.id,
            filename="licensing-requirement-image",
            content_type=incoming.mime_type,
            max_bytes=self._configuration.max_image_bytes,
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
        if (
            caption
            and not starts_fresh
            and self._caption_contains_post_upload_action(caption)
            and (
                not self._caption_may_duplicate_requirement(caption)
                or not self._caption_repeats_estate_line(caption, estate)
            )
        ):
            await self._handle_text(sender, caption)

    @staticmethod
    def _caption_contains_post_upload_action(caption: str) -> bool:
        """Process captions that clearly ask or instruct a second, distinct action.

        Attachment captions are a normal seller turn.  The attachment itself owns the
        requirement rows, while a question such as ``Show me products under INR 5,000``
        must still reach the conversational interpreter.  Descriptive captions that merely
        repeat an extracted row remain excluded by ``_caption_repeats_estate_line``.
        """

        normalized = " ".join(caption.casefold().strip(" ?!.,").split())
        return bool(
            re.match(
                r"^(?:please\s+)?(?:add|include|remove|delete|drop|replace|change|"
                r"update|set|compare|finali[sz]e|confirm|comment|note)\b",
                normalized,
            )
            or re.match(
                r"^(?:please\s+)?(?:show|tell|explain|list|find|recommend|suggest|"
                r"who|what|which|where|when|why|how|can|could|would|should|do|does|"
                r"did|is|are|will|may)\b",
                normalized,
            )
            or caption.rstrip().endswith("?")
            or re.match(
                r"^(?:customer|company|tenant|contact)\s+(?:name|id|email|number)?"
                r"\s*(?:is|:|=)\s*\S+",
                normalized,
            )
        )

    @staticmethod
    def _caption_repeats_estate_line(caption: str, estate) -> bool:
        """Detect when a caption merely repeats a requirement visible in the attachment."""

        normalized = normalize_product_title(caption)
        caption_tokens = set(normalized.split()) - {
            "add",
            "include",
            "licence",
            "licences",
            "license",
            "licenses",
            "please",
            "quantity",
            "qty",
        }
        numbers = {int(value) for value in re.findall(r"\b\d+\b", caption)}
        for line in estate.lines:
            title = normalize_product_title(line.display_title)
            title_tokens = set(title.split()) - {"microsoft", "licence", "license"}
            product_matches = bool(
                title
                and (
                    title in normalized
                    or (
                        title_tokens
                        and len(title_tokens & caption_tokens)
                        / len(title_tokens)
                        >= 0.8
                    )
                )
            )
            if product_matches and (
                not numbers or line.renewal_quantity in numbers
            ):
                return True
        return False

    @staticmethod
    def _caption_may_duplicate_requirement(caption: str) -> bool:
        """Limit row de-duplication to captions that can add an attachment row again."""

        normalized = " ".join(caption.casefold().strip(" ?!.,").split())
        return bool(
            re.match(
                r"^(?:please\s+)?(?:add|include)\b|"
                r"^(?:(?:i|we)\s+)?(?:want|need|require|would\s+like)\b",
                normalized,
            )
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
            max_bytes=self._configuration.max_audio_bytes,
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
        if not captured.extraction.lines:
            if not captured.transcript.strip():
                raise RequirementCaptureError(
                    "I could not identify speech in that voice note. Please try again or "
                    "send the request as text."
                )
            await self._handle_text(sender, captured.transcript)
            return
        session = await self._orchestrator.get_session(sender)
        if session is not None and session.confirmed_as_is is not None:
            # Once pricing exists, a voice note may be a question or proposal operation.
            # Route the transcript through the same conversational intent path as typed text.
            await self._handle_text(sender, captured.transcript)
            return
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
        consume_capture_messages: bool = False,
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
            consume_capture_messages=consume_capture_messages,
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
        session_before_capture = await self._orchestrator.get_session(sender)
        if not await self._is_assertive_requirement_capture(
            message,
            session_before_capture,
        ):
            await self._send_text(
                sender,
                "I treated that as a question or informational statement and did not add "
                "it to the licensing requirement. State the Microsoft product and quantity "
                "directly when you want it included.",
            )
            return
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
            consume_capture_messages=True,
        )
        await self._process_captured_estate(
            sender,
            estate,
            appended=append_to_draft,
        )

    @staticmethod
    def _is_negated_action(reply: str, action_pattern: str) -> bool:
        """Reject a negative statement before any keyword-based action shortcut.

        The intent model remains the primary language interpreter, but these helpers run
        before it.  They therefore have to distinguish ``do not reset`` from ``reset`` and
        ``don't cancel`` from ``cancel`` without relying on a later model correction.
        """

        normalized = " ".join(
            reply.casefold().replace("\u2019", "'").strip(" ?!.,").split()
        )
        return bool(
            re.search(
                rf"\b(?:do not|don't|dont|never|not to|no need(?:\s+(?:to|for))?|need not)\b"
                rf".{{0,48}}(?:{action_pattern})\b",
                normalized,
            )
            or re.search(
                rf"\b(?:{action_pattern})\b.{{0,32}}\b(?:is|are|was|were)\s+not\s+"
                r"(?:needed|required|wanted|approved)\b",
                normalized,
            )
        )

    @classmethod
    def _is_direct_action_request(cls, reply: str, action_pattern: str) -> bool:
        """Return true only for a seller-authored directive.

        This helper is used by several pre-model shortcuts, so a permissive fallback such
        as "every non-question is an instruction" is unsafe.  Narratives, reported speech,
        hypotheticals, and negated examples may contain an action verb without asking the
        advisor to do anything.
        """

        normalized = " ".join(
            reply.casefold().replace("\u2019", "'").strip(" ?!.,").split()
        )
        if not normalized or cls._is_negated_action(reply, action_pattern):
            return False
        if re.match(
            r"^(?:if|unless|whether|suppose|supposing|assuming|imagine|maybe|"
            r"perhaps|possibly|for example|example)\b",
            normalized,
        ):
            return False
        if re.match(
            r"^(?:(?:the\s+)?(?:customer|client|seller|user)|he|she|they)\s+"
            r"(?:said|says|asked|asks|mentioned|mentions|wanted|wants|requested|requests)\b",
            normalized,
        ):
            return False
        direct_polite = bool(re.match(
            rf"^(?:can|could|would|will)\s+you\s+(?:please\s+)?(?:{action_pattern})\b",
            normalized,
        ))
        if "?" in reply and not direct_polite:
            return False
        return direct_polite or bool(
            re.match(rf"^(?:please\s+)?(?:{action_pattern})\b", normalized)
            or re.match(
                rf"^(?:i|we)\s+(?:want|need|require|would\s+like)\s+(?:to\s+)?"
                rf"(?:{action_pattern})\b",
                normalized,
            )
            or re.match(rf"^(?:let'?s|let\s+us)\s+(?:{action_pattern})\b", normalized)
        )

    @classmethod
    def _is_assertive_action_request(cls, reply: str, action_pattern: str) -> bool:
        """Require a seller-authored directive, not a narrative containing action words."""

        normalized = " ".join(
            reply.casefold().replace("\u2019", "'").strip(" ?!.,").split()
        )
        direct_polite = bool(
            re.fullmatch(
                rf"(?:can|could|would|will)\s+you\s+(?:please\s+)?"
                rf"(?:{action_pattern})(?:\s+please)?",
                normalized,
            )
        )
        if not normalized or ("?" in reply and not direct_polite):
            return False
        if cls._is_negated_action(reply, action_pattern):
            return False
        if re.match(
            r"^(?:if|unless|whether|suppose|supposing|assuming|imagine|maybe|"
            r"perhaps|possibly|i\s+might|i\s+may|i\s+am\s+considering|"
            r"i'm\s+considering)\b",
            normalized,
        ):
            return False
        if re.match(
            r"^(?:(?:the\s+)?(?:customer|client|seller|user)|he|she|they)\s+"
            r"(?:said|says|asked|asks|mentioned|mentions|wants?|requested|requests?)\b",
            normalized,
        ):
            return False
        return direct_polite or bool(
            re.fullmatch(rf"(?:please\s+)?(?:{action_pattern})(?:\s+please)?", normalized)
            or re.fullmatch(
                rf"(?:i|we)\s+(?:want|need|would\s+like)\s+to\s+"
                rf"(?:{action_pattern})",
                normalized,
            )
        )

    @classmethod
    def _is_comparison_request(cls, reply: str) -> bool:
        """Recognize a requested result while excluding process questions and negation."""

        comparison_pattern = (
            r"(?:compar(?:e|ing)|(?:show|give|prepare|generate)(?: me| us)?"
            r"(?: a)?(?: enterprise)? comparison)"
        )
        if cls._is_negated_action(reply, r"compar(?:e|ing|ison)"):
            return False
        if cls._is_direct_action_request(reply, comparison_pattern):
            return True
        normalized = " ".join(reply.casefold().strip(" ?!.,").split())
        return bool(
            re.match(
                r"^(?:what(?: is|'s)\s+the\s+(?:difference|cost difference)|"
                r"how\s+do\b.+\bcompare|which\b.+\b(?:costs?|is cheaper|is lower))",
                normalized,
            )
        )

    @classmethod
    def _is_assertive_finalization_request(cls, message: str) -> bool:
        """Accept only an unambiguous seller instruction to begin finalization.

        The language model can occasionally classify a question *about* finalization as the
        ``finalize`` action.  Commercial state must not change from questions, conditional or
        hypothetical wording, quoted/reported speech, or an explicit negation.
        """

        normalized = " ".join(
            message.casefold().replace("\u2019", "'").strip(" ?!.,").split()
        )
        direct_polite_request = bool(
            re.fullmatch(
                r"(?:can|could|would|will)\s+you\s+(?:please\s+)?finali[sz]e"
                r"(?:\s+(?:it|this|the\s+proposal|proposal))?",
                normalized,
            )
        )
        if not normalized or ("?" in message and not direct_polite_request):
            return False
        if re.match(
            r"^(?:who|what|which|where|when|why|how|can|could|would|should|"
            r"do|does|did|is|are|will|may|if|unless|whether|suppose|supposing|"
            r"assuming|imagine)\b",
            normalized,
        ) and not direct_polite_request:
            return False
        if re.search(r"[\"\u201c\u201d\u2018\u2019]", message) or re.match(
            r"^(?:(?:the\s+)?(?:customer|client|seller|user)|he|she|they)\s+"
            r"(?:said|says|asked|asks|wants?|requested|requests?)\b",
            normalized,
        ):
            return False
        if cls._is_negated_action(message, r"finali[sz](?:e|ed|ing|ation)"):
            return False
        return direct_polite_request or bool(
            re.fullmatch(
                r"(?:/finalize|"
                r"(?:(?:yes|yeah|ok|okay)[, ]+)?(?:please\s+)?finali[sz]e"
                r"(?:\s+(?:it|this|the\s+proposal|proposal))?|"
                r"(?:please\s+)?go\s+ahead\s+and\s+finali[sz]e"
                r"(?:\s+(?:it|this|the\s+proposal|proposal))?|"
                r"(?:please\s+)?proceed\s+with\s+finali[sz]ation|"
                r"(?:i|we)\s+(?:want|would\s+like|am\s+ready|are\s+ready)\s+to\s+"
                r"finali[sz]e(?:\s+(?:it|this|the\s+proposal|proposal))?|"
                r"(?:i|we)\s+(?:confirm|approve)\s+(?:it|this|the\s+proposal|proposal)"
                r"\s+for\s+finali[sz]ation)",
                normalized,
            )
        )

    @classmethod
    def _is_explicit_finalization_approval(cls, message: str) -> bool:
        """Return whether a seller explicitly approves the visible final-validation gate."""

        return cls._is_assertive_finalization_request(message) or cls._is_assertive_validation_reply(
            message,
            allow_bare_affirmative=True,
        )

    @classmethod
    def _is_explicit_validation_rejection(cls, message: str) -> bool:
        """Reject validation only from a direct negative answer, never model labelling alone."""

        normalized = " ".join(
            message.casefold().replace("\u2019", "'").strip(" ?!.,").split()
        )
        if not normalized or "?" in message:
            return False
        if re.match(
            r"^(?:who|what|which|where|when|why|how|can|could|would|should|"
            r"do|does|did|is|are|will|may|if|unless|whether|suppose|supposing|"
            r"assuming|imagine)\b",
            normalized,
        ):
            return False
        if re.search(r"[\"\u201c\u201d\u2018\u2019]", message) or cls._is_negated_action(
            message,
            r"(?:reject|cancel)",
        ):
            return False
        return bool(
            re.fullmatch(
                r"(?:no|nope|not\s+yet|"
                r"(?:no[, ]+)?(?:it|(?:this|that|the)\s+(?:requirement|proposal|"
                r"configuration|pricing|details?))\s+(?:is|are)\s+not\s+correct|"
                r"(?:it|(?:this|that|the)\s+(?:requirement|proposal|configuration|"
                r"pricing|details?))\s+(?:is|are)\s+(?:incorrect|wrong)|"
                r"(?:i|we)\s+(?:do\s+not|don't|dont)\s+(?:confirm|approve|accept|"
                r"finali[sz]e)(?:\s+(?:it|(?:this|the)\s+(?:requirement|proposal)))?|"
                r"(?:please\s+)?(?:reject|cancel)(?:\s+(?:it|this|the\s+validation|"
                r"finalization))?|"
                r"(?:it|this|that)\s+needs?\s+(?:a\s+)?(?:change|changes|correction|"
                r"corrections))",
                normalized,
            )
        )

    @staticmethod
    def _seller_scenario_reference(message: str) -> ScenarioType | None:
        """Return a proposal target only when the seller actually wrote it."""

        normalized = " ".join(message.casefold().replace("-", " ").split())
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

    @classmethod
    def _seller_target_quantity(cls, message: str, action: str) -> int | None:
        """Resolve the requested *new* quantity, not an old/source number.

        Membership in the seller's numbers is insufficient for sentences such as
        ``change L1 from 100 to 50``.  Prefer the value in the target grammatical role;
        use the sole remaining quantity only when the message is unambiguous.
        """

        normalized = " ".join(
            message.casefold().replace("\u2019", "'").replace(",", "").split()
        )
        numeric_targets = [
            int(value)
            for value in re.findall(
                r"\b(?:from\s+\d+\s+)?(?:to|at|make\s+it)\s+(\d+)\b",
                normalized,
            )
        ]
        word_pattern = cls._number_word_pattern()
        for match in re.finditer(
            rf"\b(?:to|at|make\s+it)\s+(?P<number>{word_pattern})\b",
            normalized,
        ):
            parsed = cls._number_words_to_int(match.group("number"))
            if parsed is not None:
                numeric_targets.append(parsed)
        if numeric_targets:
            return numeric_targets[-1]

        values = cls._seller_quantity_values(message)
        if len(values) == 1:
            return next(iter(values))
        if action == "add_sku":
            anchored = re.search(
                r"\b(?:add|include|order|buy|purchase|need|want|require)\s+"
                r"(?:about\s+|around\s+|approximately\s+)?(\d+)\b",
                normalized,
            )
            if anchored is not None:
                return int(anchored.group(1))
        return None

    @staticmethod
    def _phrase_is_seller_grounded(supplied: str, seller_message: str) -> bool:
        """Match a normalized phrase on token boundaries, never raw substrings."""

        supplied_value = normalize_product_title(supplied)
        seller_value = normalize_product_title(seller_message)
        if not supplied_value:
            return False
        return bool(
            re.search(
                rf"(?<![a-z0-9]){re.escape(supplied_value)}(?![a-z0-9])",
                seller_value,
            )
        )

    @staticmethod
    def _explicit_requirement_detail(message: str) -> tuple[str, str] | None:
        """Parse an explicit proposal-metadata assignment from the seller's own words."""

        match = re.match(
            r"^\s*(?P<label>(?:customer|company|tenant|contact|opportunity|proposal)"
            r"(?:\s+(?:name|reference|number|id|email|note))?)\s*"
            r"(?:is|:|=)\s*(?P<value>\S(?:.*\S)?)\s*[.!]?\s*$",
            message,
            flags=re.IGNORECASE,
        )
        if match is None:
            return None
        label = " ".join(match.group("label").casefold().split())
        value = match.group("value").strip().rstrip(".! ")
        return (label, value) if value else None

    @classmethod
    def _seller_product_role_is_supported(
        cls,
        message: str,
        supplied_product: str,
        action: str,
    ) -> bool:
        """Reject a source/rejected product that the model placed in the target role."""

        supplied = normalize_product_title(supplied_product)
        normalized = normalize_product_title(message)
        if not supplied or not cls._phrase_is_seller_grounded(supplied, normalized):
            return False
        alternative = re.search(r"\b(?:instead\s+of|rather\s+than)\b", normalized)
        if alternative is not None:
            preferred = normalized[: alternative.start()].strip()
            rejected = normalized[alternative.end() :].strip()
            if cls._phrase_is_seller_grounded(supplied, rejected) and not cls._phrase_is_seller_grounded(
                supplied, preferred
            ):
                return False
        if action == "replace_sku":
            replacement = re.search(
                r"\breplace\b.+?\b(?:with|by)\b(?P<target>.+)$",
                normalized,
            )
            if replacement is not None and not cls._phrase_is_seller_grounded(
                supplied, replacement.group("target")
            ):
                return False
        return True

    @staticmethod
    def _seller_preferred_product_query(message: str, action: str) -> str:
        """Extract the seller's preferred side of an explicit alternative."""

        normalized = " ".join(message.strip().split())
        alternative = re.search(
            r"\b(?:instead\s+of|rather\s+than)\b",
            normalized,
            flags=re.IGNORECASE,
        )
        if alternative is None:
            return ""
        preferred = normalized[: alternative.start()].strip(" ,.;")
        if action == "add_sku":
            preferred = re.sub(
                r"^(?:please\s+)?(?:add|include|order|buy|purchase)\s+"
                r"(?:about\s+|around\s+|approximately\s+)?\d+\s+",
                "",
                preferred,
                flags=re.IGNORECASE,
            )
            preferred = re.sub(
                r"\s+\d+\s+(?:(?:licence|license)s?|users?|seats?)$",
                "",
                preferred,
                flags=re.IGNORECASE,
            )
            preferred = re.sub(
                r"\s+(?:(?:licence|license)s?|subscriptions?)$",
                "",
                preferred,
                flags=re.IGNORECASE,
            )
        return preferred.strip(" ,.;")

    @staticmethod
    def _seller_comment_text(message: str) -> str:
        """Extract an explicit seller comment while preserving wording and polarity."""

        match = re.match(
            r"^\s*(?:please\s+)?(?:add|include|record|note)\s+"
            r"(?:a\s+)?(?:comment|note|remark|assumption)"
            r"(?:\s+(?:that|is)|\s*[:=])?\s+(?P<comment>\S(?:.*\S)?)\s*$",
            message,
            flags=re.IGNORECASE,
        )
        if match is None:
            return ""
        return match.group("comment").strip().rstrip(".")

    def _intent_with_seller_supported_targets(
        self,
        intent: AgentIntent,
        message: str,
        session: WorkflowSession | None,
        *,
        pending_slot_completion: bool,
    ) -> AgentIntent:
        """Remove model-invented line/scenario targets before commercial dispatch."""

        if session is None or pending_slot_completion:
            return intent
        updates: dict[str, object] = {}

        # A model-proposed catalogue query may expand a vague seller phrase, but it must
        # never invent an unrelated product and thereby create an exact-match mutation.
        # Keep only queries with at least one seller-authored product-bearing token. The
        # catalogue confirmation layer remains responsible for resolving broad terms such
        # as "Copilot" or "E5" to an exact SKU.
        supplied_product = str(getattr(intent, "product_query", "") or "").strip()
        if supplied_product:
            role_sensitive = bool(
                re.search(
                    r"\b(?:instead\s+of|rather\s+than)\b|"
                    r"\breplace\b.+?\b(?:with|by)\b",
                    normalize_product_title(message),
                )
            )
            if role_sensitive and not self._seller_product_role_is_supported(
                message,
                supplied_product,
                intent.action,
            ):
                updates["product_query"] = self._seller_preferred_product_query(
                    message,
                    intent.action,
                )
            ignored_product_tokens = {
                "add",
                "change",
                "for",
                "include",
                "licence",
                "licences",
                "license",
                "licenses",
                "microsoft",
                "365",
                "product",
                "replace",
                "sku",
                "the",
                "to",
                "with",
            }
            supplied_tokens = (
                set(normalize_product_title(supplied_product).split())
                - ignored_product_tokens
            )
            seller_tokens = (
                set(normalize_product_title(message).split())
                - ignored_product_tokens
            )
            seller_plan_codes = {
                value.casefold()
                for value in re.findall(
                    r"\b(?:m?e\d+|p\d+|f\d+)\b",
                    normalize_product_title(message),
                )
            }
            seller_plan_codes.update(
                f"p{value}"
                for value in re.findall(
                    r"\bplan\s*(\d+)\b",
                    normalize_product_title(message),
                )
            )
            seller_tokens.update(seller_plan_codes)
            supplied_plan_codes = {
                value.casefold()
                for value in re.findall(
                    r"\b(?:m?e\d+|p\d+|f\d+)\b",
                    normalize_product_title(supplied_product),
                )
            }
            unsupported_plan = bool(supplied_plan_codes - seller_plan_codes)
            seller_suite_families: set[str] = set()
            supplied_suite_families: set[str] = set()
            seller_product_text = normalize_product_title(message)
            supplied_product_text = normalize_product_title(supplied_product)
            if re.search(r"\b(?:office\s*365|o365)\b", seller_product_text):
                seller_suite_families.add("office_365")
            if re.search(r"\b(?:microsoft\s*365|m365)\b", seller_product_text):
                seller_suite_families.add("microsoft_365")
            if re.search(r"\b(?:office\s*365|o365)\b", supplied_product_text):
                supplied_suite_families.add("office_365")
            if re.search(r"\b(?:microsoft\s*365|m365)\b", supplied_product_text):
                supplied_suite_families.add("microsoft_365")
            unsupported_suite = bool(
                supplied_suite_families
                and supplied_suite_families != seller_suite_families
            )
            if "product_query" not in updates and (unsupported_suite or (
                supplied_tokens
                and (
                    unsupported_plan
                    or not supplied_tokens.issubset(seller_tokens)
                )
            )):
                updates["product_query"] = ""

        seller_numbers = self._seller_quantity_values(message)
        raw_quantity = getattr(intent, "quantity", -1)
        supplied_quantity = int(raw_quantity if raw_quantity is not None else -1)
        if supplied_quantity >= 0:
            target_quantity = (
                self._seller_target_quantity(message, intent.action)
                if intent.action in {"add_sku", "replace_sku", "set_quantity"}
                else None
            )
            if target_quantity is not None and supplied_quantity != target_quantity:
                updates["quantity"] = target_quantity
            elif (
                target_quantity is None and supplied_quantity not in seller_numbers
            ):
                updates["quantity"] = -1
        raw_copilot = getattr(intent, "copilot_quantity", -1)
        supplied_copilot = int(raw_copilot if raw_copilot is not None else -1)
        if supplied_copilot >= 0:
            target_copilot = self._seller_target_quantity(message, "set_copilot")
            if target_copilot is not None and supplied_copilot != target_copilot:
                updates["copilot_quantity"] = target_copilot
            elif (
                target_copilot is None and supplied_copilot not in seller_numbers
            ):
                updates["copilot_quantity"] = -1

        normalized_message = normalize_product_title(message)
        disposition = str(getattr(intent, "disposition", "none") or "none")
        disposition_terms = {
            "retain": r"\b(?:retain|keep)\b",
            "remove": r"\b(?:remove|delete|drop|exclude)\b",
            "migrate": r"\bmigrat(?:e|ed|ing|ion)\b",
            "included": r"\b(?:include|included|bundle|bundled)\b",
        }
        if disposition != "none" and not re.search(
            disposition_terms.get(disposition, r"(?!)"),
            normalized_message,
        ):
            updates["disposition"] = "none"

        supplied_boolean = str(getattr(intent, "boolean_value", "none") or "none")
        boolean_terms = {
            "true": r"\b(?:yes|on|enable|enabled|apply|eligible|true)\b",
            "false": r"\b(?:no|off|disable|disabled|remove|ineligible|false)\b",
        }
        if supplied_boolean != "none" and not re.search(
            boolean_terms.get(supplied_boolean, r"(?!)"),
            normalized_message,
        ):
            updates["boolean_value"] = "none"

        supplied_percentage = Decimal(str(getattr(intent, "percentage", -1.0)))
        seller_percentages = self._seller_percentage_values(message)
        if supplied_percentage >= 0 and supplied_percentage not in seller_percentages:
            updates["percentage"] = -1.0
        supplied_amount = Decimal(str(getattr(intent, "amount", 0.0) or 0.0))
        seller_adjustments = self._seller_adjustment_values(message)
        if supplied_amount != 0 and supplied_amount not in seller_adjustments:
            updates["amount"] = 0.0

        text_fields = {
            "segment": "segment",
            "currency": "currency",
            "comment": "comment",
        }
        for attribute, update_name in text_fields.items():
            supplied_value = normalize_product_title(
                str(getattr(intent, attribute, "") or "")
            )
            text_is_grounded = self._phrase_is_seller_grounded(
                supplied_value,
                normalized_message,
            )
            if update_name == "comment" and supplied_value:
                # The model may remove harmless grammar from a seller-authored note
                # ("customer approval is pending" -> "customer approval pending").
                # Accept that normalization only when every retained content token came
                # from the seller; a model-added fact still fails closed.
                harmless = {"a", "an", "are", "is", "the", "was", "were"}
                supplied_comment_tokens = [
                    token for token in supplied_value.split() if token not in harmless
                ]
                seller_comment_tokens = [
                    token for token in normalized_message.split() if token not in harmless
                ]
                cursor = 0
                for token in seller_comment_tokens:
                    if (
                        cursor < len(supplied_comment_tokens)
                        and token == supplied_comment_tokens[cursor]
                    ):
                        cursor += 1
                text_is_grounded = cursor == len(supplied_comment_tokens)
                seller_negated = bool(
                    re.search(r"\b(?:not|no|never|without|don't|dont)\b", normalized_message)
                )
                supplied_negated = bool(
                    re.search(r"\b(?:not|no|never|without|don't|dont)\b", supplied_value)
                )
                if seller_negated and not supplied_negated:
                    text_is_grounded = False
            if supplied_value and not text_is_grounded:
                updates[update_name] = ""
        if intent.action == "add_comment":
            seller_comment = self._seller_comment_text(message)
            if seller_comment:
                updates["comment"] = seller_comment

        term_value = str(getattr(intent, "term_duration", "") or "").strip()
        if term_value:
            supported_term = bool(
                self._phrase_is_seller_grounded(term_value, normalized_message)
                or (
                    term_value.casefold() == "p1y"
                    and re.search(r"\b(?:annual|one year|1 year|12 months?|p1y)\b", normalized_message)
                )
            )
            if not supported_term:
                updates["term_duration"] = ""
        billing_value = str(getattr(intent, "billing_plan", "") or "").strip()
        if billing_value and not self._phrase_is_seller_grounded(
            billing_value,
            normalized_message,
        ):
            updates["billing_plan"] = ""

        if intent.action == "set_requirement_detail":
            written_detail = self._explicit_requirement_detail(message)
            if written_detail is None:
                updates["detail_label"] = ""
                updates["detail_value"] = ""
            else:
                grounded_label, grounded_value = written_detail
                if str(getattr(intent, "detail_label", "") or "").strip() != grounded_label:
                    updates["detail_label"] = grounded_label
                if str(getattr(intent, "detail_value", "") or "").strip() != grounded_value:
                    updates["detail_value"] = grounded_value
        supplied_line = str(getattr(intent, "line_id", "") or "").strip().upper()
        if supplied_line:
            requirement = session.confirmed_as_is is None
            if requirement and session.estate is not None:
                lines = list(session.estate.lines)
            elif session.active_scenario is not None:
                active = session.scenarios.get(session.active_scenario)
                lines = (
                    [line for line in active.lines if line.proposed_quantity > 0]
                    if active is not None
                    else []
                )
            else:
                lines = []
            supported = len(lines) == 1
            explicit_ids = {
                value.upper()
                for value in re.findall(r"\bL\d+\b", message, flags=re.IGNORECASE)
            }
            inferred = self._line_reference_from_message(
                lines,
                message,
                requirement=requirement,
            )
            supported = supported or supplied_line in explicit_ids or inferred == supplied_line
            if not supported:
                updates["line_id"] = ""

        stated_scenario = str(getattr(intent, "scenario", "none") or "none")
        if stated_scenario != "none":
            seller_scenario = self._seller_scenario_reference(message)
            if seller_scenario is None:
                updates["scenario"] = "none"
            elif seller_scenario is not None and seller_scenario.value != stated_scenario:
                updates["scenario"] = seller_scenario.value
        return self._agent_intent_with(intent, **updates) if updates else intent

    @classmethod
    def _commercial_action_has_domain_evidence(
        cls,
        message: str,
        intent: AgentIntent,
        session: WorkflowSession | None,
    ) -> bool:
        """Require the object that makes an imperative a licensing operation."""

        normalized = normalize_product_title(message)
        action = intent.action
        line_ids = set(re.findall(r"\bl\d+\b", normalized))
        product_query = str(getattr(intent, "product_query", "") or "").strip()
        session_product = False
        if session is not None:
            candidate_titles: list[str] = []
            if session.confirmed_as_is is None and session.estate is not None:
                candidate_titles.extend(line.display_title for line in session.estate.lines)
            elif session.active_scenario is not None:
                scenario = session.scenarios.get(session.active_scenario)
                if scenario is not None:
                    candidate_titles.extend(line.sku_title for line in scenario.lines)
            session_product = any(
                len(title_value.split()) >= 2
                and title_value in normalized
                for title_value in map(normalize_product_title, candidate_titles)
                if title_value
            )

        if action == "add_sku":
            # A direct instruction can be valid while still missing the exact product,
            # for example ``Add 10 licences``. The product must never come from the
            # model alone, but seller-authored licensing-object wording is sufficient to
            # enter the persisted missing-product dialogue. The separate directness,
            # negation, hypothetical and reported-speech guards still have to pass.
            return bool(
                product_query
                or re.search(
                    r"\b(?:licen[cs]es?|skus?|products?|plans?|subscriptions?|"
                    r"seats?|users?)\b",
                    normalized,
                )
            )
        if action == "replace_sku":
            # Likewise, ``Replace L2`` is an incomplete but meaningful instruction. It
            # may ask for the missing target product; it must not invent that product.
            return bool(
                product_query
                or line_ids
                or session_product
                or re.search(
                    r"\b(?:licen[cs]e|sku|product|plan|line|it|this|current)\b",
                    normalized,
                )
            )
        if action == "set_quantity":
            return bool(
                line_ids
                or session_product
                or re.search(
                    r"\b(?:qty|quantity|licen[cs]es?|users?|seats?|subscriptions?)\b",
                    normalized,
                )
            )
        if action == "set_copilot":
            return bool(re.search(r"\bcopilot\b", normalized))
        if action == "set_disposition":
            return bool(
                line_ids
                or session_product
                or re.search(r"\b(?:licen[cs]e|sku|product|line|it|this)\b", normalized)
            )
        if action == "build_scenario":
            return cls._seller_scenario_reference(message) is not None
        if action == "set_term":
            return bool(
                re.search(r"\b(?:term|contract|annual|yearly|p\d+[ymd]|months?|years?)\b", normalized)
            )
        if action == "set_billing":
            return bool(re.search(r"\b(?:billing|bill|annual|yearly|monthly)\b", normalized))
        if action == "set_segment":
            return bool(
                re.search(
                    r"\b(?:segment|commercial|education|academic|government|"
                    r"nonprofit|non profit|charity)\b",
                    normalized,
                )
            )
        if action == "set_currency":
            return bool(
                re.search(r"\b(?:currency|inr|usd|eur|gbp|rupees?|dollars?|euros?)\b", normalized)
            )
        if action == "add_comment":
            return bool(re.search(r"\b(?:comment|note|remark|assumption)\b", normalized))
        if action == "set_requirement_detail":
            return cls._explicit_requirement_detail(message) is not None
        if action == "set_promo":
            return bool(re.search(r"\b(?:promo|promotion|eligibility|eligible)\b", normalized))
        if action == "set_discount":
            return bool(re.search(r"\b(?:discount|percent|percentage|%)\b", message.casefold()))
        if action == "set_adjustment":
            return bool(
                re.search(
                    r"\b(?:adjustment|adjust|subtract|deduct|increase|decrease)\b",
                    normalized,
                )
            )
        if action == "request_recommendation":
            return session is not None or bool(
                re.search(r"\b(?:licen[cs]e|sku|microsoft|plan|product)\b", normalized)
            )
        return True

    @classmethod
    def _is_assertive_commercial_instruction(
        cls,
        message: str,
        intent: AgentIntent,
        session: WorkflowSession | None,
        *,
        pending_slot_completion: bool,
    ) -> bool:
        """Require seller-authored action evidence before arming or applying a mutation."""

        if pending_slot_completion:
            return True
        action = intent.action
        if action not in ASSERTIVE_COMMERCIAL_ACTIONS:
            return True
        normalized = " ".join(
            message.casefold().replace("\u2019", "'").strip(" ?!.,").split()
        )
        if not normalized:
            return False
        # These phrases explicitly preserve the current state. They are not mutations even
        # when a model emits a commercial action because the sentence also contains a SKU,
        # scenario, quantity, or another action noun.
        if re.search(
            r"\b(?:no\s+need(?:\s+(?:to|for))?|need\s+not|not\s+(?:needed|required)|"
            r"do\s+not\s+(?:need|want|require)|don't\s+(?:need|want|require)|"
            r"dont\s+(?:need|want|require))\b",
            normalized,
        ) or re.search(
            r"\b(?:leave|keep)\b.{0,60}\b(?:unchanged|as[ -]?is|the\s+same)\b",
            normalized,
        ):
            return False
        if re.match(
            r"^(?:if|unless|whether|suppose|supposing|assuming|imagine|"
            r"for example|example)\b",
            normalized,
        ) or re.search(
            r"\b(?:for example|as an example|hypothetically)\b",
            normalized,
        ):
            return False
        if re.search(
            r"\b(?:customer|client|seller|user|manager|analyst)\b.{0,48}"
            r"\b(?:said|says|asked|asks|mentioned|mentions|requested|requests?)\b|"
            r"\b(?:said|asked|mentioned|requested)\b.{0,24}"
            r"\b(?:customer|client|seller|user|manager|analyst)\b",
            normalized,
        ):
            return False
        if re.search(r"\bbut\s+not\b|\bnot\s+(?:to\s+)?-?\d+(?:\.\d+)?\b", normalized):
            return False
        if re.match(
            r"^(?:(?:the\s+)?(?:customer|client|seller|user)|he|she|they)\s+"
            r"(?:said|says|asked|asks|mentioned|mentions|wants?|requested|requests?)\b",
            normalized,
        ):
            return False
        if re.search(
            r"\b(?:want|need|would\s+like)\s+to\s+know\b|"
            r"\b(?:want|need|would\s+like)\s+(?:a\s+)?(?:pricing|price|quote|cost)\b|"
            r"^(?:please\s+)?(?:tell|explain)\b|"
            r"\bshow\s+me\s+(?:what|how|whether)\b|"
            r"\b(?:what\s+would|would\s+it)\b.{0,80}\b(?:cost|price|happen)\b|"
            r"\b(?:price|cost)\b.{0,40}\bif\b",
            normalized,
        ):
            return False
        if re.search(r"[\"\u201c\u201d].*\b(?:add|replace|change|set|remove|build|finali[sz]e)\b", message, re.I):
            return False

        # Only action verbs count as imperative evidence. Product/scenario/attribute nouns
        # (ME5, Copilot, annual, USD, quantity) are deliberately absent: a sentence such as
        # "ME5 is expensive" or "USD is volatile" must remain read-only even if the model
        # assigns a mutation label.
        action_terms = {
            "add_sku": r"(?:add|include|order|purchase|buy)",
            "replace_sku": r"(?:replace|switch|swap|change|upgrade|downgrade)",
            "set_quantity": r"(?:change|set|update|make)",
            "set_copilot": r"(?:change|set|update|make)",
            "set_disposition": r"(?:retain|remove|delete|drop|migrate|include|exclude)",
            "build_scenario": r"(?:build|prepare|create|show|use|select|choose|evaluate)",
            "set_term": r"(?:set|change|update|make|use)",
            "set_billing": r"(?:set|change|update|make|use)",
            "set_segment": r"(?:set|change|update|make|use)",
            "set_currency": r"(?:set|change|update|make|use|convert)",
            "add_comment": r"(?:add|include|record|note)",
            "set_requirement_detail": r"(?:set|change|update|record|use)",
            "request_recommendation": r"(?:recommend|suggest|advice|advise|guidance)",
            "set_promo": r"(?:apply|enable|disable|remove|use)",
            "set_discount": r"(?:apply|set|change|remove|use)",
            "set_adjustment": (
                r"(?:apply|set|change|remove|adjust|use|add|increase|subtract|"
                r"deduct|decrease)"
            ),
        }
        evidence = action_terms.get(action, r"(?:add|change|set|update)")
        direct_polite = bool(
            re.match(
                rf"^(?:can|could|would|will)\s+you\s+(?:please\s+)?{evidence}\b",
                normalized,
            )
        )
        recommendation_question = (
            action == "request_recommendation"
            and cls._is_direct_recommendation_request(normalized)
        )
        if ("?" in message or re.match(
            r"^(?:who|what|which|where|when|why|how|can|could|would|should|"
            r"do|does|did|is|are|will|may)\b",
            normalized,
        )) and not (direct_polite or recommendation_question):
            return False
        if cls._is_negated_action(message, evidence):
            return False
        has_action_verb = bool(re.search(rf"\b{evidence}\b", normalized))
        direct_action = direct_polite or bool(
            re.match(rf"^(?:please\s+)?{evidence}\b", normalized)
            or re.match(
                rf"^(?:i|we)\s+(?:want|need|would\s+like)\s+(?:to\s+)?{evidence}\b",
                normalized,
            )
        )
        if has_action_verb and direct_action:
            return cls._commercial_action_has_domain_evidence(
                message,
                intent,
                session,
            )

        # Explicit first-person procurement is a direct addition even without the word
        # "add" (for example, "I need 10 Power BI Pro licences"). Informational pricing
        # requests were rejected above.
        if action == "add_sku" and re.match(
            r"^(?:(?:i|we)\s+)?(?:want|need|require|would\s+like)\b",
            normalized,
        ):
            return cls._commercial_action_has_domain_evidence(message, intent, session)

        # Safe shorthand forms are intentionally full-message grammars. A bare number is
        # accepted only by a persisted visible quantity slot, never here; narratives such as
        # "L1 cost 50 last year" cannot change a proposal.
        if action == "set_quantity" and re.fullmatch(
            r"(?:l\d+\s+(?:to\s+)?\d+(?:\s+(?:licen[cs]es?|users?|seats?|qty|quantity))?|"
            r"\d+(?:\s+(?:licen[cs]es?|users?|seats?|qty|quantity))?\s+(?:for\s+)?l\d+)",
            normalized,
        ):
            return True
        if action == "set_copilot" and re.fullmatch(
            r"(?:(?:microsoft\s+365\s+)?copilot(?:\s+(?:qty|quantity|count))?\s+"
            r"(?:to\s+)?\d+(?:\s+(?:licen[cs]es?|users?|seats?))?|"
            r"\d+(?:\s+(?:licen[cs]es?|users?|seats?))?\s+(?:for\s+)?"
            r"(?:microsoft\s+365\s+)?copilot)",
            normalized,
        ):
            return True
        if action == "build_scenario" and re.fullmatch(
            r"(?:me3|me5|me7|renew(?:al)?\s+as[ -]?is)", normalized
        ):
            return True
        if action == "build_scenario" and re.match(
            r"^(?:(?:i|we)\s+)?(?:want|need|would\s+like)\s+(?:the\s+)?"
            r"(?:me3|me5|me7|renew(?:al)?\s+as[ -]?is)(?:\s+(?:option|proposal))?$",
            normalized,
        ):
            return True
        if action == "set_requirement_detail" and re.match(
            r"^(?:customer|company|tenant|contact)\s+(?:name|id|email|number)?\s*"
            r"(?:is|:|=)\s*\S+",
            normalized,
        ):
            return True
        if action == "add_comment" and re.match(
            r"^(?:comment|note|remark)\s*(?::|is|=)\s*\S+",
            normalized,
        ):
            return True
        if action == "set_discount" and re.fullmatch(
            r"\d+(?:\.\d+)?\s*%\s+discount", normalized
        ):
            return True
        if action == "set_adjustment" and re.fullmatch(
            r"(?:commercial\s+)?adjustment\s+(?:of\s+)?-?\d+(?:\.\d+)?",
            normalized,
        ):
            return True
        return recommendation_question

    @staticmethod
    def _is_direct_recommendation_request(normalized: str) -> bool:
        """Recognize direct seller requests for advice without treating hypotheticals as edits."""

        patterns = (
            r"^(?:what|which)\b.*\b(?:recommend|suggest|advise|advice|guidance)\b",
            r"^(?:please\s+)?(?:recommend|suggest|advise)\b",
            r"^(?:can|could|would|will)\s+you\s+(?:please\s+)?(?:"
            r"(?:give|share|provide)(?:\s+me)?\s+(?:(?:a|some)\s+)?"
            r"(?:recommendations?|suggestions?|advice|guidance)|"
            r"recommend|suggest|advise)\b",
            r"^(?:can|could|should|would)\s+(?:i|we)\s+"
            r"(?:upgrade|downgrade|switch|replace|use|choose|pick)\b",
            r"^(?:i|we)\s+(?:want|need|would\s+like)\s+"
            r"(?:(?:a|some)\s+)?(?:recommendations?|suggestions?|advice|guidance)\b",
        )
        return any(re.search(pattern, normalized) for pattern in patterns)

    @classmethod
    def _requests_fresh_start(cls, reply: str) -> bool:
        normalized = " ".join(
            reply.casefold().replace("\u2019", "'").strip(" ?!.,").split()
        )
        if cls._is_negated_action(
            reply,
            r"(?:start|reset|clear|begin)(?:\s+(?:fresh|again|everything|the\s+requirement|a\s+new\s+requirement))?",
        ):
            return False
        direct_polite = re.fullmatch(
            r"(?:can|could|would|will)\s+you\s+(?:please\s+)?(?P<request>.+)",
            normalized,
        )
        if direct_polite is not None:
            normalized = direct_polite.group("request")
        elif "?" in reply:
            return False
        normalized = re.sub(r"^(?:please\s+|just\s+)+", "", normalized)
        normalized = re.sub(
            r"^(?:(?:i|we)\s+(?:want|need|would\s+like)\s+to\s+|"
            r"let(?:'s|\s+us)\s+)",
            "",
            normalized,
        )
        return normalized in RESET_REQUESTS or bool(
            re.fullmatch(
                r"(?:start|begin)(?:\s+(?:again|fresh|over|from\s+scratch)|"
                r"\s+(?:a\s+)?new\s+(?:licensing\s+)?requirement)|"
                r"(?:reset|clear)(?:\s+(?:everything|the\s+(?:draft|requirement)|"
                r"this\s+(?:draft|requirement)))?|"
                r"(?:discard|clear)\s+(?:the\s+)?(?:old|previous|saved)?\s*"
                r"draft\s+(?:and|then)\s+(?:start|begin)\s+"
                r"(?:a\s+)?new\s+(?:licensing\s+)?requirement",
                normalized,
            )
        )

    @classmethod
    def _requests_resume_saved_draft(cls, reply: str) -> bool:
        """Recognize a direct request to resume without relying on exact canned text."""

        normalized = " ".join(
            reply.casefold().replace("\u2019", "'").strip(" ?!.,").split()
        )
        if not normalized or cls._is_negated_action(
            reply,
            r"(?:resume|continue|show|open)(?:\s+(?:it|the\s+(?:draft|proposal)|"
            r"my\s+(?:draft|proposal)|where\s+we\s+(?:stopped|left\s+off)))?",
        ):
            return False
        direct_polite = re.fullmatch(
            r"(?:can|could|would|will)\s+(?:you|we)\s+(?:please\s+)?(?P<request>.+)",
            normalized,
        )
        if direct_polite is not None:
            normalized = direct_polite.group("request")
        elif "?" in reply:
            return False
        normalized = re.sub(r"^(?:please\s+|just\s+)+", "", normalized)
        normalized = re.sub(
            r"^(?:(?:i|we)\s+(?:want|need|would\s+like)\s+to\s+|"
            r"let(?:'s|\s+us)\s+)",
            "",
            normalized,
        )
        return normalized in RESUME_REQUESTS or bool(
            re.fullmatch(
                r"(?:resume|continue|open|show)(?:\s+with)?"
                r"(?:\s+(?:it|my|the))?\s*"
                r"(?:saved\s+)?(?:draft|proposal|requirement|session)|"
                r"(?:resume|continue)\s+(?:where\s+)?we\s+"
                r"(?:stopped|left\s+off)",
                normalized,
            )
        )

    @classmethod
    def _requests_enterprise_comparison(cls, reply: str) -> bool:
        if "compare" not in reply:
            return False
        if not cls._is_comparison_request(reply):
            return False
        compact = " ".join(reply.split())
        if re.search(r"\b(?:all|other|the)?\s*(?:4|four)\b", compact):
            return True
        return all(value in compact for value in ("me3", "me5", "me7"))

    @classmethod
    def _requests_pending_change_cancel(cls, reply: str) -> bool:
        normalized = " ".join(
            reply.casefold().replace("\u2019", "'").strip(" ?!.,").split()
        )
        if not normalized or "?" in reply:
            return False
        if re.match(
            r"^(?:(?:the\s+)?(?:customer|client|seller|user)|he|she|they)\s+"
            r"(?:said|says|asked|asks|mentioned|mentions|requested|requests?)\b",
            normalized,
        ):
            return False
        if re.fullmatch(
            r"(?:please\s+)?(?:do\s+not|don't|dont|never)\s+cancel"
            r"(?:\s+(?:(?:it|this|that)(?:\s+(?:change|question))?|"
            r"the\s+(?:change|question)))?",
            normalized,
        ):
            return False
        if re.fullmatch(
            r"(?:(?:please\s+)?(?:i\s+)?(?:do\s+not|don't|dont)\s+want\s+to\s+"
            r"(?:make|apply|continue|proceed\s+with)\s+(?:this|that|the)\s+"
            r"(?:change|replacement|selection)|"
            r"(?:please\s+)?(?:forget|drop|skip|stop)\s+(?:this|that|the)\s+"
            r"(?:change|replacement|selection)|"
            r"(?:that|this)\s+is\s+not\s+(?:the\s+)?(?:product|sku|option)\s+"
            r"i\s+(?:meant|wanted|need))",
            normalized,
        ):
            return True
        if re.fullmatch(
            r"(?:(?:wait|actually|on\s+second\s+thought)[,;]?\s+)?"
            r"let(?:'s|\s+us)\s+not\s+replace(?:\s+it)?"
            r"(?:\s*(?:;|,|and|then)\s*(?:please\s+)?compare"
            r"(?:\s+(?:it\s+)?with\s+another\s+proposal|\s+the\s+proposals?)?)?",
            normalized,
        ):
            return True
        return bool(
            re.fullmatch(
                r"(?:please\s+)?(?:"
                r"cancel(?:\s+(?:(?:it|this|that)(?:\s+(?:change|question))?|"
                r"the\s+(?:change|question)))?|"
                r"abandon(?:\s+(?:the\s+)?)?(?:(?:catalogue|product|sku)\s+)?"
                r"(?:selection|choice|change)|"
                r"discard(?:\s+(?:the\s+)?)?(?:(?:catalogue|product|sku)\s+)?"
                r"(?:selection|choice|change)|"
                r"never\s+mind|nevermind|stop\s+this\s+change|"
                r"(?:do\s+not|don't|dont|not)\s+replace(?:\s+it)?|"
                r"(?:leave|keep)\s+it\s+(?:unchanged|as[ -]?is))"
                r"(?:\s+please)?",
                normalized,
            )
        )

    @staticmethod
    def _professional_agent_text(value: str) -> str:
        """Keep model-authored seller copy in professional English."""

        value = re.sub(
            r"\[([^\]]+)\]\(https?://[^)]+\)",
            r"\1",
            value,
            flags=re.IGNORECASE,
        )
        value = re.sub(r"https?://\S+", "", value, flags=re.IGNORECASE)
        if re.search(
            r"\b(?:openai|gpt(?:-[\w.]+)?|system prompt|azure blob|migration_seed|"
            r"rate[ -]?card|pricebook|pricing workbook)\b",
            value,
            flags=re.IGNORECASE,
        ):
            # Internal implementation/source names are not seller-facing. An empty
            # value makes the caller use its safe, context-specific fallback.
            return ""
        cleaned: list[str] = []
        for character in unicodedata.normalize("NFC", value):
            category = unicodedata.category(character)
            if category.startswith("C"):
                # Remove model-supplied control/format characters, including bidi
                # overrides, before the text is displayed in WhatsApp.
                continue
            if ord(character) > 127:
                name = unicodedata.name(character, "")
                if category.startswith("L") and "LATIN" not in name:
                    continue
                if category.startswith("M") and "COMBINING" not in name:
                    continue
            cleaned.append(character)
        return " ".join("".join(cleaned).split())

    @staticmethod
    def _seller_safe_scenario_error(error: ScenarioError) -> str:
        """Hide internal scenario identifiers while giving the seller a recovery step."""

        return WhatsAppWebhookService._seller_safe_workflow_error(error)

    @staticmethod
    def _seller_safe_workflow_error(error: Exception) -> str:
        """Translate internal workflow state errors into actionable seller language."""

        message = str(error).strip()
        if "product-selection menu belongs to an earlier requirement" in message.casefold():
            return (
                "That product-selection menu belongs to an earlier requirement and is no "
                "longer active. Nothing was changed. Use the latest product options shown."
            )
        if re.search(
            r"\b(?:(?:scenario|requirement|existing)\s+)?line(?:\(s\))?\b"
            r".*\bnot found\b",
            message,
            re.I,
        ):
            return (
                "I could not identify that licence in the active proposal, so no change was "
                "made. Send the product name you want to change, or tell me the product and "
                "quantity you want to add."
            )
        if "no rate-card sku matched" in message.casefold():
            return (
                "I could not find an approved catalogue match for that product. Send the "
                "complete Microsoft product name or choose from the available SKU options."
            )
        if any(
            phrase in message.casefold()
            for phrase in (
                "active scenario could not be found",
                "initial proposal could not be found",
                "pending sku change has no scenario",
                "revised configuration could not be found",
                "selected proposal is no longer available",
            )
        ):
            return (
                "The active proposal changed or is no longer available. Review the current "
                "proposal, then retry the change."
            )
        if any(
            phrase in message.casefold()
            for phrase in (
                "not awaiting product confirmation",
                "there is no pending sku change",
                "sku confirmation is invalid",
                "product selection is no longer valid",
                "unknown commercial workflow action",
                "interpreted action is not supported",
            )
        ):
            return (
                "That earlier selection is no longer active, so nothing was changed. "
                "Review the latest requirement or proposal and use its current options."
            )
        # Do not expose implementation-oriented exception text. Known, seller-safe
        # validation messages are preserved; everything else receives a neutral recovery
        # instruction instead of leaking state names, identifiers, or backend details.
        if re.search(
            r"\b(?:pending|scenario|workflow|traceback|exception|etag|blob|internal|"
            r"confirmation[_ -]?id|source[_ -]?line[_ -]?id)\b",
            message,
            re.I,
        ):
            return (
                "I could not apply that request to the current proposal, so nothing was "
                "changed. Review the latest proposal and restate the intended licensing "
                "change with the product name and quantity."
            )
        return (
            "I could not apply that request safely. Nothing was changed. Review the latest "
            "requirement or proposal, then restate the licensing action with the product name "
            "and quantity where relevant."
        )

    @staticmethod
    def _trailing_question(value: str) -> str:
        """Return the final direct question so a short seller reply keeps its context."""

        match = re.search(r"([^?]{3,500}\?)\s*$", value.strip())
        return " ".join(match.group(1).split()) if match else ""

    @classmethod
    def _seller_safe_research_text(cls, value: str) -> str:
        """Remove source markup while retaining the supported seller-facing answer."""

        without_links = re.sub(
            r"\[([^\]]+)\]\(https?://[^)]+\)",
            r"\1",
            value,
            flags=re.IGNORECASE,
        )
        without_links = re.sub(
            r"\(https?://[^)]+\)", "", without_links, flags=re.IGNORECASE
        )
        without_links = re.sub(
            r"https?://\S+", "", without_links, flags=re.IGNORECASE
        )
        without_links = re.sub(r"【\d+†[^】]+】", "", without_links)
        return cls._professional_agent_text(without_links)

    @classmethod
    def _reference_product_names(
        cls,
        session: WorkflowSession | None,
        explicit_product: str = "",
        seller_question: str = "",
    ) -> list[str]:
        mentioned_products = cls._product_names_from_question(seller_question)
        products: list[str] = list(mentioned_products)
        if not products and session is not None and session.active_scenario is not None:
            active = session.scenarios.get(session.active_scenario)
            if active is not None:
                products = [
                    line.sku_title
                    for line in active.lines
                    if line.proposed_quantity > 0
                ]
        if not products and session is not None and session.confirmed_as_is is not None:
            products = [
                line.sku_title
                for line in session.confirmed_as_is.lines
                if line.proposed_quantity > 0
            ]
        if not products and session is not None and session.estate is not None:
            products = [line.display_title for line in session.estate.lines]
        # The model may identify a workload named in the question (for example Teams or
        # Excel) as product_query while "these products" refers to the proposal. Preserve
        # both so official research can answer the relationship instead of losing context.
        if explicit_product.strip():
            products.insert(0, explicit_product.strip())
        return list(dict.fromkeys(product for product in products if product.strip()))[:20]

    @staticmethod
    def _product_names_from_question(message: str) -> list[str]:
        """Extract an explicit multi-line product list without treating its question as data."""

        product_markers = re.compile(
            r"\b(?:microsoft|office\s*365|power\s*bi|teams|defender|purview|"
            r"agent\s*365|dynamics\s*365|intune|entra|visio|copilot|windows\s*365)\b",
            flags=re.IGNORECASE,
        )
        question_markers = re.compile(
            r"\b(?:which|what|why|how|can|could|would|should|is|are|list|show|tell)\b",
            flags=re.IGNORECASE,
        )
        products: list[str] = []
        for raw_line in message.splitlines():
            line = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", raw_line).strip()
            if not line or len(line) > 220 or not product_markers.search(line):
                continue
            if "?" in line or (
                question_markers.search(line)
                and re.search(r"\b(?:for|from|among|included|used|assigned)\b", line, re.I)
            ):
                continue
            products.append(line)
        return list(dict.fromkeys(products))[:20]

    async def _send_information_table(
        self,
        sender: str,
        *,
        title: str,
        headers: list[str],
        rows: list[list[str]],
        note: str = "",
    ) -> None:
        safe_headers = [self._professional_agent_text(value)[:80] for value in headers]
        safe_rows = [
            [self._professional_agent_text(value)[:600] for value in row]
            for row in rows
        ]
        try:
            images = render_information_table_images(
                title=self._professional_agent_text(title)[:80],
                headers=safe_headers,
                rows=safe_rows,
                note=self._professional_agent_text(note)[:500],
            )
            for index, content in enumerate(images, start=1):
                await self._whatsapp_client.send_image(
                    to=sender,
                    content=content,
                    filename=f"licensing-guidance-table-{index}.png",
                    content_type="image/png",
                    caption=f"Product guidance • Page {index}/{len(images)}",
                )
            return
        except Exception:
            if "images" in locals():
                # Rendering succeeded and an outbound page failed. Propagate the
                # delivery error so the webhook replay barrier handles it; sending a
                # complete text fallback here would duplicate pages already delivered.
                raise
            logger.exception(
                "Unable to render product guidance table; using mobile text fallback"
            )
        blocks: list[str] = []
        for row in safe_rows:
            lines = [f"*{row[0]}*"]
            lines.extend(
                f"• {header}: {value}"
                for header, value in zip(safe_headers[1:], row[1:], strict=True)
            )
            blocks.append("\n".join(lines))
        await self._send_text_chunks(
            sender,
            "\n\n".join(blocks),
            limit=RESPONSIVE_MESSAGE_LIMIT,
        )

    async def _send_catalog_budget_options(
        self,
        sender: str,
        intent: AgentIntent,
    ) -> None:
        budget = Decimal(str(intent.amount))
        if budget <= 0:
            await self._send_text(
                sender,
                "What annual per-licence budget should I use, and is there a particular "
                "Microsoft product family you want me to consider?",
            )
            return
        offers = await self._orchestrator.affordable_catalog_offers(
            budget=budget,
            price_basis=self._configuration.simple_price_basis,
            product_query=intent.product_query.strip(),
        )
        currency = self._configuration.currency
        if not offers:
            qualifier = (
                f" for {intent.product_query.strip()}" if intent.product_query.strip() else ""
            )
            await self._send_text(
                sender,
                f"I found no unambiguous one-year annual option{qualifier} at or below "
                f"{currency} {budget:,.2f} per licence in the current pricing data. "
                "You can raise the budget or name a different product family.",
            )
            return
        rows = "\n".join(
            f"{index}. {offer.sku_title} — {currency} {offer.unit_price:,.2f} per licence/year"
            for index, offer in enumerate(offers, start=1)
        )
        await self._send_text_chunks(
            sender,
            f"*One-year annual options within {currency} {budget:,.2f} per licence*\n\n"
            f"{rows}\n\nThese are price-qualified options, not a feature-fit recommendation. "
            "Tell me the required capability and quantity if you want me to narrow the list.",
        )

    async def _send_official_product_answer(
        self,
        sender: str,
        intent: AgentIntent,
        *,
        original_message: str | None,
    ) -> None:
        session = await self._orchestrator.get_session(sender)
        question = intent.detail_value.strip() or (original_message or "").strip()
        if not question:
            await self._send_text(
                sender,
                "What would you like to know about the selected Microsoft products?",
            )
            return
        products = self._reference_product_names(
            session,
            intent.product_query,
            question,
        )
        if not products:
            await self._send_text(
                sender,
                "Which Microsoft product or plan should I check?",
            )
            return
        if self._recommendation_advisor is None:
            await self._send_text(
                sender,
                "I cannot verify that product feature from official Microsoft information "
                "in this environment. No proposal change has been made.",
            )
            return
        proposal_context = (
            f"Active proposal: {session.active_scenario.label if session and session.active_scenario else 'none'}; "
            f"products supplied by application: {len(products)}."
        )
        try:
            answer = await self._recommendation_advisor.answer_product_question(
                seller_question=question,
                product_names=products,
                proposal_context=proposal_context,
            )
        except (IntentInterpretationError, AttributeError):
            logger.warning("Official Microsoft product research failed safely")
            await self._send_text(
                sender,
                "I could not verify that product detail from official Microsoft information "
                "just now. No proposal change has been made; please retry the question.",
            )
            return
        seller_answer = self._seller_safe_research_text(answer.answer)
        clarification = self._seller_safe_research_text(answer.clarification_question)
        if seller_answer:
            await self._send_text(sender, seller_answer[:1200])
        if answer.table_headers and answer.table_rows:
            await self._send_information_table(
                sender,
                title=answer.table_title or "Microsoft licensing guidance",
                headers=answer.table_headers,
                rows=answer.table_rows,
            )
        if clarification:
            # A useful answer or structured table must not leave a stale blocking question
            # behind. Persist only a clarification that is required before any answer is
            # possible; otherwise the seller can change subject or confirm the requirement.
            if not seller_answer and not answer.table_rows:
                await self._orchestrator.set_pending_dialogue(
                    sender,
                    PendingDialogue(
                        kind="agent_clarification",
                        question=clarification[:500],
                        context_message=question[:2000],
                        detail_value="official_product_clarification",
                    ),
                )
            await self._send_text(sender, clarification[:500])

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

    @staticmethod
    def _scenario_from_bare_reply(message: str) -> ScenarioType | None:
        """Resolve only a short answer to a scenario question, never a new instruction.

        ``ME5`` can safely fill the slot that is visible to the seller.  A complete turn such
        as ``add Visio to ME5`` or ``build ME5 instead`` is a newer commercial action and must
        be routed on its own rather than inheriting an older pending operation.
        """

        if "?" in message or WhatsAppWebhookService._is_negated_action(
            message,
            r"(?:me3|me5|me7|renew(?:al)?(?:\s+as\s+is)?)",
        ):
            return None
        normalized = " ".join(
            message.casefold().replace("-", " ").strip(" ?!.,").split()
        )
        match = re.fullmatch(
            r"(?:the\s+)?(me3|me5|me7|renew(?:al)?(?:\s+as\s+is)?)"
            r"(?:\s+(?:option|scenario|proposal))?(?:\s+please)?",
            normalized,
        )
        if match is None:
            return None
        token = match.group(1)
        if token == "me3":
            return ScenarioType.ME3_COPILOT
        if token == "me5":
            return ScenarioType.ME5_COPILOT
        if token == "me7":
            return ScenarioType.ME7
        return ScenarioType.RENEW_AS_IS

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
        if session is None:
            return False
        target = self._scenario_from_request(intent, original_message)
        if target is None and intent.action == "set_copilot":
            # A seller may say only "Set Copilot to 30" after preparing one Copilot
            # proposal.  The model-provided scenario is deliberately discarded unless the
            # seller wrote it, so recover the target only when persisted commercial state
            # makes it unambiguous.  A four-way comparison can contain multiple Copilot
            # proposals and therefore still requires an explicit seller choice.
            copilot_targets = [
                scenario_type
                for scenario_type, scenario in session.scenarios.items()
                if any(line.line_id == "COPILOT" for line in scenario.lines)
            ]
            if len(copilot_targets) == 1:
                target = copilot_targets[0]
        if target is not None:
            await self._ensure_operation_allowed(sender, agent_action=intent.action)
            await self._orchestrator.build_scenario(sender, target)
            return False
        if len(session.scenarios) <= 1:
            return False
        available = ", ".join(item.label for item in ScenarioType if item in session.scenarios)
        question = f"Which proposal should I update: {available}?"
        pending = await self._pending_dialogue_for_intent(
            sender,
            intent,
            question=question,
            original_message=original_message,
            scope="scenario",
            awaiting_slot="scenario",
        )
        await self._orchestrator.set_pending_dialogue(sender, pending)
        await self._send_text(sender, question)
        return True

    async def _resolve_line_reference(
        self,
        sender: str,
        supplied_line_id: str,
        *,
        requirement: bool,
        operation: str,
        original_message: str | None,
        pending_intent: AgentIntent | None = None,
    ) -> str | None:
        """Infer a sole line or ask a contextual question instead of leaking an ID error."""

        supplied = supplied_line_id.strip().upper()
        session = await self._orchestrator.get_session(sender)
        lines = []
        if session is not None:
            if requirement and session.estate is not None:
                lines = list(session.estate.lines)
            elif not requirement and session.active_scenario is not None:
                scenario = session.scenarios.get(session.active_scenario)
                if scenario is not None:
                    lines = [line for line in scenario.lines if line.proposed_quantity > 0]
        available_ids = {line.line_id for line in lines}
        # A scenario keeps zero-quantity source rows to preserve the audit trail for
        # migrations and replacements. A saved multi-turn edit can legitimately refer to
        # one of those rows after the seller selects a different target proposal. Accept an
        # exact persisted line ID, while continuing to show only visible (>0) lines when a
        # new target must be inferred from natural language.
        all_scenario_ids: set[str] = set()
        if (
            not requirement
            and session is not None
            and session.active_scenario is not None
        ):
            full_scenario = session.scenarios.get(session.active_scenario)
            if full_scenario is not None:
                all_scenario_ids = {line.line_id for line in full_scenario.lines}
        if supplied and supplied.casefold() != "none" and supplied in available_ids:
            return supplied
        if supplied and supplied.casefold() != "none" and supplied in all_scenario_ids:
            return supplied
        if len(lines) == 1:
            return lines[0].line_id

        inferred = self._line_reference_from_message(
            lines,
            original_message or "",
            requirement=requirement,
        )
        if inferred is not None:
            return inferred

        if lines:
            choices = "; ".join(
                line.display_title if requirement else line.sku_title
                for line in lines[:10]
            )
            if len(lines) > 10:
                choices += f", and {len(lines) - 10} more"
            question = (
                f"Which licence should I {operation}? Reply with the product name: {choices}"
            )
        else:
            question = (
                "Which licence should I update? Send the product name or first add a "
                "licence to the requirement."
            )
        if pending_intent is not None:
            pending = await self._pending_dialogue_for_intent(
                sender,
                pending_intent,
                question=question,
                original_message=original_message,
                scope="requirement" if requirement else "scenario",
            )
        else:
            pending = PendingDialogue(
                kind="agent_clarification",
                question=question,
                context_message=(original_message or "")[:2000],
                scope="requirement" if requirement else "scenario",
            )
        await self._orchestrator.set_pending_dialogue(sender, pending)
        await self._send_text(sender, question)
        return None

    @staticmethod
    def _line_reference_from_message(
        lines: list,
        message: str,
        *,
        requirement: bool,
    ) -> str | None:
        """Resolve a unique product reference without exposing internal line identifiers."""

        normalized_message = normalize_product_title(message)
        if not normalized_message:
            return None
        mentioned_ids = {
            match.upper() for match in re.findall(r"\bL\d+\b", message, flags=re.IGNORECASE)
        }
        matching_ids = [line.line_id for line in lines if line.line_id in mentioned_ids]
        if len(matching_ids) == 1:
            return matching_ids[0]

        generic = {
            "add",
            "change",
            "delete",
            "for",
            "licence",
            "licences",
            "license",
            "licenses",
            "microsoft",
            "quantity",
            "remove",
            "replace",
            "set",
            "the",
            "to",
            "user",
            "users",
        }
        message_tokens = set(normalized_message.split()) - generic
        scored: list[tuple[int, str]] = []
        for line in lines:
            title = line.display_title if requirement else line.sku_title
            normalized_title = normalize_product_title(title)
            if not normalized_title:
                continue
            if normalized_title in normalized_message:
                scored.append((1000 + len(normalized_title), line.line_id))
                continue
            title_tokens = set(normalized_title.split()) - generic
            overlap = title_tokens & message_tokens
            product_codes = {
                token
                for token in title_tokens
                if re.fullmatch(r"(?:m?e|p|f)\d+", token)
            }
            code_overlap = product_codes & message_tokens
            if code_overlap:
                scored.append((500 + 20 * len(code_overlap) + len(overlap), line.line_id))
            elif len(overlap) >= 2:
                scored.append((100 + 10 * len(overlap), line.line_id))
            elif len(overlap) == 1:
                # A seller will often answer a product-target question with the one
                # distinguishing word from the displayed title (for example
                # ``Residency``). Accept that shorthand only when the token is
                # meaningful and it resolves to exactly one line below. Equal scores
                # deliberately remain ambiguous rather than selecting the wrong SKU.
                token = next(iter(overlap))
                if len(token) >= 4:
                    scored.append((20 + len(token), line.line_id))
        if not scored:
            return None
        scored.sort(reverse=True)
        best_score = scored[0][0]
        winners = {line_id for score, line_id in scored if score == best_score}
        return next(iter(winners)) if len(winners) == 1 else None

    async def _pending_dialogue_for_intent(
        self,
        sender: str,
        intent: AgentIntent,
        *,
        question: str,
        original_message: str | None,
        operation: str | None = None,
        scope: Literal["none", "requirement", "scenario"] | None = None,
        awaiting_slot: str | None = None,
    ) -> PendingDialogue:
        """Capture every known operation slot before asking a follow-up question."""

        session = await self._orchestrator.get_session(sender)
        inferred_scope: Literal["none", "requirement", "scenario"] = "none"
        if session is not None:
            if session.confirmed_as_is is None:
                inferred_scope = "requirement"
            elif session.active_scenario is not None:
                inferred_scope = "scenario"

        def integer(name: str) -> int:
            raw_value = getattr(intent, name, -1)
            if raw_value is None or raw_value == "":
                return -1
            try:
                return int(raw_value)
            except (TypeError, ValueError):
                return -1

        disposition = str(getattr(intent, "disposition", "none") or "none")
        if disposition not in {"retain", "remove", "migrate", "included", "none"}:
            disposition = "none"
        action = operation or str(getattr(intent, "action", "none") or "none")
        allowed_operations = {
            "none",
            "choose_change",
            "add_sku",
            "replace_sku",
            "set_quantity",
            "set_copilot",
            "set_disposition",
            "build_scenario",
            "set_term",
            "set_billing",
            "set_segment",
            "set_currency",
            "add_comment",
            "request_recommendation",
            "compare_enterprise_options",
        }
        if action not in allowed_operations:
            action = "none"
        detail_value = ""
        detail_fields = {
            "set_term": "term_duration",
            "set_billing": "billing_plan",
            "set_segment": "segment",
            "set_currency": "currency",
            "add_comment": "comment",
        }
        if action in detail_fields:
            detail_value = str(getattr(intent, detail_fields[action], "") or "").strip()

        source_line_id = str(getattr(intent, "line_id", "") or "").strip().upper()
        if source_line_id.casefold() == "none":
            source_line_id = ""
        product_query = str(getattr(intent, "product_query", "") or "").strip()
        dialogue_scope = scope or inferred_scope
        if (
            source_line_id
            and session is not None
            and action in {"choose_change", "replace_sku", "set_quantity", "set_disposition"}
        ):
            editable_lines, _ = self._pending_lines(session, dialogue_scope)
            if source_line_id not in {line.line_id for line in editable_lines}:
                source_line_id = ""
        resolved_slot = awaiting_slot or "none"
        if resolved_slot == "none":
            if action == "choose_change":
                resolved_slot = "change_dimension"
            elif action in {"replace_sku", "set_quantity", "set_disposition"} and not source_line_id:
                resolved_slot = "line"
            elif action in {"add_sku", "replace_sku"} and not product_query:
                resolved_slot = "product"
            elif action == "add_sku" and integer("quantity") <= 0:
                resolved_slot = "quantity"
            elif action == "set_quantity" and integer("quantity") < 0:
                resolved_slot = "quantity"
            elif action == "set_copilot" and integer("copilot_quantity") < 0:
                resolved_slot = "quantity"
            elif action == "set_disposition" and disposition == "none":
                resolved_slot = "disposition"
            elif action == "build_scenario" and self._scenario_from_request(
                intent, original_message
            ) is None:
                resolved_slot = "scenario"
            elif action == "set_term" and not detail_value:
                resolved_slot = "term"
            elif action == "set_billing" and not detail_value:
                resolved_slot = "billing"
            elif action == "set_segment" and not detail_value:
                resolved_slot = "segment"
            elif action == "set_currency" and not detail_value:
                resolved_slot = "currency"
            elif action == "add_comment" and not detail_value:
                resolved_slot = "comment"
            elif action == "request_recommendation":
                resolved_slot = "recommendation_context"
        return PendingDialogue(
            kind="agent_clarification",
            question=question[:500],
            context_message=(original_message or "")[:2000],
            operation=action,  # type: ignore[arg-type]
            awaiting_slot=resolved_slot,  # type: ignore[arg-type]
            scope=dialogue_scope,
            scenario_type=self._scenario_from_request(intent, original_message),
            source_line_id=source_line_id,
            product_query=product_query,
            quantity=integer("quantity"),
            copilot_quantity=integer("copilot_quantity"),
            disposition=disposition,  # type: ignore[arg-type]
            detail_value=detail_value,
        )

    async def _pause_for_missing_intent_detail(
        self,
        sender: str,
        intent: AgentIntent,
        original_message: str | None,
    ) -> bool:
        """Persist incomplete edits so a short follow-up completes the intended action."""

        action = intent.action
        question = ""
        if action == "add_sku":
            product = str(getattr(intent, "product_query", "") or "").strip()
            quantity = int(getattr(intent, "quantity", -1) or -1)
            if not product:
                question = (
                    f"Which exact Microsoft product should I add for {quantity} licences?"
                    if quantity > 0
                    else "Which exact Microsoft product should I add?"
                )
            elif quantity <= 0:
                question = f"How many {product} licences should I add?"
        elif action == "replace_sku":
            product = str(getattr(intent, "product_query", "") or "").strip()
            source_line = str(getattr(intent, "line_id", "") or "").strip()
            if not source_line:
                question = "Which licence in the current proposal should I replace?"
            elif not product:
                question = "Which Microsoft product should replace the selected licence?"
        elif action == "set_quantity" and int(getattr(intent, "quantity", -1)) < 0:
            question = "What should the new licence quantity be?"
        elif action == "set_copilot" and int(getattr(intent, "copilot_quantity", -1)) < 0:
            question = "How many Copilot licences should the proposal include?"
        elif action == "build_scenario" and str(
            getattr(intent, "scenario", "none")
        ) == "none":
            question = "Which option should I prepare: Renew As-Is, ME3, ME5, or ME7?"
        elif action == "set_disposition" and str(
            getattr(intent, "disposition", "none")
        ) == "none":
            question = "Should the selected licence be retained, removed, or replaced?"
        elif action == "set_term" and not str(
            getattr(intent, "term_duration", "") or ""
        ).strip():
            question = "What annual contract term should I apply?"
        elif action == "set_billing" and not str(
            getattr(intent, "billing_plan", "") or ""
        ).strip():
            question = "Which annual billing plan should I apply?"
        elif action == "set_segment" and not str(
            getattr(intent, "segment", "") or ""
        ).strip():
            question = "Which customer segment applies to this proposal?"
        elif action == "set_currency" and not str(
            getattr(intent, "currency", "") or ""
        ).strip():
            question = "Which currency are you asking about?"
        elif action == "add_comment" and not str(
            getattr(intent, "comment", "") or ""
        ).strip():
            question = "What seller comment should I add to the proposal?"
        if not question:
            return False

        pending = await self._pending_dialogue_for_intent(
            sender,
            intent,
            question=question,
            original_message=original_message,
        )
        await self._orchestrator.set_pending_dialogue(sender, pending)
        await self._send_text(sender, question)
        return True

    @staticmethod
    def _looks_like_requirement_fragment(message: str) -> bool:
        """Conservative fallback when the intent model asks instead of starting capture."""

        value = " ".join(message.casefold().strip(" ?!.,").split())
        if WhatsAppWebhookService._is_negated_requirement_statement(value):
            return False
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
            "start with",
            "begin with",
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
    def _is_high_confidence_requirement_statement(cls, message: str) -> bool:
        """Recognize an explicit SKU request without overriding genuine questions.

        The language model remains the primary router. This narrow structural fallback is
        used only when it returns ``clarify`` (or is temporarily unavailable), so an obvious
        statement such as "I also need Copilot" cannot be stranded in a generic question.
        """

        if cls._is_negated_requirement_statement(message):
            return False
        if not (
            cls._looks_like_requirement_fragment(message)
            and not cls._is_licensing_question_message(message)
            and not cls._is_clear_non_requirement_turn(message)
            and not cls._looks_like_existing_requirement_operation(message)
        ):
            return False
        value = " ".join(
            message.casefold().replace("\u2019", "'").strip(" ?!.,").split()
        )
        explicit_request = bool(
            re.search(
                r"(?:^|[;,.]\s*)(?:please\s+)?(?:also\s+)?(?:(?:i|we)\s+)?"
                r"(?:want|need|require|add|include|order|purchase|buy|renew|use|"
                r"give\s+me|go\s+with|consider)\b",
                value,
            )
            or re.match(
                r"^(?:let'?s|let\s+us)\s+(?:start|begin)"
                r"(?:\s+(?:with|using))?\b",
                value,
            )
            or re.match(
                r"^(?:let'?s|let\s+us)\s+(?:add|include|use|order)\b",
                value,
            )
            or re.match(
                r"^(?:can|could|would|will)\s+you\s+(?:please\s+)?"
                r"(?:add|include|order|use|capture|record)\b",
                value,
            )
        )
        explicit_quantity = bool(
            re.search(
                r"\b\d+\s*(?:licen[cs]es?|users?|seats?|subscriptions?|qty|quantity)\b",
                value,
            )
        )
        compact_product_quantity = bool(
            re.fullmatch(
                r"[a-z0-9+()./& -]{1,120}\s+\d+"
                r"(?:\s+(?:licen[cs]es?|users?|seats?|subscriptions?|qty|quantity))?",
                value,
            )
            and not re.search(
                r"\b(?:has|have|had|costs?|priced?|includes?|contains?|features?|"
                r"was|were|last\s+year|expensive|cheap)\b",
                value,
            )
        )
        return explicit_request or explicit_quantity or compact_product_quantity or bool(
            re.fullmatch(r"(?:m?e[1357]|o365|m365)", value)
        )

    async def _is_assertive_requirement_capture(
        self,
        message: str,
        session: WorkflowSession | None,
    ) -> bool:
        """Ground every text-capture mutation in the seller's actual wording."""

        normalized = " ".join(
            message.casefold().replace("\u2019", "'").strip(" ?!.,").split()
        )
        direct_polite = bool(
            re.match(
                r"^(?:can|could|would|will)\s+you\s+(?:please\s+)?"
                r"(?:add|include|capture|record|use|order)\b",
                normalized,
            )
        )
        direct_possession_request = bool(
            re.match(
                r"^(?:can|could|may)\s+(?:i|we)\s+"
                r"(?:have|get|use|order|take)\b",
                normalized,
            )
        )
        if not normalized or (
            "?" in message and not (direct_polite or direct_possession_request)
        ):
            return False
        if self._is_licensing_question_message(message) and not (
            direct_polite or direct_possession_request
        ):
            return False
        if self._is_negated_requirement_statement(message):
            return False
        if self._is_quantity_only_reply(message):
            # Approximate quantity answers ("maybe around ten") are valid only when a
            # persisted capture turn is visibly waiting for that quantity. They must not
            # start a new requirement by themselves.
            return bool(session is not None and session.capture_messages)
        if re.search(
            r"\b(?:if|unless|whether|suppose|supposing|assuming|imagine|maybe|"
            r"perhaps|hypothetically|for\s+example|as\s+an\s+example)\b",
            normalized,
        ):
            return False
        if re.search(
            r"\b(?:(?:customer|client|seller|user|manager|analyst|report|invoice)\b"
            r".{0,36}\b(?:said|says|asked|asks|mentioned|mentions|lists?|stated|"
            r"requested|requests?|wants?|selected|selects|chose|chooses|picked|picks))\b",
            normalized,
        ) or re.search(
                r"\b(?:said|asked|mentioned|stated|requested|selected|chose|picked)\s+"
                r"(?:the\s+)?"
            r"(?:customer|client|seller|user|manager|analyst)\b",
            normalized,
        ):
            return False
        if re.search(
            r"\b(?:want|need|would\s+like)\s+to\s+know\b|"
            r"\b(?:want|need|would\s+like)\s+(?:a\s+)?"
            r"(?:pricing|price|quote|cost)\b|"
            r"\b(?:has|have|had|costs?|priced?|includes|contains?|features?|"
            r"is\s+expensive|is\s+cheap)\b",
            normalized,
        ):
            return False
        direct_requirement = bool(
            re.search(
                r"(?:^|[;,.]\s*)(?:please\s+)?(?:also\s+)?(?:(?:i|we)\s+)?"
                r"(?:want|need|require|add|include|order|purchase|buy|renew|use|"
                r"give\s+me|go\s+with|consider)\b",
                normalized,
            )
            or re.match(
                r"^(?:let'?s|let\s+us)\s+(?:start|begin)"
                r"(?:\s+(?:with|using))?\b",
                normalized,
            )
            or re.match(
                r"^(?:let'?s|let\s+us)\s+(?:add|include|use|order)\b",
                normalized,
            )
        )
        if direct_polite or direct_possession_request or direct_requirement:
            return True
        # A bare catalogue title/code is a valid first turn ("Power BI Pro", "ME7").
        # Requiring real catalogue evidence prevents arbitrary one-word conversation from
        # entering the commercial draft.
        query = self._catalog_query_from_requirement(message)
        return bool(query and await self._orchestrator.catalog_candidates(query))

    @staticmethod
    def _catalog_query_from_requirement(message: str) -> str:
        """Reduce a seller sentence to product-bearing terms without naming products.

        This is used only as a safety check for the deterministic fallback path. The
        language model remains the primary interpreter; when it is unavailable or returns
        an unsuitable top-level action, the fallback must still prove that the remaining
        terms match the maintained catalogue before it starts requirement capture.
        """

        value = normalize_product_title(message)
        value = re.sub(
            r"\b(?:for\s+)?(?:one|1|twelve|12)\s*[- ]?"
            r"(?:years?|months?)\b|\b(?:p1y|annual(?:ly)?|yearly)\b",
            " ",
            value,
        )
        value = re.sub(
            r"\b\d+\s*(?:licen[cs]es?|users?|seats?|quantity|qty)\b",
            " ",
            value,
        )
        value = re.sub(
            r"\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|"
            r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|"
            r"eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|"
            r"eighty|ninety|hundred|thousand|and)(?:[ -]+(?:zero|one|two|"
            r"three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|"
            r"fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|"
            r"thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|"
            r"and))*\s*(?:licen[cs]es?|users?|seats?|quantity|qty)\b",
            " ",
            value,
        )
        value = re.sub(r"\b\d+\b", " ", value)
        ignored = {
            "a",
            "add",
            "also",
            "an",
            "around",
            "consider",
            "for",
            "give",
            "i",
            "include",
            "licence",
            "licences",
            "license",
            "licenses",
            "let",
            "lets",
            "me",
            "need",
            "now",
            "order",
            "please",
            "quantity",
            "qty",
            "require",
            "subscription",
            "start",
            "starting",
            "begin",
            "confusing",
            "take",
            "that",
            "the",
            "this",
            "to",
            "s",
            "us",
            "use",
            "want",
            "we",
            "with",
            "within",
        }
        return " ".join(token for token in value.split() if token not in ignored)

    async def _is_catalog_backed_requirement_statement(self, message: str) -> bool:
        """Require real catalogue evidence before overriding a model routing result."""

        if not await self._is_assertive_requirement_capture(message, None):
            return False
        query = self._catalog_query_from_requirement(message)
        if not query:
            return False
        return bool(await self._orchestrator.catalog_candidates(query))

    @staticmethod
    def _is_negated_requirement_statement(message: str) -> bool:
        """Keep removals and negative statements out of heuristic requirement capture."""

        value = " ".join(message.casefold().strip(" ?!.,").split())
        return bool(
            re.search(
                r"\bno\s+need(?:\s+(?:to|for))?\b|"
                r"\b(?:do\s+not|don't|dont)\s+(?:need|want|require)\b|"
                r"\b(?:not\s+(?:needed|required|wanted)|no\s+longer\s+(?:needed|required))\b",
                value,
            )
            or re.search(
                r"\b(?:copilot|defender|power\s+bi|microsoft|office|me[357]|"
                r"licen[cs]e|sku)\b.{0,60}\b(?:is|are|was|were)\s+not\s+"
                r"(?:needed|required|wanted)\b",
                value,
            )
            or
            re.search(
                r"\b(?:do not|don't|dont|no longer|not required|not needed|without)\b"
                r".{0,80}\b(?:need|want|require|add|include|renew|use|licen[cs]e|sku)\b",
                value,
            )
            or re.search(
                r"\b(?:remove|delete|drop|exclude)\b.{0,80}"
                r"\b(?:licen[cs]e|sku|copilot|defender|power bi|microsoft|office|me[357])\b",
                value,
            )
        )

    @staticmethod
    def _contains_multiple_mutation_clauses(message: str) -> bool:
        """Detect independent edits that the one-action contract cannot apply atomically."""

        clauses = [
            clause.strip()
            for clause in re.split(r"\s+(?:and then|then|and)\s+|[;\n]+", message.casefold())
            if clause.strip()
        ]
        mutation = re.compile(
            r"\b(?:add|remove|delete|drop|replace|change|update|set|retain|migrate|include)\b"
        )
        mutation_clauses = [clause for clause in clauses if mutation.search(clause)]

        def is_quantity_modifier(clause: str) -> bool:
            tokens = re.findall(r"[a-z0-9]+", clause)
            allowed = {
                "change",
                "it",
                "licence",
                "licences",
                "license",
                "licenses",
                "make",
                "quantity",
                "qty",
                "seat",
                "seats",
                "set",
                "the",
                "to",
                "update",
                "user",
                "users",
            }
            return (
                any(token.isdigit() for token in tokens)
                and all(token.isdigit() or token in allowed for token in tokens)
            )

        if len(mutation_clauses) >= 2:
            # "Replace X with Y and set the quantity to N" is one representable
            # replacement. Other multiple-verb requests cannot be committed atomically by
            # the one-action intent contract.
            if (
                len(mutation_clauses) == 2
                and re.search(
                    r"\b(?:add|replace|change|update)\b",
                    mutation_clauses[0],
                )
                and is_quantity_modifier(mutation_clauses[1])
                and not re.search(
                    r"\b(?:add|remove|delete|drop|replace)\b",
                    mutation_clauses[1],
                )
            ):
                return False
            return True

        if len(clauses) < 2 or not mutation.search(clauses[0]):
            return False

        # A shared verb can govern several independent objects: "Add E3 and Copilot",
        # "Remove L1 and L2", or "Change L1 and L2 to 50". The structured intent schema
        # can represent only one target, so detect the omitted repeated verb rather than
        # partially applying the first object.
        if len(set(re.findall(r"\bl\d+\b", message.casefold()))) >= 2:
            return True

        modifier_tokens = {
            "a",
            "all",
            "an",
            "annual",
            "annually",
            "billing",
            "comment",
            "contract",
            "currency",
            "discount",
            "for",
            "keep",
            "licence",
            "licences",
            "license",
            "licenses",
            "it",
            "make",
            "monthly",
            "of",
            "plan",
            "quantity",
            "qty",
            "same",
            "seat",
            "seats",
            "set",
            "term",
            "the",
            "to",
            "user",
            "users",
            "with",
            "year",
            "yearly",
        }
        for clause in clauses[1:]:
            tokens = re.findall(r"[a-z0-9]+", clause)
            meaningful = [
                token
                for token in tokens
                if token not in modifier_tokens and not token.isdigit()
            ]
            if meaningful:
                return True
        return False

    @staticmethod
    def _is_licensing_question_message(message: str) -> bool:
        """Keep product-rich questions out of requirement capture."""

        lines = [" ".join(line.strip().split()) for line in message.splitlines() if line.strip()]
        if not lines:
            return False
        tail = lines[-1].casefold().strip(" ?!.,")
        operation = re.search(
            r"\b(?:add|include|remove|delete|replace|change|update|set|order|renew)\b",
            tail,
        )
        if operation:
            return False
        if re.search(
            r"\b(?:(?:want|need|would like) to (?:know|understand|learn)|"
            r"information about|features?|benefits?|capabilities?|"
            r"help (?:me )?(?:choose|pick|select)|suggest|recommend)\b",
            tail,
        ):
            return True
        return bool(
            "?" in lines[-1]
            or re.match(
                r"^(?:who|what|which|where|when|why|how|is|are|can|could|would|"
                r"should|do|does|list|show|tell)\b",
                tail,
            )
            or re.search(
                r"\b(?:which among|which of|for these products|from the above|"
                r"student friendly|business purposes|included for|can be used)\b",
                tail,
            )
        )

    @classmethod
    def _is_quantity_only_reply(cls, message: str) -> bool:
        """Identify a quantity that completes the product currently being captured."""

        value = " ".join(
            message.casefold().replace("\u2019", "'").strip(" ?!.,").split()
        )
        numeric_form = bool(
            re.fullmatch(
                r"(?:(?:let'?s|let us|i|we|please)\s+)?"
                r"(?:(?:want|need|consider|use|take|make it|set it to|go with)\s+)?"
                r"(?:(?:about|around|approximately|approx|maybe(?:\s+around)?)\s+)?"
                r"\d+\s*(?:(?:licence|license)s?|users?|seats?|quantity|qty)?",
                value,
            )
        )
        if numeric_form:
            return True
        values = cls._seller_quantity_values(value)
        return bool(
            len(values) == 1
            and re.fullmatch(
                r"(?:(?:let'?s|let us|i|we|please)\s+)?"
                r"(?:(?:want|need|consider|use|take|make it|set it to|go with)\s+)?"
                r"(?:(?:about|around|approximately|approx|maybe(?:\s+around)?)\s+)?"
                r"(?:[a-z]+(?:[ -]+(?:and\s+)?[a-z]+)*)"
                r"\s*(?:(?:licence|license)s?|users?|seats?|quantity|qty)?",
                value,
            )
        )

    @staticmethod
    def _number_words_to_int(value: str) -> int | None:
        """Parse a bounded English whole-number phrase used for licence quantities."""

        small = {
            "zero": 0,
            "one": 1,
            "two": 2,
            "three": 3,
            "four": 4,
            "five": 5,
            "six": 6,
            "seven": 7,
            "eight": 8,
            "nine": 9,
            "ten": 10,
            "eleven": 11,
            "twelve": 12,
            "thirteen": 13,
            "fourteen": 14,
            "fifteen": 15,
            "sixteen": 16,
            "seventeen": 17,
            "eighteen": 18,
            "nineteen": 19,
            "twenty": 20,
            "thirty": 30,
            "forty": 40,
            "fifty": 50,
            "sixty": 60,
            "seventy": 70,
            "eighty": 80,
            "ninety": 90,
        }
        tokens = value.casefold().replace("-", " ").split()
        if not tokens or any(
            token not in small and token not in {"and", "hundred", "thousand"}
            for token in tokens
        ):
            return None
        total = 0
        current = 0
        saw_number = False
        for token in tokens:
            if token == "and":
                continue
            saw_number = True
            if token in small:
                current += small[token]
            elif token == "hundred":
                current = max(current, 1) * 100
            elif token == "thousand":
                total += max(current, 1) * 1000
                current = 0
        result = total + current
        return result if saw_number and 0 <= result <= 1_000_000 else None

    @staticmethod
    def _number_word_pattern() -> str:
        word = (
            r"(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|"
            r"twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|"
            r"nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|"
            r"hundred|thousand|and)"
        )
        return rf"{word}(?:[ -]+{word})*"

    @classmethod
    def _number_word_matches(cls, message: str, patterns: tuple[str, ...]) -> set[int]:
        """Parse seller-authored number words only at field-specific language anchors."""

        normalized = " ".join(
            message.casefold().replace("\u2019", "'").strip(" ?!.,").split()
        )
        number_words = cls._number_word_pattern()
        values: set[int] = set()
        for template in patterns:
            pattern = template.format(number=number_words)
            for match in re.finditer(pattern, normalized):
                phrase = match.group("number")
                parsed = cls._number_words_to_int(phrase)
                if parsed is not None:
                    values.add(parsed)
        return values

    @classmethod
    def _seller_percentage_values(cls, message: str) -> set[Decimal]:
        """Return percentages explicitly stated by the seller, including number words."""

        normalized = " ".join(
            message.casefold().replace("\u2019", "'").replace(",", "").split()
        )
        values = {
            Decimal(value)
            for value in re.findall(
                r"(?<![a-z0-9])(-?\d+(?:\.\d+)?)\s*(?:%|percent(?:age)?\b)",
                normalized,
            )
        }
        values.update(
            Decimal(value)
            for value in cls._number_word_matches(
                normalized,
                (r"\b(?P<number>{number})\s+percent(?:age)?\b",),
            )
        )
        return values

    @classmethod
    def _seller_adjustment_values(cls, message: str) -> set[Decimal]:
        """Return explicitly stated adjustment amounts with seller-authored direction."""

        normalized = " ".join(
            message.casefold().replace("\u2019", "'").replace(",", "").split()
        )
        unsigned_numeric = r"(?P<number>\d+(?:\.\d+)?)"
        signed_numeric = r"(?P<number>-?\d+(?:\.\d+)?)"
        positive_patterns = (
            rf"\b(?:add|increase)(?:\s+(?:an?|the|commercial))?\s+"
            rf"(?:adjustment\s+(?:of|by)\s+)?{unsigned_numeric}\b",
            rf"\b(?:adjustment|amount)(?:\s+(?:of|to|by))?\s+{signed_numeric}\b",
        )
        negative_patterns = (
            rf"\b(?:subtract|deduct|decrease)(?:\s+(?:an?|the|commercial))?\s+"
            rf"(?:adjustment\s+(?:of|by)\s+)?{unsigned_numeric}\b",
        )
        values: set[Decimal] = set()
        for pattern in positive_patterns:
            values.update(Decimal(match.group("number")) for match in re.finditer(pattern, normalized))
        for pattern in negative_patterns:
            values.update(-abs(Decimal(match.group("number"))) for match in re.finditer(pattern, normalized))

        word_patterns = (
            (
                r"\b(?:add|increase)(?:\s+(?:an?|the|commercial))?\s+"
                r"(?:adjustment\s+(?:of|by)\s+)?(?P<number>{number})\b",
                1,
            ),
            (
                r"\b(?:subtract|deduct|decrease)(?:\s+(?:an?|the|commercial))?\s+"
                r"(?:adjustment\s+(?:of|by)\s+)?(?P<number>{number})\b",
                -1,
            ),
            (
                r"\b(?:adjustment|amount)(?:\s+(?:of|to|by))?\s+"
                r"(?P<number>{number})\b",
                1,
            ),
        )
        for pattern, sign in word_patterns:
            for value in cls._number_word_matches(normalized, (pattern,)):
                values.add(Decimal(sign * value))
        return values

    @classmethod
    def _seller_quantity_values(cls, message: str) -> set[int]:
        """Return seller-authored quantities without treating SKU digits as quantities."""

        normalized = " ".join(
            message.casefold().replace("\u2019", "'").strip(" ?!.,").split()
        )
        scrubbed = re.sub(
            r"\b(?:product|sku)\s+id\s*[:=#-]?\s*[a-z0-9-]+\b",
            " ",
            normalized,
        )
        scrubbed = re.sub(r"\b(?:microsoft|office)\s*365\b", " ", scrubbed)
        # ``Plan 1`` and ``Plan 2`` identify a product edition. They are never licence
        # quantities unless a separate quantity is explicitly supplied.
        scrubbed = re.sub(r"\bplan\s+\d+\b", " ", scrubbed)
        scrubbed = re.sub(r"\b(?:l\d+|m?e\d+|p\d+|f\d+)\b", " ", scrubbed)
        scrubbed = re.sub(r"\bp\d+[ymd]\b", " ", scrubbed)
        scrubbed = re.sub(
            r"\b\d+\s*(?:years?|months?|days?)\b",
            " ",
            scrubbed,
        )
        scrubbed = re.sub(
            r"\b(?=[a-z0-9-]*[a-z])(?=[a-z0-9-]*\d)[a-z0-9-]+\b",
            " ",
            scrubbed,
        )
        values = {int(value) for value in re.findall(r"\b\d+\b", scrubbed)}

        number_word = cls._number_word_pattern()
        phrases: list[str] = []
        phrases.extend(
            match.group("number")
            for match in re.finditer(
                rf"\b(?P<number>{number_word}(?:[ -]+{number_word})*)\s+"
                r"(?:(?:licence|license)s?|users?|seats?|subscriptions?|qty|quantity)\b",
                normalized,
            )
        )
        trailing = re.search(
            rf"\b(?:to|quantity(?:\s+of)?|qty(?:\s+of)?)\s+"
            rf"(?P<number>{number_word}(?:[ -]+{number_word})*)$",
            normalized,
        )
        if trailing is not None:
            phrases.append(trailing.group("number"))
        if re.fullmatch(rf"{number_word}(?:[ -]+{number_word})*", normalized):
            phrases.append(normalized)
        phrases.extend(
            match.group("number")
            for match in re.finditer(
                rf"\b(?:add|include|need|want|require|order|buy|purchase|take|"
                rf"consider|with|to)\s+(?P<number>{number_word})\b"
                rf"(?![- ]+(?:year|month|day)s?\b)",
                normalized,
            )
        )
        for phrase in phrases:
            parsed = cls._number_words_to_int(phrase)
            if parsed is not None:
                values.add(parsed)
        return values

    @staticmethod
    def _looks_like_existing_requirement_operation(message: str) -> bool:
        """Keep corrections and replacements out of the new-line capture path."""

        value = " ".join(message.casefold().strip(" ?!.,").split())
        if re.search(
            r"\b(?:change|replace|remove|delete|update|edit|set|upgrade|downgrade|"
            r"switch|move)\b",
            value,
        ):
            return True
        return bool(
            re.search(r"\bl\d+\b", value)
            and not re.search(r"\b(?:add|include|another|new)\b", value)
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

    async def _send_non_requirement_boundary(self, sender: str) -> None:
        session = await self._orchestrator.get_session(sender)
        if session is not None and session.capture_messages:
            next_step = (
                "The unfinished licence details remain saved; continue with the missing "
                "product or quantity whenever you are ready."
            )
        elif session is not None and session.estate is not None:
            next_step = (
                "The current licensing draft remains unchanged; continue its review whenever "
                "you are ready."
            )
        else:
            next_step = (
                "When you are ready, share a Microsoft licensing requirement or business need."
            )
        await self._send_text(
            sender,
            "That request is outside this licensing advisor's scope, so I have "
            "not added it to the licensing requirement. I can help with "
            "Microsoft licensing requirements, SKU clarification, annual pricing, proposal changes, "
            f"and comparisons. {next_step}",
        )

    async def _message_has_licensing_context(self, message: str) -> bool:
        """Conservatively recognize a licensing/product question from seller evidence."""

        normalized = normalize_product_title(message)
        if re.search(
            r"\b(?:microsoft|office|licen[cs]e|licensing|sku|subscription|tenant|"
            r"renewal|renew|pricing|price|quote|proposal|copilot|defender|intune|"
            r"entra|power\s+bi|visio|teams|exchange|dynamics|windows|m?e[1357]|"
            r"o365|m365|requirements?|commercial\s+value|budget|inr|usd|eur|gbp)\b",
            normalized,
        ):
            return True
        if re.search(
            r"\b(?:upload|attach|send|share|provide)\b.{0,40}"
            r"\b(?:pdf|word|docx?|excel|csv|xlsx?|image|screenshot|voice\s+note|"
            r"audio|document|file)\b|"
            r"\b(?:pdf|word|docx?|excel|csv|xlsx?|image|screenshot|voice\s+note|"
            r"audio|document|file)\b.{0,40}\b(?:upload|attach|send|share|provide)\b",
            normalized,
        ):
            return True
        matches = await self._orchestrator.catalog_candidates(message.strip())
        return bool(matches and matches[0].confidence >= 90)

    def _deterministic_session_fact_answer(
        self,
        session: WorkflowSession | None,
        message: str,
    ) -> str | None:
        """Answer current-proposal fact questions only from committed session data.

        The language model decides conversational intent, but it is not an authority for
        seller quantities, selected SKUs, terms, or commercial totals.  Questions about the
        saved requirement therefore cross this deterministic boundary before any model text
        can be sent to WhatsApp.
        """

        if session is None:
            return None
        normalized = normalize_product_title(message)
        explicit_scenario = self._seller_scenario_reference(message)
        captured_reference = bool(
            re.search(
                r"\b(?:captured|uploaded|submitted|original|draft)\s+"
                r"(?:requirement|licen[cs]es?|file|estate)\b|"
                r"\b(?:requirement|estate)\s+(?:i\s+)?(?:uploaded|submitted)\b",
                normalized,
            )
        )
        baseline_reference = bool(
            explicit_scenario == ScenarioType.RENEW_AS_IS
            or re.search(r"\b(?:confirmed\s+baseline|baseline)\b", normalized)
        )
        active_reference = bool(
            re.search(
                r"\b(?:current|active|revised|selected|latest)\s+"
                r"(?:proposal|configuration|option|scenario)\b",
                normalized,
            )
        )
        state_reference = bool(
            captured_reference
            or baseline_reference
            or active_reference
            or explicit_scenario is not None
            or re.search(
                r"\b(?:my|this|confirmed|saved|proposal|requirement|draft|"
                r"renewal|renew\s+as\s+is|as\s+is|l\d+)\b",
                normalized,
            )
        )
        unit_price_question = bool(
            re.search(
                r"\b(?:unit\s+price|price\s+per\s+(?:licen[cs]e|user|seat)|"
                r"per[ -](?:licen[cs]e|user|seat)\s+price)\b",
                normalized,
            )
        )
        line_total_question = bool(
            re.search(
                r"\b(?:line\s+total|subtotal\s+for|total\s+for\s+(?:l\d+|the\s+))\b",
                normalized,
            )
        )
        total_question = bool(
            not unit_price_question
            and (
                re.search(r"\b(?:overall\s+)?(?:total|value)\b", normalized)
                or (
                    state_reference
                    and re.search(r"\b(?:cost|price|how\s+much)\b", normalized)
                )
            )
        )
        quantity_question = bool(
            state_reference
            and re.search(r"\b(?:how\s+many|qty|quantity|quantities)\b", normalized)
        )
        configuration_question = bool(
            state_reference
            and re.search(
                r"\b(?:which|what|show|list)\b.{0,40}"
                r"\b(?:skus?|products?|licen[cs]es?|lines?|configuration)\b|"
                r"\b(?:skus?|products?|licen[cs]es?|lines?|configuration)\b.{0,40}"
                r"\b(?:included|selected|have|current)\b",
                normalized,
            )
        )
        term_question = bool(
            state_reference
            and re.search(r"\b(?:term|billing|annual|monthly)\b", normalized)
        )
        if not (
            total_question
            or unit_price_question
            or line_total_question
            or quantity_question
            or configuration_question
            or term_question
        ):
            return None

        scenario = None
        scenario_label = ""
        if not captured_reference:
            if explicit_scenario == ScenarioType.RENEW_AS_IS or baseline_reference:
                scenario = session.confirmed_as_is or session.scenarios.get(
                    ScenarioType.RENEW_AS_IS
                )
                scenario_label = ScenarioType.RENEW_AS_IS.label
            elif explicit_scenario is not None:
                scenario = session.scenarios.get(explicit_scenario)
                scenario_label = explicit_scenario.label
            elif active_reference and session.active_scenario is not None:
                scenario = session.scenarios.get(session.active_scenario)
                scenario_label = "current " + session.active_scenario.label
            else:
                scenario = (
                    session.scenarios.get(session.active_scenario)
                    if session.active_scenario is not None
                    else None
                ) or session.confirmed_as_is
        if explicit_scenario is not None and scenario is None and not captured_reference:
            return (
                f"The {scenario_label or explicit_scenario.label} proposal has not been "
                "prepared in this session. Ask me to prepare it before requesting its "
                "configuration or commercial value."
            )
        if scenario is not None:
            visible_lines = [
                line for line in scenario.lines if line.proposed_quantity > 0
            ]
            label = scenario_label or scenario.scenario_type.label
            target = self._line_reference_from_message(
                visible_lines,
                message,
                requirement=False,
            )
            selected = (
                [line for line in visible_lines if line.line_id == target]
                if target is not None
                else visible_lines
            )
            if target is None and re.search(r"\bl\d+\b", normalized):
                requested = re.search(r"\bl\d+\b", normalized)
                assert requested is not None
                return (
                    f"I could not find {requested.group(0).upper()} in the committed "
                    f"{label} proposal. No value was inferred."
                )
            if unit_price_question or line_total_question:
                if target is None:
                    return (
                        "Which licence line do you mean? Send its displayed line ID or exact "
                        "product name; I will use only the committed proposal value."
                    )
                line = selected[0]
                unavailable = (
                    " Price is unavailable for this line."
                    if line.price_unavailable
                    else ""
                )
                if unit_price_question:
                    return (
                        f"The committed {label} unit price for {line.line_id} - "
                        f"{line.sku_title} is "
                        f"{format_money(line.unit_price, self._configuration.currency)}."
                        f"{unavailable}"
                    )
                return (
                    f"The committed {label} line total for {line.line_id} - "
                    f"{line.sku_title} is "
                    f"{format_money(line.extended_price, self._configuration.currency)}."
                    f"{unavailable}"
                )
            sections: list[str] = []
            if quantity_question or configuration_question:
                rows = [
                    f"{line.line_id} - {line.sku_title}: "
                    f"{line.proposed_quantity:,} licence(s)"
                    for line in selected
                ]
                sections.append(
                    f"Committed {label} configuration:\n" + "\n".join(rows)
                )
            if term_question:
                sections.append(
                    f"The committed {label} proposal uses term "
                    f"{scenario.term_duration} with {scenario.billing_plan} billing."
                )
            if total_question:
                unavailable = sum(line.price_unavailable for line in visible_lines)
                warning = (
                    f" {unavailable} line(s) still have unavailable pricing and are not "
                    "silently treated as free."
                    if unavailable
                    else ""
                )
                sections.append(
                    f"The committed {label} annual total is "
                    f"{format_money(scenario.total_value, self._configuration.currency)}."
                    f"{warning}"
                )
            return "\n\n".join(sections) if sections else None

        if session.estate is None:
            return None
        estate_lines = list(session.estate.lines)
        target = self._line_reference_from_message(
            estate_lines,
            message,
            requirement=True,
        )
        selected_estate = (
            [line for line in estate_lines if line.line_id == target]
            if target is not None
            else estate_lines
        )
        rows = [
            f"{line.line_id} - {line.display_title}: "
            f"{line.renewal_quantity:,} licence(s)"
            for line in selected_estate
        ]
        if total_question or unit_price_question or line_total_question:
            return (
                "Pricing remains paused until the complete captured requirement and every "
                "exact SKU are confirmed. I have not inferred or estimated a price.\n"
                + "Captured requirement:\n"
                + "\n".join(rows)
            )
        suffix = ""
        return "Captured requirement:\n" + "\n".join(rows) + suffix

    @classmethod
    def _is_explicit_title_choice(cls, message: str) -> bool:
        """Require selection language before accepting a title embedded in a sentence."""

        normalized = " ".join(
            message.casefold().replace("\u2019", "'").strip(" ?!.,").split()
        )
        if (
            cls._is_clear_non_requirement_turn(message)
            or "?" in message
            or re.match(
                r"^(?:who|what|which|where|when|why|how|can\s+i|could\s+i|"
                r"would\s+i|explain|tell\s+me)\b",
                normalized,
            )
            or cls._is_negated_action(
                message,
                r"(?:choose|select|use|confirm|go\s+with|pick)",
            )
            or re.search(
                r"\b(?:customer|client|seller|user|manager|analyst)\b.{0,48}"
                r"\b(?:said|says|asked|asks|selected|chose|picked|requested|requests?)\b|"
                r"\b(?:was|is|has\s+been)\s+(?:selected|chosen|picked)\s+by\s+"
                r"(?:the\s+)?(?:customer|client|seller|user|manager|analyst)\b",
                normalized,
            )
        ):
            return False
        return bool(
            re.search(
                r"\b(?:choose|chosen|select|selected|use|confirm|confirmed|"
                r"go with|pick|picked|that is|this is)\b",
                normalized,
            )
        )

    @classmethod
    def _is_direct_candidate_title_reply(cls, message: str) -> bool:
        """Accept a bare/explicit title answer, never a question about that title."""

        normalized = " ".join(
            message.casefold().replace("\u2019", "'").strip(" ?!.,").split()
        )
        if not normalized or "?" in message:
            return False
        if re.match(
            r"^(?:who|what|which|where|when|why|how|can\s+i|could\s+i|"
            r"would\s+i|if|unless|whether|explain|tell\s+me)\b",
            normalized,
        ):
            return False
        if cls._is_negated_action(
            message,
            r"(?:choose|select|use|confirm|go\s+with|pick)",
        ) or re.search(
            r"\b(?:customer|client|seller|user|manager|analyst)\b.{0,48}"
            r"\b(?:said|says|asked|asks|selected|chose|picked|requested|requests?)\b|"
            r"\b(?:was|is|has\s+been)\s+(?:selected|chosen|picked)\s+by\s+"
            r"(?:the\s+)?(?:customer|client|seller|user|manager|analyst)\b",
            normalized,
        ):
            return False
        return not bool(
            re.match(
                r"^(?:(?:the\s+)?(?:customer|client|seller|user)|he|she|they)\s+"
                r"(?:said|says|asked|asks|mentioned|mentions|wants?|requested|requests?)\b",
                normalized,
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

        # A standalone quantity belongs to the product fragment already retained in
        # capture_messages. Do this before model interpretation so it can never be
        # misrouted to set_quantity, which requires an existing line reference.
        if self._is_quantity_only_reply(message):
            return False
        intent = await self._interpret_pending_message(message, session)
        if intent is None:
            return False
        if intent.action in CONVERSATIONAL_ACTIONS:
            await self._execute_agent_intent(
                sender,
                intent,
                original_message=message,
            )
            return True
        if intent.action == "request_recommendation":
            await self._execute_agent_intent(
                sender,
                intent,
                original_message=message,
            )
            return True
        if self._is_clear_non_requirement_turn(message):
            await self._send_non_requirement_boundary(sender)
            return True
        if intent.action == "capture_requirement":
            return False
        if intent.action == "clarify":
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
        intent = self._intent_with_seller_supported_targets(
            intent,
            message,
            session,
            pending_slot_completion=False,
        )
        if not await self._is_assertive_requirement_capture(message, session):
            return False
        extracted = await self._extract_single_turn_requirement(message)
        product = str(getattr(intent, "product_query", "") or "").strip()
        quantity = int(getattr(intent, "quantity", -1) or -1)
        clarification = ""
        if extracted is not None:
            extracted_product, extracted_quantity, clarification = extracted
            grounded_extraction = self._intent_with_seller_supported_targets(
                self._agent_intent_with(
                    intent,
                    product_query=extracted_product,
                    quantity=extracted_quantity,
                ),
                message,
                session,
                pending_slot_completion=False,
            )
            product = product or str(grounded_extraction.product_query or "").strip()
            if quantity <= 0:
                quantity = grounded_extraction.quantity
        if not product:
            return False

        add_another = self._explicitly_adds_another_line(message, intent)
        if add_another and quantity <= 0:
            await self._orchestrator.remember_capture_message(
                sender,
                message[:2000],
            )
            await self._send_text(
                sender,
                clarification
                or f"How many {product} licences should I add to the requirement?",
            )
            return True

        if add_another:
            result = await self._orchestrator.add_requirement_sku(
                sender,
                product,
                quantity,
            )
        else:
            pending_lines = list(session.estate.pending_lines)
            supplied_line = str(getattr(intent, "line_id", "") or "").strip().upper()
            target = next(
                (line for line in pending_lines if line.line_id == supplied_line),
                None,
            )
            if target is None:
                inferred = self._line_reference_from_message(
                    pending_lines,
                    message,
                    requirement=True,
                )
                target = next(
                    (line for line in pending_lines if line.line_id == inferred),
                    None,
                )
            if target is None and len(pending_lines) == 1:
                target = pending_lines[0]
            if target is None:
                choices = "; ".join(
                    f"{line.line_id} ({line.source_product_title})"
                    for line in pending_lines[:10]
                )
                question = (
                    "Which unresolved licence should I correct? Reply with its product "
                    f"name: {choices}"
                )
                replacement_intent = self._agent_intent_with(
                    intent,
                    action="replace_sku",
                    product_query=product,
                    quantity=quantity,
                )
                pending = await self._pending_dialogue_for_intent(
                    sender,
                    replacement_intent,
                    question=question,
                    original_message=message,
                    operation="replace_sku",
                    scope="requirement",
                    awaiting_slot="line",
                )
                await self._orchestrator.set_pending_dialogue(sender, pending)
                await self._orchestrator.set_pending_match_prompt_suspended(sender, True)
                await self._send_text(sender, question)
                return True
            if quantity <= 0:
                quantity = target.renewal_quantity
            product = await self._qualified_pending_product_query(
                target.source_product_title,
                product,
                narrowing_required=target.candidate_narrowing_required,
            )
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

        session = await self._orchestrator.get_session(sender)
        intent = self._intent_with_seller_supported_targets(
            intent,
            message,
            session,
            pending_slot_completion=False,
        )
        if intent.action == "capture_requirement":
            if not await self._is_assertive_requirement_capture(message, session):
                return False
            product = str(intent.product_query or "").strip()
            if not product:
                extracted = await self._extract_single_turn_requirement(message)
                if extracted is not None:
                    grounded_extraction = self._intent_with_seller_supported_targets(
                        self._agent_intent_with(
                            intent,
                            product_query=extracted[0],
                            quantity=extracted[1],
                        ),
                        message,
                        session,
                        pending_slot_completion=False,
                    )
                    product = str(
                        grounded_extraction.product_query or ""
                    ).strip()
            if not product:
                return False
            product = await self._qualified_pending_product_query(
                pending.product_query,
                product,
                narrowing_required=pending.candidate_narrowing_required,
            )
            quantity = intent.quantity if intent.quantity > 0 else pending.quantity
            await self._orchestrator.cancel_sku_change(sender)
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

        if intent.action in {"add_sku", "replace_sku"}:
            # A complete newer instruction supersedes the whole unconfirmed operation,
            # but a complete product answer to an outstanding add/replace confirmation
            # retains that operation's target. A model can correctly classify a concise
            # answer such as ``Microsoft 365 E7 for one licence`` as requirement capture;
            # treating it as an unrelated add would silently lose replacement semantics.
            grounded = self._intent_with_seller_supported_targets(
                intent,
                message,
                session,
                pending_slot_completion=False,
            )
            if not self._is_assertive_commercial_instruction(
                message,
                grounded,
                session,
                pending_slot_completion=False,
            ):
                return False
            await self._orchestrator.cancel_sku_change(sender)
            await self._execute_agent_intent(
                sender,
                grounded,
                original_message=message,
            )
            return True

        extracted = await self._extract_single_turn_requirement(message)
        product = str(getattr(intent, "product_query", "") or "").strip()
        quantity = int(getattr(intent, "quantity", -1) or -1)
        if extracted is not None:
            extracted_product, extracted_quantity, _clarification = extracted
            grounded_extraction = self._intent_with_seller_supported_targets(
                self._agent_intent_with(
                    intent,
                    product_query=extracted_product,
                    quantity=extracted_quantity,
                ),
                message,
                session,
                pending_slot_completion=False,
            )
            product = product or str(grounded_extraction.product_query or "").strip()
            if quantity <= 0:
                quantity = grounded_extraction.quantity
        if not product:
            return False
        if quantity <= 0:
            quantity = pending.quantity
        product = await self._qualified_pending_product_query(
            pending.product_query,
            product,
            narrowing_required=pending.candidate_narrowing_required,
        )

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

    async def _qualified_pending_product_query(
        self,
        original_query: str,
        supplied_product: str,
        *,
        narrowing_required: bool,
    ) -> str:
        """Keep a complete replacement title independent of an older broad query.

        A qualifier such as ``Plan 2`` needs the earlier family name to become safe,
        while a complete catalogue title such as ``Microsoft 365 E7`` must replace the
        earlier query instead of becoming an impossible string such as
        ``Copilot Microsoft 365 E7``. Exact catalogue confidence is deterministic and
        therefore safer than guessing completeness from word count.
        """

        product = " ".join(supplied_product.split()).strip()
        if not narrowing_required or not product:
            return product
        candidates = await self._orchestrator.catalog_candidates(product)
        if any(candidate.confidence == 100 for candidate in candidates):
            return product
        return combine_product_qualifier(original_query, product)

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
        line_count = len(session.estate.lines)
        line_summary = "one SKU line" if line_count == 1 else f"{line_count} SKU lines"
        await self._send_text(
            sender,
            "*SkySecure Microsoft Licensing Advisor*\n\n"
            "Welcome back. I found an active saved draft containing "
            f"{line_summary} and "
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

    @classmethod
    def _quantity_from_reply(cls, message: str) -> int:
        if not cls._is_quantity_only_reply(message):
            return -1
        values = cls._seller_quantity_values(message)
        return next(iter(values)) if len(values) == 1 else -1

    @staticmethod
    def _product_from_reply(
        message: str,
        intent: AgentIntent,
        *,
        allow_single_token: bool = False,
    ) -> str:
        """Return a product-like answer, excluding operation words such as bare 'SKU'."""

        interpreted = str(getattr(intent, "product_query", "") or "").strip()
        candidates = [interpreted, message.strip()]
        generic = {
            "a",
            "about",
            "add",
            "an",
            "change",
            "current",
            "i",
            "it",
            "licence",
            "licences",
            "license",
            "licenses",
            "more",
            "new",
            "plan",
            "please",
            "product",
            "replace",
            "sku",
            "the",
            "this",
            "to",
            "want",
            "with",
        }
        markers = (
            "copilot",
            "defender",
            "dynamics",
            "entra",
            "exchange",
            "intune",
            "microsoft",
            "office",
            "power bi",
            "project",
            "teams",
            "visio",
            "windows",
        )
        for index, candidate in enumerate(candidates):
            normalized = normalize_product_title(candidate)
            if not normalized or re.fullmatch(r"\d+", normalized):
                continue
            if re.match(r"^(?:who|what|where|when|why|how)\b", normalized):
                continue
            meaningful = set(normalized.split()) - generic
            has_marker = any(marker in normalized for marker in markers)
            has_code = bool(re.search(r"\b(?:m?e\d+|p\d+|f\d+|o365|m365)\b", normalized))
            if has_marker or has_code or (
                index == 0 and bool(interpreted) and len(meaningful) >= 1
            ) or (
                allow_single_token and len(meaningful) == 1
            ):
                cleaned = re.sub(
                    r"^(?:please\s+)?(?:i\s+)?(?:want|need)\s+(?:to\s+)?"
                    r"(?:(?:add|use|replace|change)\s+)?",
                    "",
                    candidate,
                    flags=re.IGNORECASE,
                ).strip(" .")
                return cleaned or candidate.strip()
        return ""

    async def _direct_product_slot_query(
        self,
        message: str,
        intent: AgentIntent,
    ) -> str:
        """Return a catalogue-backed direct answer to a visible product question.

        The seller's text is authoritative here.  A hostile or mistaken model label must
        not reject an exact title, while a narrative such as "I watched a video about
        Power BI Pro yesterday" must not silently fill the product slot.
        """

        normalized = " ".join(
            message.casefold().replace("\u2019", "'").strip(" ?!.,").split()
        )
        if (
            not normalized
            or normalized in AFFIRMATIVE_REPLIES
            or normalized in UNCERTAIN_REPLIES
            or normalized in CANCEL_REPLIES
            or "?" in message
            or self._is_licensing_question_message(message)
        ):
            return ""
        explicit_selection = re.fullmatch(
            r"(?:(?:please\s+)?(?:choose|select|use|pick|add|include)\s+(?:the\s+)?|"
            r"(?:the\s+)?(?:product|sku|plan|licen[cs]e)\s+"
            r"(?:is|should\s+be)\s+|(?:it|this)\s+should\s+be\s+)(?P<product>.+)",
            normalized,
        )
        explicit_product = (
            explicit_selection.group("product").strip()
            if explicit_selection is not None
            else ""
        )
        narrative_predicate = re.search(
            r"\b(?:is|are|was|were|has|have|had|seems?|looks?|costs?|priced?|"
            r"includes?|supports?|contains?|requires?|expensive|cheap|costly|"
            r"available|unavailable|better|worse)\b",
            explicit_product or normalized,
        )
        if narrative_predicate:
            return ""
        if re.search(
            r"\b(?:watched|read|heard|saw|discussed|mentioned|talked|spoke|"
            r"video|article|yesterday|last\s+(?:week|month|year))\b",
            normalized,
        ) or re.match(
            r"^(?:(?:the\s+)?(?:customer|client|seller|user)|he|she|they)\s+"
            r"(?:said|says|asked|asks|mentioned|mentions|selected|chose|picked)\b",
            normalized,
        ):
            return ""

        seller_query = re.sub(
            r"^(?:please\s+)?(?:(?:i|we)\s+(?:want|need|would\s+like)\s+(?:to\s+)?)?"
            r"(?:(?:choose|select|use|pick|add|include)\s+)?(?:the\s+)?",
            "",
            message.strip(),
            flags=re.IGNORECASE,
        ).strip(" .")
        interpreted = str(getattr(intent, "product_query", "") or "").strip()
        candidates: list[str] = []
        if explicit_product:
            candidates.append(explicit_product)
        if interpreted and self._seller_product_role_is_supported(
            message,
            interpreted,
            str(getattr(intent, "action", "") or ""),
        ):
            candidates.append(interpreted)
        candidates.extend([seller_query, message.strip()])
        seen: set[str] = set()
        for query in candidates:
            key = normalize_product_title(query)
            if not key or key in seen:
                continue
            seen.add(key)
            matches = await self._orchestrator.catalog_candidates(query)
            if matches:
                return query
        return ""

    @staticmethod
    def _is_non_value_slot_reply(message: str) -> bool:
        """Reject acknowledgements/refusals that cannot be a free-text field value."""

        normalized = " ".join(
            message.casefold().replace("\u2019", "'").strip(" ?!.,").split()
        )
        return bool(
            normalized in AFFIRMATIVE_REPLIES
            or normalized in UNCERTAIN_REPLIES
            or normalized in CANCEL_REPLIES
            or re.fullmatch(
                r"(?:yes|yeah|yep|no|nope|ok|okay|sure|thanks?|thank\s+you|"
                r"cancel(?:\s+(?:it|this|that|please))?|please\s+cancel|"
                r"never\s*mind)",
                normalized,
            )
        )

    @staticmethod
    def _change_dimension(message: str) -> Literal["sku", "quantity", "disposition", "none"]:
        value = " ".join(message.casefold().strip(" ?!.,").split())
        if re.search(r"\b(?:sku|product|plan|replace|replacement)\b", value):
            return "sku"
        if re.search(r"\b(?:quantity|qty|count|licences|licenses|seats|users)\b", value):
            return "quantity"
        if re.search(r"\b(?:disposition|retain|remove|migrate|included)\b", value):
            return "disposition"
        return "none"

    @staticmethod
    def _questions_semantically_equivalent(left: str, right: str) -> bool:
        stop_words = {
            "a",
            "about",
            "and",
            "do",
            "i",
            "is",
            "it",
            "like",
            "please",
            "the",
            "to",
            "what",
            "which",
            "would",
            "you",
            "your",
        }
        left_tokens = set(normalize_product_title(left).split()) - stop_words
        right_tokens = set(normalize_product_title(right).split()) - stop_words
        if not left_tokens or not right_tokens:
            return False
        overlap = len(left_tokens & right_tokens)
        return overlap / max(len(left_tokens), len(right_tokens)) >= 0.6

    @staticmethod
    def _agent_intent_with(
        source: AgentIntent,
        **updates: object,
    ) -> AgentIntent:
        defaults: dict[str, object] = {
            "action": "clarify",
            "scenario": "none",
            "line_id": "",
            "quantity": -1,
            "copilot_quantity": -1,
            "product_query": "",
            "disposition": "none",
            "boolean_value": "none",
            "percentage": -1.0,
            "amount": 0.0,
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
        for field in AgentIntent.model_fields:
            if hasattr(source, field):
                defaults[field] = getattr(source, field)
        defaults.update(updates)
        return AgentIntent.model_validate(defaults)

    @staticmethod
    def _pending_lines(
        session: WorkflowSession,
        scope: Literal["none", "requirement", "scenario"],
        scenario_type: ScenarioType | None = None,
    ) -> tuple[list, bool]:
        requirement = scope == "requirement"
        if requirement and session.estate is not None:
            return list(session.estate.lines), True
        selected_scenario = scenario_type or session.active_scenario
        if selected_scenario is not None:
            scenario = session.scenarios.get(selected_scenario)
            if scenario is not None:
                return [line for line in scenario.lines if line.proposed_quantity > 0], False
        return [], requirement

    def _pending_line_id(
        self,
        pending: PendingDialogue,
        session: WorkflowSession,
        message: str,
        intent: AgentIntent,
    ) -> str:
        lines, requirement = self._pending_lines(
            session,
            pending.scope,
            pending.scenario_type,
        )
        available = {line.line_id for line in lines}
        if pending.scope != "requirement" and pending.source_line_id:
            selected_scenario = pending.scenario_type or session.active_scenario
            scenario = (
                session.scenarios.get(selected_scenario)
                if selected_scenario is not None
                else None
            )
            if scenario is not None and any(
                line.line_id == pending.source_line_id for line in scenario.lines
            ):
                # Preserve an already-grounded source row even when that proposal renders it
                # at quantity zero. It remains a real auditable row and is a valid source for
                # a seller-requested replacement.
                return pending.source_line_id
        current_line = str(getattr(intent, "line_id", "") or "").strip().upper()
        explicit_message_lines = [
            match.upper()
            for match in re.findall(r"\bL\d+\b", message, flags=re.IGNORECASE)
            if match.upper() in available
        ]
        # A complete newer instruction such as ``Replace L2 with ...`` owns its explicit
        # target. The saved target is only a fallback for a true slot answer such as a bare
        # replacement product or quantity.
        for supplied in (current_line, *explicit_message_lines, pending.source_line_id):
            if supplied in available:
                return supplied
        inferred = self._line_reference_from_message(
            lines,
            message,
            requirement=requirement,
        )
        if inferred is not None:
            return inferred
        inferred = self._line_reference_from_message(
            lines,
            f"{pending.context_message} {pending.question}",
            requirement=requirement,
        )
        if inferred is not None:
            return inferred
        return lines[0].line_id if len(lines) == 1 else ""

    def _pending_target_conflicts_with_current_turn(
        self,
        pending: PendingDialogue,
        session: WorkflowSession,
        message: str,
        intent: AgentIntent,
    ) -> bool:
        """Detect a complete same-action instruction that supersedes saved slot context.

        Short answers inherit the visible question. A newer complete action must not inherit
        an older line, proposal, product, quantity, or disposition merely because the model
        assigned both turns the same action label.
        """

        if pending.operation == "none" or intent.action != pending.operation:
            return False

        lines, _ = self._pending_lines(
            session,
            pending.scope,
            pending.scenario_type,
        )
        available_lines = {line.line_id for line in lines}
        current_line = str(getattr(intent, "line_id", "") or "").strip().upper()
        explicit_message_lines = {
            value.upper()
            for value in re.findall(r"\bL\d+\b", message, flags=re.IGNORECASE)
            if value.upper() in available_lines
        }
        current_lines = ({current_line} if current_line in available_lines else set()) | (
            explicit_message_lines
        )
        if (
            pending.source_line_id in available_lines
            and current_lines
            and pending.source_line_id not in current_lines
        ):
            return True

        current_scenario = self._seller_scenario_reference(message)
        if (
            pending.scenario_type is not None
            and current_scenario is not None
            and current_scenario != pending.scenario_type
        ):
            return True

        normalized = " ".join(message.casefold().strip(" ?!.,").split())
        explicit_action = bool(
            re.match(
                r"^(?:please\s+)?(?:(?:i|we)\s+(?:want|need|would\s+like)\s+"
                r"(?:to\s+)?)?(?:add|include|replace|change|update|set|remove|"
                r"delete|retain|migrate|build|prepare)\b",
                normalized,
            )
        )
        if not explicit_action:
            return False

        current_product = str(getattr(intent, "product_query", "") or "").strip()
        if (
            pending.product_query
            and current_product
            and normalize_product_title(current_product)
            != normalize_product_title(pending.product_query)
        ):
            return True

        current_quantity = int(getattr(intent, "quantity", -1) or -1)
        if (
            pending.quantity >= 0
            and current_quantity >= 0
            and current_quantity != pending.quantity
        ):
            return True
        current_copilot = int(getattr(intent, "copilot_quantity", -1) or -1)
        if (
            pending.copilot_quantity >= 0
            and current_copilot >= 0
            and current_copilot != pending.copilot_quantity
        ):
            return True
        current_disposition = str(
            getattr(intent, "disposition", "none") or "none"
        )
        if (
            pending.disposition != "none"
            and current_disposition != "none"
            and current_disposition != pending.disposition
        ):
            return True
        return False

    def _pending_line_title(
        self,
        pending: PendingDialogue,
        session: WorkflowSession,
        line_id: str,
    ) -> str:
        lines, requirement = self._pending_lines(
            session,
            pending.scope,
            pending.scenario_type,
        )
        for line in lines:
            if line.line_id == line_id:
                return line.display_title if requirement else line.sku_title
        return "the selected licence"

    async def _repeat_or_release_pending(
        self,
        sender: str,
        pending: PendingDialogue,
        question: str,
    ) -> None:
        attempts = pending.failed_attempts + 1
        if attempts >= 2:
            await self._orchestrator.clear_pending_dialogue(sender)
            await self._send_text(
                sender,
                "No change was made because the required detail was not supplied. You can "
                "continue with another request, or send the exact product and quantity in one "
                "message when ready.",
            )
            return
        updated = pending.model_copy(
            update={"question": question[:500], "failed_attempts": attempts}
        )
        await self._orchestrator.set_pending_dialogue(sender, updated)
        await self._send_text(sender, question[:500])

    async def _clear_pending_if_unchanged(
        self,
        sender: str,
        pending: PendingDialogue,
    ) -> None:
        current = await self._orchestrator.get_session(sender)
        if current is not None and current.pending_dialogue == pending:
            await self._orchestrator.clear_pending_dialogue(sender)

    @staticmethod
    def _commercial_state_snapshot(session: WorkflowSession) -> dict:
        """Return state whose change proves a pending operation may have committed."""

        return session.model_dump(
            mode="json",
            exclude={
                "pending_dialogue",
                "updated_at",
                "processed_message_ids",
                "inflight_message_ids",
                "failure_notified_message_ids",
            },
        )

    @staticmethod
    def _committed_commercial_state_snapshot(session: WorkflowSession) -> dict:
        """Return committed state while ignoring the two transient question stacks."""

        return session.model_dump(
            mode="json",
            exclude={
                "pending_dialogue",
                "pending_sku_change",
                "updated_at",
                "processed_message_ids",
                "inflight_message_ids",
                "failure_notified_message_ids",
            },
        )

    async def _resolve_pending_dialogue_preserving_sku(
        self,
        sender: str,
        message: str,
        session: WorkflowSession,
    ) -> bool:
        """Resolve a newer read-only question without losing an older SKU choice.

        A seller may ask a Microsoft product question while an add/replace candidate list is
        still waiting for confirmation. If the official answer requires clarification, both
        pieces of context are valid: the clarification must receive the next reply, while the
        uncommitted SKU choice must remain available afterwards. A real commercial mutation
        or a newer SKU choice always supersedes the older choice.
        """

        pending_sku = session.pending_sku_change
        pending_dialogue = session.pending_dialogue
        if (
            pending_sku is None
            or pending_dialogue is None
            or pending_dialogue.detail_value != "official_product_clarification"
        ):
            return False
        candidate_reference = self._single_candidate_number(message) is not None or any(
            normalize_product_title(candidate.sku_title)
            in normalize_product_title(message)
            for candidate in pending_sku.candidates
            if normalize_product_title(candidate.sku_title)
        )
        if candidate_reference:
            await self._repeat_or_release_pending(
                sender,
                pending_dialogue,
                "The newer clarification is still active, so I did not apply that option to "
                "the earlier hidden SKU choice. " + pending_dialogue.question,
            )
            latest = await self._orchestrator.get_session(sender)
            if latest is not None and latest.pending_dialogue is None:
                await self._send_sku_change_result(
                    sender,
                    SkuChangeResult(
                        state="confirmation_required",
                        confirmation=pending_sku,
                    ),
                )
            return True
        before = self._committed_commercial_state_snapshot(session)
        handled = await self._resolve_pending_dialogue(sender, message, session)
        if not handled:
            return False
        latest = await self._orchestrator.get_session(sender)
        if (
            latest is not None
            and self._committed_commercial_state_snapshot(latest) == before
            and (
                latest.pending_sku_change is None
                or latest.pending_sku_change.id == pending_sku.id
            )
        ):
            await self._orchestrator.restore_pending_sku_change(
                sender,
                pending_sku,
                preserve_pending_dialogue=latest.pending_dialogue is not None,
            )
            if latest.pending_dialogue is None:
                await self._send_sku_change_result(
                    sender,
                    SkuChangeResult(
                        state="confirmation_required",
                        confirmation=pending_sku,
                    ),
                )
        return True

    async def _execute_completed_pending(
        self,
        sender: str,
        pending: PendingDialogue,
        intent: AgentIntent,
        message: str,
    ) -> None:
        # The saved question is now complete. Remove it before dispatch so guarded
        # scenario operations do not mistake the operation being completed for a
        # separate unfinished edit. Per-seller dispatch is serialized; if execution
        # fails before changing domain state, restore this exact question. An output/network
        # failure after a successful mutation must never re-arm an already-applied action.
        current = await self._orchestrator.get_session(sender)
        cleared = current is not None and current.pending_dialogue == pending
        before_state = (
            self._commercial_state_snapshot(current) if current is not None else None
        )
        if cleared:
            await self._orchestrator.clear_pending_dialogue(sender)
        try:
            await self._execute_agent_intent(
                sender,
                intent,
                original_message=message,
                pending_slot_completion=True,
            )
        except Exception:
            latest = await self._orchestrator.get_session(sender)
            unchanged = bool(
                latest is not None
                and before_state is not None
                and self._commercial_state_snapshot(latest) == before_state
            )
            if (
                cleared
                and unchanged
                and latest is not None
                and latest.pending_dialogue is None
                and latest.pending_sku_change is None
            ):
                # A newly-created fuzzy selection is newer state and must never be cleared
                # by restoring the older question.
                await self._orchestrator.set_pending_dialogue(sender, pending)
            raise

    async def _execute_conversational_preserving_pending(
        self,
        sender: str,
        pending: PendingDialogue,
        intent: AgentIntent,
        message: str,
    ) -> None:
        """Answer an interruption without silently discarding an unfinished operation."""

        await self._execute_agent_intent(sender, intent, original_message=message)
        current = await self._orchestrator.get_session(sender)
        official_product_question = (
            intent.action == "answer_question"
            and str(getattr(intent, "detail_label", "")).strip().casefold()
            == "official_product_question"
        )
        if current is not None and not (
            official_product_question and current.pending_dialogue is not None
        ):
            # Ordinary read-only interruptions preserve the commercial question already in
            # progress. Official product research is the exception when it creates a newer,
            # required clarification: that question describes the seller's latest turn and
            # must not be overwritten by stale commercial context.
            if current.pending_dialogue != pending:
                await self._orchestrator.set_pending_dialogue(sender, pending)
            attempts = await self._orchestrator.record_pending_dialogue_failure(sender)
            if attempts >= 2:
                await self._orchestrator.clear_pending_dialogue(sender)
                await self._send_text(
                    sender,
                    "I closed the earlier unfinished question after two interruptions so it "
                    "will not keep repeating or intercept a later reply. No licensing or "
                    "commercial value was changed; restate the change when you want to "
                    "continue it.",
                )
                return
            await self._send_text(
                sender,
                "The earlier proposal change is still waiting for this detail: "
                + pending.question,
            )

    async def _pending_reply_is_compatible(
        self,
        pending: PendingDialogue,
        session: WorkflowSession,
        message: str,
        intent: AgentIntent,
    ) -> bool:
        """Return whether this turn can safely fill the pending operation's next slot.

        This deterministic compatibility check is deliberately independent of the model's
        top-level action. A short answer such as ``51`` must still fill a requested quantity
        even if the language model labels it as an acknowledgement. Conversely, a complete
        new commercial action must not inherit an older add/replace operation.
        """

        operation = pending.operation
        if operation == "none":
            return False
        pending_action_terms = {
            "add_sku": r"(?:add|include|use)",
            "replace_sku": r"(?:replace|switch|swap|change|use)",
            "set_quantity": r"(?:change|set|update|use)",
            "set_copilot": r"(?:change|set|update|use)",
            "set_disposition": r"(?:retain|remove|delete|migrate|include)",
            "build_scenario": r"(?:build|prepare|use|select|choose)",
            "set_term": r"(?:set|change|use|apply)",
            "set_billing": r"(?:set|change|use|apply)",
            "set_segment": r"(?:set|change|use|apply)",
            "set_currency": r"(?:set|change|use|convert)",
            "add_comment": r"(?:add|include|use)",
        }
        pending_evidence = pending_action_terms.get(operation, r"(?:set|change|use)")
        direct_pending_request = bool(
            re.search(rf"\b{pending_evidence}\b", message, flags=re.IGNORECASE)
            and self._is_direct_action_request(message, pending_evidence)
            and not self._is_negated_action(message, pending_evidence)
        )
        normalized_for_semantics = " ".join(
            message.casefold().replace("\u2019", "'").strip(" ?!.,").split()
        )
        informational_or_hypothetical = bool(
            ("?" in message)
            or re.match(
                r"^(?:who|what|which|where|when|why|how|can|could|would|should|"
                r"do|does|did|is|are|will|may|if|unless|whether|suppose|"
                r"supposing|assuming|imagine|for example|example)\b",
                normalized_for_semantics,
            )
            or re.search(
                r"\b(?:want|need|would\s+like)\s+to\s+know\b|"
                r"\b(?:want|need|would\s+like)\s+(?:a\s+)?"
                r"(?:pricing|price|quote|cost)\b|"
                r"\b(?:for example|as an example|hypothetically)\b",
                normalized_for_semantics,
            )
            or re.match(
                r"^(?:(?:the\s+)?(?:customer|client|seller|user)|he|she|they)\s+"
                r"(?:said|says|asked|asks|mentioned|mentions|requested|requests?)\b",
                normalized_for_semantics,
            )
            or re.search(
                r"\b(?:no\s+need(?:\s+(?:to|for))?|need\s+not|"
                r"not\s+(?:needed|required)|do\s+not\s+(?:need|want|require)|"
                r"don't\s+(?:need|want|require)|dont\s+(?:need|want|require))\b",
                normalized_for_semantics,
            )
            or re.search(
                r"\b(?:leave|keep)\b.{0,60}\b(?:unchanged|as[ -]?is|the\s+same)\b",
                normalized_for_semantics,
            )
            or re.search(
                r"\b(?:but\s+)?not\s+(?:to\s+)?-?\d+(?:\.\d+)?\b",
                normalized_for_semantics,
            )
        )
        if informational_or_hypothetical and not direct_pending_request:
            return False
        if self._pending_target_conflicts_with_current_turn(
            pending,
            session,
            message,
            intent,
        ):
            return False
        normalized = " ".join(message.casefold().strip(" ?!.,").split())
        quantity = self._quantity_from_reply(message)
        same_action = intent.action == operation
        commercial_actions = FINALIZATION_CORRECTION_ACTIONS | {
            "capture_requirement",
            "compare",
            "compare_enterprise_options",
            "confirm_matches",
            "confirm_sku",
            "confirm_validation",
            "finalize",
            "reject_validation",
            "reset_requirement",
        }
        distinct_commercial_action = (
            intent.action in commercial_actions and not same_action
        )
        if pending.awaiting_slot == "scenario":
            # A deterministic proposal token such as ``ME5`` is authoritative for the
            # exact slot that was just requested, even if the stateless model labels the
            # short reply as a new requirement. A complete action that merely mentions
            # ME5 is newer intent and must supersede the stale operation.
            return self._scenario_from_bare_reply(message) is not None
        if pending.awaiting_slot == "line":
            if distinct_commercial_action:
                return False
            lines, requirement = self._pending_lines(
                session,
                pending.scope,
                pending.scenario_type,
            )
            return self._line_reference_from_message(
                lines,
                message,
                requirement=requirement,
            ) is not None
        if pending.awaiting_slot == "quantity":
            # A bare numeric answer belongs to the quantity question in front of the
            # seller. This deterministic fact outranks a model label such as set_copilot;
            # complete new instructions are not quantity-only and therefore do not enter
            # this branch.
            if quantity >= 0:
                return True
            if distinct_commercial_action and not (
                intent.action == "capture_requirement"
                and not str(getattr(intent, "product_query", "") or "").strip()
            ):
                return False
            # The model's numeric field is not evidence that the seller supplied that
            # value. Only the strict, full-message quantity grammar above may fill the
            # visible slot; narrative text such as "15 was the old amount" must not.
            return False
        if pending.awaiting_slot == "product":
            return bool(await self._direct_product_slot_query(message, intent))
        if pending.awaiting_slot == "disposition":
            if distinct_commercial_action:
                return False
            return bool(
                re.search(
                    r"\b(?:retain|remove|migrate|include|included|replace)\b",
                    normalized,
                )
            ) or same_action
        if operation == "choose_change":
            if distinct_commercial_action:
                return False
            return self._change_dimension(message) != "none"
        if operation == "add_sku":
            if pending.product_query and pending.quantity <= 0:
                return quantity >= 0
            if not pending.product_query:
                return intent.action in {"add_sku", "capture_requirement", "clarify"} and bool(
                    self._product_from_reply(message, intent)
                )
            return same_action
        if operation == "replace_sku":
            if not pending.source_line_id:
                lines, requirement = self._pending_lines(
                    session,
                    pending.scope,
                    pending.scenario_type,
                )
                if self._line_reference_from_message(
                    lines,
                    message,
                    requirement=requirement,
                ) is not None:
                    return True
            if not pending.product_query:
                return intent.action in {"replace_sku", "capture_requirement", "clarify"} and bool(
                    self._product_from_reply(message, intent)
                )
            return same_action
        if operation in {"set_quantity", "set_copilot"}:
            return quantity >= 0 or same_action
        if operation == "set_disposition":
            return bool(
                re.search(
                    r"\b(?:retain|remove|migrate|include|included|replace)\b",
                    normalized,
                )
            ) or same_action
        if operation == "build_scenario":
            if pending.awaiting_slot == "scenario":
                return self._scenario_from_bare_reply(message) is not None
            return self._scenario_from_request(intent, message) is not None
        if operation in {
            "set_term",
            "set_billing",
            "set_segment",
            "set_currency",
            "add_comment",
        }:
            # Never convert an information question, greeting, acknowledgement, or
            # unrelated turn into a seller comment or commercial attribute. Only a
            # same-action extraction, or an unambiguous non-question clarification,
            # may fill these free-text slots.
            if self._is_non_value_slot_reply(message):
                return False
            if self._is_licensing_question_message(message):
                return False
            if operation == "set_segment":
                return bool(
                    re.fullmatch(
                        r"(?:commercial|education|academic|government|non[ -]?profit|charity)",
                        normalized,
                    )
                )
            if operation == "set_term":
                return bool(
                    re.fullmatch(
                        r"(?:p\d+[ymd]|annual|yearly|one\s+year|\d+\s+(?:months?|years?))",
                        normalized,
                    )
                )
            if operation == "set_billing":
                return bool(re.fullmatch(r"(?:annual|yearly|monthly)", normalized))
            if operation == "set_currency":
                return bool(re.fullmatch(r"(?:inr|usd|eur|gbp)", normalized))
            if operation == "add_comment":
                return bool(message.strip()) and len(normalized.split()) >= 2
            return same_action
        if operation in {"request_recommendation", "compare_enterprise_options"}:
            return same_action
        return False

    async def _resolve_structured_pending_dialogue(
        self,
        sender: str,
        message: str,
        session: WorkflowSession,
        pending: PendingDialogue,
        interpreted: AgentIntent,
    ) -> bool:
        """Merge a short answer into the saved operation instead of reclassifying it."""

        operation = pending.operation
        if operation == "none":
            return False

        if operation == "choose_change":
            dimension = self._change_dimension(message)
            line_id = self._pending_line_id(pending, session, message, interpreted)
            title = self._pending_line_title(pending, session, line_id)
            if dimension in {"sku", "quantity", "disposition"} and not line_id:
                next_operation = {
                    "sku": "replace_sku",
                    "quantity": "set_quantity",
                    "disposition": "set_disposition",
                }[dimension]
                verb = {
                    "sku": "replace",
                    "quantity": "change",
                    "disposition": "update",
                }[dimension]
                question = (
                    f"Which licence in the current proposal would you like to {verb}? "
                    "Reply with its product name."
                )
                await self._orchestrator.set_pending_dialogue(
                    sender,
                    pending.model_copy(
                        update={
                            "operation": next_operation,
                            "awaiting_slot": "line",
                            "source_line_id": "",
                            "question": question,
                            "failed_attempts": 0,
                        }
                    ),
                )
                await self._send_text(sender, question)
                return True
            if dimension == "sku":
                replacement = self._product_from_reply(message, interpreted)
                if replacement:
                    completed = self._agent_intent_with(
                        interpreted,
                        action="replace_sku",
                        line_id=line_id,
                        product_query=replacement,
                        quantity=-1,
                    )
                    await self._execute_completed_pending(
                        sender, pending, completed, message
                    )
                    return True
                question = f"Which Microsoft product should replace {title}?"
                await self._orchestrator.set_pending_dialogue(
                    sender,
                    pending.model_copy(
                        update={
                            "operation": "replace_sku",
                            "awaiting_slot": "product",
                            "source_line_id": line_id,
                            "question": question,
                            "failed_attempts": 0,
                        }
                    ),
                )
                await self._send_text(sender, question)
                return True
            if dimension == "quantity":
                quantity = self._quantity_from_reply(message)
                interpreted_quantity = int(
                    getattr(interpreted, "quantity", -1) or -1
                )
                if quantity < 0 and interpreted_quantity >= 0:
                    quantity = interpreted_quantity
                if quantity >= 0:
                    completed = self._agent_intent_with(
                        interpreted,
                        action="set_quantity",
                        line_id=line_id,
                        quantity=quantity,
                    )
                    await self._execute_completed_pending(
                        sender, pending, completed, message
                    )
                    return True
                question = f"What quantity should I set for {title}?"
                await self._orchestrator.set_pending_dialogue(
                    sender,
                    pending.model_copy(
                        update={
                            "operation": "set_quantity",
                            "awaiting_slot": "quantity",
                            "source_line_id": line_id,
                            "question": question,
                            "failed_attempts": 0,
                        }
                    ),
                )
                await self._send_text(sender, question)
                return True
            if dimension == "disposition":
                disposition = str(
                    getattr(interpreted, "disposition", "none") or "none"
                )
                if disposition == "none":
                    normalized = " ".join(message.casefold().split())
                    disposition = next(
                        (
                            value
                            for value in (
                                "retain",
                                "remove",
                                "migrate",
                                "included",
                                "replace",
                            )
                            if re.search(rf"\b{value}\b", normalized)
                        ),
                        "none",
                    )
                if disposition == "replace":
                    question = f"Which Microsoft product should replace {title}?"
                    await self._orchestrator.set_pending_dialogue(
                        sender,
                        pending.model_copy(
                            update={
                                "operation": "replace_sku",
                                "awaiting_slot": "product",
                                "source_line_id": line_id,
                                "question": question,
                                "failed_attempts": 0,
                            }
                        ),
                    )
                    await self._send_text(sender, question)
                    return True
                if disposition != "none":
                    completed = self._agent_intent_with(
                        interpreted,
                        action="set_disposition",
                        line_id=line_id,
                        disposition=disposition,
                    )
                    await self._execute_completed_pending(
                        sender, pending, completed, message
                    )
                    return True
                question = f"Should I retain, remove, or replace {title}?"
                await self._orchestrator.set_pending_dialogue(
                    sender,
                    pending.model_copy(
                        update={
                            "operation": "set_disposition",
                            "awaiting_slot": "disposition",
                            "source_line_id": line_id,
                            "question": question,
                            "failed_attempts": 0,
                        }
                    ),
                )
                await self._send_text(sender, question)
                return True
            await self._repeat_or_release_pending(sender, pending, pending.question)
            return True

        scenario_type = pending.scenario_type
        selecting_scenario = pending.awaiting_slot == "scenario"
        if selecting_scenario:
            scenario_type = self._scenario_from_bare_reply(message)
            if scenario_type is None:
                await self._repeat_or_release_pending(sender, pending, pending.question)
                return True
        elif scenario_type is None and operation in {
            "replace_sku",
            "set_quantity",
            "set_copilot",
            "set_disposition",
            "build_scenario",
        }:
            # A seller may answer a different missing slot first (for example, "ME5"
            # while the target line is still unknown). Preserve that valid information
            # and continue asking only for the detail that remains missing.
            scenario_type = self._scenario_from_request(interpreted, message)

        scenario_progressed = scenario_type != pending.scenario_type
        if scenario_progressed:
            # Persist the seller's unambiguous scenario answer before resolving the next
            # slot.  This makes the selected proposal authoritative for line-name lookup and
            # lets completed operations clear the exact pending object they consumed.
            pending = pending.model_copy(
                update={"scenario_type": scenario_type, "failed_attempts": 0}
            )
            await self._orchestrator.set_pending_dialogue(sender, pending)

        line_id = pending.source_line_id
        selecting_line = (
            operation in {"replace_sku", "set_quantity", "set_disposition"}
            and not line_id
        )
        if operation in {"replace_sku", "set_quantity", "set_disposition"}:
            # A bare scenario answer (for example ``ME5``) names the proposal, not its
            # synthetic BASE line. Never consume that token as a previously *missing* line,
            # but retain a source line that the seller already grounded in the saved
            # operation before the proposal question was asked.
            line_id = (
                ""
                if selecting_scenario and not pending.source_line_id
                else self._pending_line_id(pending, session, message, interpreted)
            )
            if not line_id:
                target_lines, requirement_scope = self._pending_lines(
                    session,
                    pending.scope,
                    scenario_type,
                )
                verb = {
                    "replace_sku": "replace",
                    "set_quantity": "change",
                    "set_disposition": "update",
                }[operation]
                target_name = (
                    "the current requirement"
                    if requirement_scope
                    else (
                        scenario_type.label
                        if scenario_type is not None
                        else "the current proposal"
                    )
                )
                choices = "; ".join(
                    f"{line.line_id} ({line.display_title if requirement_scope else line.sku_title})"
                    for line in target_lines[:8]
                )
                question = (
                    f"Which licence in {target_name} would you like to {verb}? "
                    "Reply with its product name"
                    + (f": {choices}." if choices else ".")
                )
                updated = pending.model_copy(
                    update={
                        "scenario_type": scenario_type,
                        "awaiting_slot": "line",
                        "question": question[:500],
                        "failed_attempts": 0 if scenario_progressed else pending.failed_attempts,
                    }
                )
                if scenario_progressed:
                    await self._orchestrator.set_pending_dialogue(sender, updated)
                    await self._send_text(sender, question[:500])
                else:
                    await self._repeat_or_release_pending(
                        sender, updated, question[:500]
                    )
                return True

        quantity = pending.quantity
        reply_quantity = self._quantity_from_reply(message)
        if reply_quantity >= 0:
            quantity = reply_quantity
        elif quantity < 0 and int(getattr(interpreted, "quantity", -1) or -1) >= 0:
            quantity = int(interpreted.quantity)

        product = pending.product_query
        reply_product = (
            await self._direct_product_slot_query(message, interpreted)
            if pending.awaiting_slot == "product"
            else self._product_from_reply(message, interpreted)
        )
        source_only_replacement_reply = bool(
            operation == "replace_sku"
            and selecting_line
            and not re.search(r"\b(?:with|by)\b", message, flags=re.IGNORECASE)
        )
        if (
            reply_product
            and not selecting_scenario
            and not source_only_replacement_reply
            and not (selecting_line and bool(product))
        ):
            product = reply_product

        if operation == "add_sku":
            if not product:
                question = "Which exact Microsoft product should I add?"
            elif quantity <= 0:
                question = f"How many {product} licences should I add?"
            else:
                completed = self._agent_intent_with(
                    interpreted,
                    action="add_sku",
                    scenario=scenario_type.value if scenario_type else "none",
                    product_query=product,
                    quantity=quantity,
                )
                await self._execute_completed_pending(
                    sender, pending, completed, message
                )
                return True
            progressed = product != pending.product_query or quantity != pending.quantity
            updated = pending.model_copy(
                update={
                    "product_query": product,
                    "quantity": quantity,
                    "scenario_type": scenario_type,
                    "question": question,
                    "awaiting_slot": (
                        "product" if not product else "quantity"
                    ),
                    "failed_attempts": 0 if progressed else pending.failed_attempts,
                }
            )
            if progressed:
                await self._orchestrator.set_pending_dialogue(sender, updated)
                await self._send_text(sender, question)
            else:
                await self._repeat_or_release_pending(sender, updated, question)
            return True

        if operation == "replace_sku":
            if not product:
                title = self._pending_line_title(
                    pending.model_copy(
                        update={
                            "source_line_id": line_id,
                            "scenario_type": scenario_type,
                        }
                    ),
                    session,
                    line_id,
                )
                question = f"Which Microsoft product should replace {title}?"
                progressed = bool(
                    line_id != pending.source_line_id
                    or scenario_type != pending.scenario_type
                )
                updated = pending.model_copy(
                    update={
                        "source_line_id": line_id,
                        "scenario_type": scenario_type,
                        "awaiting_slot": "product",
                        "question": question,
                        "failed_attempts": 0 if progressed else pending.failed_attempts,
                    }
                )
                if progressed:
                    await self._orchestrator.set_pending_dialogue(sender, updated)
                    await self._send_text(sender, question)
                else:
                    await self._repeat_or_release_pending(sender, updated, question)
                return True
            completed = self._agent_intent_with(
                interpreted,
                action="replace_sku",
                scenario=scenario_type.value if scenario_type else "none",
                line_id=line_id,
                product_query=product,
                quantity=quantity,
            )
            await self._execute_completed_pending(sender, pending, completed, message)
            return True

        if operation == "set_quantity":
            if quantity < 0:
                title = self._pending_line_title(pending, session, line_id)
                await self._repeat_or_release_pending(
                    sender, pending, f"What quantity should I set for {title}?"
                )
                return True
            completed = self._agent_intent_with(
                interpreted,
                action="set_quantity",
                scenario=scenario_type.value if scenario_type else "none",
                line_id=line_id,
                quantity=quantity,
            )
            await self._execute_completed_pending(sender, pending, completed, message)
            return True

        if operation == "set_copilot":
            copilot_quantity = pending.copilot_quantity
            if reply_quantity >= 0:
                copilot_quantity = reply_quantity
            elif copilot_quantity < 0:
                interpreted_copilot = getattr(interpreted, "copilot_quantity", -1)
                copilot_quantity = (
                    -1
                    if interpreted_copilot is None or interpreted_copilot == ""
                    else int(interpreted_copilot)
                )
            if copilot_quantity < 0:
                await self._repeat_or_release_pending(
                    sender,
                    pending,
                    "How many Copilot licences should the proposal include?",
                )
                return True
            completed = self._agent_intent_with(
                interpreted,
                action="set_copilot",
                scenario=scenario_type.value if scenario_type else "none",
                copilot_quantity=copilot_quantity,
            )
            await self._execute_completed_pending(sender, pending, completed, message)
            return True

        if operation == "set_disposition":
            disposition = pending.disposition
            interpreted_disposition = str(
                getattr(interpreted, "disposition", "none") or "none"
            )
            if interpreted_disposition != "none":
                disposition = interpreted_disposition  # type: ignore[assignment]
            if disposition == "none":
                normalized_disposition = " ".join(message.casefold().split())
                disposition = next(
                    (
                        value
                        for value in ("retain", "remove", "migrate", "included")
                        if re.search(rf"\b{value}\b", normalized_disposition)
                    ),
                    "none",
                )  # type: ignore[assignment]
                if disposition == "none" and re.search(
                    r"\binclude\b", normalized_disposition
                ):
                    disposition = "included"
            if disposition == "none" and re.search(
                r"\breplace\b",
                " ".join(message.casefold().split()),
            ):
                title = self._pending_line_title(pending, session, line_id)
                question = f"Which Microsoft product should replace {title}?"
                await self._orchestrator.set_pending_dialogue(
                    sender,
                    pending.model_copy(
                        update={
                            "operation": "replace_sku",
                            "awaiting_slot": "product",
                            "source_line_id": line_id,
                            "question": question,
                            "failed_attempts": 0,
                        }
                    ),
                )
                await self._send_text(sender, question)
                return True
            if disposition == "none":
                await self._repeat_or_release_pending(
                    sender,
                    pending,
                    "Should I retain, remove, migrate, or include the selected licence?",
                )
                return True
            completed = self._agent_intent_with(
                interpreted,
                action="set_disposition",
                scenario=scenario_type.value if scenario_type else "none",
                line_id=line_id,
                disposition=disposition,
            )
            await self._execute_completed_pending(sender, pending, completed, message)
            return True

        if operation == "build_scenario":
            selected = scenario_type or self._scenario_from_request(interpreted, message)
            if selected is None:
                await self._repeat_or_release_pending(sender, pending, pending.question)
                return True
            completed = self._agent_intent_with(
                interpreted,
                action="build_scenario",
                scenario=selected.value,
                quantity=pending.quantity,
                copilot_quantity=pending.copilot_quantity,
            )
            await self._execute_completed_pending(sender, pending, completed, message)
            return True

        detail_fields = {
            "set_term": "term_duration",
            "set_billing": "billing_plan",
            "set_segment": "segment",
            "set_currency": "currency",
            "add_comment": "comment",
        }
        if operation in detail_fields:
            field = detail_fields[operation]
            if self._is_non_value_slot_reply(message):
                await self._repeat_or_release_pending(sender, pending, pending.question)
                return True
            supplied = str(getattr(interpreted, field, "") or "").strip()
            detail = pending.detail_value or supplied or message.strip()
            if not detail:
                await self._repeat_or_release_pending(sender, pending, pending.question)
                return True
            completed = self._agent_intent_with(
                interpreted,
                action=operation,
                scenario=scenario_type.value if scenario_type else "none",
                **{field: detail},
            )
            await self._execute_completed_pending(sender, pending, completed, message)
            return True

        return False

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
            if "?" not in message and (reply in CANCEL_REPLIES or re.fullmatch(
                r"(?:cancel(?:\s+please)?|please\s+cancel|never\s*mind)",
                reply,
            )):
                await self._orchestrator.clear_pending_dialogue(sender)
                await self._send_text(
                    sender,
                    "Okay — I closed the resume choice. The saved draft remains unchanged. "
                    "Send *Resume* or *Start fresh* whenever you want to continue.",
                )
                return True
            if self._requests_fresh_start(message):
                await self._orchestrator.reset_session(sender)
                await self._send_text(
                    sender,
                    "Done — I cleared the previous draft. Send the first licence and quantity "
                    "when you are ready; you can provide them together or one detail at a time.",
                )
                return True
            if self._requests_resume_saved_draft(message):
                await self._resume_saved_session(sender, session)
                return True
            intent = await self._interpret_pending_message(message, session)
            if intent is not None and intent.action in CONVERSATIONAL_ACTIONS:
                await self._execute_conversational_preserving_pending(
                    sender,
                    pending,
                    intent,
                    message,
                )
                return True
            if intent is None and self._is_clear_non_requirement_turn(message):
                await self._send_non_requirement_boundary(sender)
                return True
            if intent is not None and intent.action == "capture_requirement":
                attempts = await self._orchestrator.record_pending_dialogue_failure(sender)
                if attempts >= 2:
                    await self._orchestrator.clear_pending_dialogue(sender)
                    await self._send_text(
                        sender,
                        "I closed the saved-draft choice after two unsupported replies. The "
                        "saved draft remains unchanged, and no hidden question is active. "
                        "Send *Resume* or *Start fresh* in a new message when ready.",
                    )
                    return True
                await self._send_text(
                    sender,
                    "I found an existing saved draft and will not merge a new requirement "
                    "into it without your approval. Reply *Resume* to continue it or "
                    "*Start fresh* to clear it and begin again.",
                )
                return True
            elif intent is not None and intent.action in {
                "confirm_validation",
                "reject_validation",
            }:
                # A bare approval/rejection must not mutate an unseen saved draft. Resume it
                # visibly, then let the seller validate the displayed state.
                await self._resume_saved_session(sender, session)
                return True
            elif intent is not None and intent.action not in CONVERSATIONAL_ACTIONS:
                grounded = self._intent_with_seller_supported_targets(
                    intent,
                    message,
                    session,
                    pending_slot_completion=False,
                )
                direct_edit = bool(
                    grounded.action in ASSERTIVE_COMMERCIAL_ACTIONS
                    and self._scenario_from_bare_reply(message) is None
                    and self._is_assertive_commercial_instruction(
                        message,
                        grounded,
                        session,
                        pending_slot_completion=False,
                    )
                )
                direct_compare = bool(
                    grounded.action in {"compare", "compare_enterprise_options"}
                    and self._is_comparison_request(message)
                )
                direct_finalize = bool(
                    grounded.action == "finalize"
                    and self._is_assertive_finalization_request(message)
                )
                if direct_edit or direct_compare or direct_finalize:
                    # A complete seller-grounded instruction can safely operate on the
                    # saved draft. A model-only clarification, hidden option selection, bare
                    # scenario token, or question cannot implicitly resume it.
                    await self._orchestrator.clear_pending_dialogue(sender)
                    await self._execute_agent_intent(
                        sender,
                        grounded,
                        original_message=message,
                    )
                    return True
            attempts = await self._orchestrator.record_pending_dialogue_failure(sender)
            if attempts >= 2:
                await self._orchestrator.clear_pending_dialogue(sender)
                await self._send_text(
                    sender,
                    "The saved draft remains protected and unchanged. I could not determine "
                    "whether that message meant resume or start fresh, so I closed that "
                    "choice instead of intercepting future messages. Send *Resume* or "
                    "*Start fresh* in a new message when ready.",
                )
                return True
            await self._send_text(
                sender,
                "I found an existing saved draft and will not merge a new requirement into it "
                "without your approval. Reply *Resume* to continue it or *Start fresh* to "
                "clear it and begin again.",
            )
            return True
        intent: AgentIntent | None = None
        if self._intent_interpreter is not None:
            try:
                intent = await self._intent_interpreter.interpret(
                    message,
                    session,
                )
            except IntentInterpretationError:
                logger.warning("Pending-turn intent interpretation failed")
        if intent is not None:
            # Pending context may supply the operation and target, but every value parsed
            # from this reply must still be present in the seller's words. The structured
            # resolver adds trusted pending fields only after this grounding pass.
            intent = self._intent_with_seller_supported_targets(
                intent,
                message,
                session,
                pending_slot_completion=False,
            )

        fallback_intent = self._agent_intent_with(
            intent or AgentIntent.model_construct(),
            action=(intent.action if intent is not None else "clarify"),
        )

        if pending.operation == "none":
            normalized_question = " ".join(pending.question.casefold().split())
            if all(
                marker in normalized_question
                for marker in ("quantity", "sku", "disposition")
            ):
                inferred_scope: Literal["requirement", "scenario"] = (
                    "requirement" if session.confirmed_as_is is None else "scenario"
                )
                upgraded = pending.model_copy(
                    update={
                        "operation": "choose_change",
                        "scope": inferred_scope,
                    }
                )
                inferred_line = self._pending_line_id(
                    upgraded,
                    session,
                    pending.context_message or pending.question,
                    fallback_intent,
                )
                pending = upgraded.model_copy(
                    update={"source_line_id": inferred_line}
                )
                await self._orchestrator.set_pending_dialogue(sender, pending)

        if self._requests_pending_change_cancel(message):
            await self._orchestrator.clear_pending_dialogue(sender)
            await self._send_text(
                sender,
                "Okay — I cancelled that question. The saved requirement and proposal are "
                "unchanged. What would you like to do next?",
            )
            return True

        if (
            pending.detail_value == "official_product_clarification"
            and intent is not None
            and intent.action
            in {"answer_question", "clarify", "capture_requirement", "request_recommendation"}
            and not self._looks_like_existing_requirement_operation(message)
        ):
            # This turn answers the official advisor's required follow-up. Combine it with
            # the original question so the advisor sees the complete context, then consume
            # the old question exactly once. The surrounding dual-state helper restores any
            # unrelated pending add/replace choice after this read-only turn.
            await self._orchestrator.clear_pending_dialogue(sender)
            combined_question = (
                f"{pending.context_message or pending.question}\n"
                f"Seller clarification: {message}\n"
                "Answer the Microsoft product question using this clarification. Do not "
                "change or price the requirement."
            )
            research_intent = AgentIntent.model_construct(
                detail_value=combined_question,
                product_query=str(getattr(intent, "product_query", "") or ""),
            )
            await self._send_official_product_answer(
                sender,
                research_intent,
                original_message=combined_question,
            )
            return True

        if await self._pending_reply_is_compatible(
            pending,
            session,
            message,
            fallback_intent,
        ) and await self._resolve_structured_pending_dialogue(
                sender,
                message,
                session,
                pending,
                fallback_intent,
            ):
            return True

        if pending.operation == "none" and reply in AFFIRMATIVE_REPLIES:
            # Let the model resolve a safe, read-only follow-up such as "Would you like
            # guidance?" -> "Yes". Never let a bare approval mutate pricing, validate an
            # estate, or finalize a proposal when the preceding question had no structured
            # operation attached to it.
            if intent is not None and intent.action in (
                CONVERSATIONAL_ACTIONS | {"clarify", "request_recommendation"}
            ):
                await self._orchestrator.clear_pending_dialogue(sender)
                await self._execute_agent_intent(
                    sender,
                    intent,
                    original_message=message,
                )
                return True
            # An unstructured missing-detail question cannot safely interpret bare approval.
            # Close it once rather than looping or treating it as estate approval.
            await self._release_pending_dialogue(
                sender,
                session,
                confirmation_blocked=True,
            )
            return True

        if intent is None:
            if self._is_clear_non_requirement_turn(message):
                await self._send_non_requirement_boundary(sender)
                return True
            await self._repeat_or_release_pending(sender, pending, pending.question)
            return True

        if (
            session.confirmed_as_is is None
            and self._pending_is_recommendation_guidance(pending)
            and intent.action
            in {"request_recommendation", "answer_question", "clarify", "capture_requirement"}
            and not self._looks_like_existing_requirement_operation(message)
        ):
            # This is an answer to a read-only recommendation question, not a new
            # requirement merely because it contains a user count (for example
            # "endpoint protection for 100 users"). An explicit add/change/remove
            # instruction still supersedes the guidance question through the operation
            # check above.
            await self._orchestrator.clear_pending_dialogue(sender)
            combined_question = (
                f"{pending.context_message or pending.question}\n"
                f"Seller clarification: {message}\n"
                "Give read-only product guidance for the current draft. Do not change or "
                "price the requirement."
            )
            research_intent = AgentIntent.model_construct(
                detail_value=combined_question,
                product_query="",
            )
            await self._send_official_product_answer(
                sender,
                research_intent,
                original_message=combined_question,
            )
            return True

        if intent.action == "confirm_validation":
            if (
                session.stage == WorkflowStage.AWAITING_INITIAL_VALIDATION
                and session.estate is not None
                and not session.estate.pending_lines
                and not session.capture_messages
                and self._is_requirement_confirmation_reply(message)
                and (
                    self._pending_confirms_complete_requirement(pending)
                    or self._is_explicit_requirement_confirmation(message)
                )
            ):
                await self._orchestrator.clear_pending_dialogue(sender)
                await self._confirm_validation(sender)
                return True
            await self._release_pending_dialogue(
                sender,
                session,
                confirmation_blocked=True,
            )
            return True
        if (
            session.confirmed_as_is is None
            and intent.action == "clarify"
            and not self._looks_like_existing_requirement_operation(message)
            and await self._is_catalog_backed_requirement_statement(message)
        ):
            # An unstructured review question must not swallow an obvious new catalogue
            # line merely because the model returned a generic clarification. Catalogue
            # evidence plus requirement wording is the deterministic capture boundary.
            await self._orchestrator.clear_pending_dialogue(sender)
            await self._capture_typed_requirement(sender, message)
            return True
        if intent.action in CONVERSATIONAL_ACTIONS:
            if intent.action == "out_of_scope":
                # An unrelated new subject must not leave a latent edit armed. Answer it and
                # clear the pending operation so a later number or product cannot be consumed
                # by stale context.
                await self._orchestrator.clear_pending_dialogue(sender)
                await self._execute_agent_intent(
                    sender,
                    intent,
                    original_message=message,
                )
                return True
            # Relevant informational interruptions can be answered while retaining the
            # unfinished commercial action; no old prompt is replayed in the same turn.
            await self._execute_conversational_preserving_pending(
                sender,
                pending,
                intent,
                message,
            )
            return True
        if intent.action == "clarify":
            next_question = self._professional_agent_text(intent.clarification)
            normalized_pending = " ".join(pending.question.casefold().split())
            normalized_next = " ".join(next_question.casefold().split())
            if normalized_next and (
                normalized_next == normalized_pending
                or self._questions_semantically_equivalent(
                    pending.question,
                    next_question,
                )
            ):
                await self._repeat_or_release_pending(
                    sender,
                    pending,
                    next_question,
                )
                return True
        if intent.action in ASSERTIVE_COMMERCIAL_ACTIONS and not self._is_assertive_commercial_instruction(
            message,
            intent,
            session,
            pending_slot_completion=False,
        ):
            # A model can label a question, historical statement, or negated sentence as
            # an edit. Preserve the visible unfinished question and answer safely instead
            # of clearing its context before the central mutation guard rejects it.
            await self._execute_conversational_preserving_pending(
                sender,
                pending,
                intent,
                message,
            )
            return True
        await self._orchestrator.clear_pending_dialogue(sender)
        if intent.action == "capture_requirement":
            # The pending context can be an earlier information question or abandoned
            # clarification. Requirement fragments are already persisted separately by
            # the capture workflow, so merging this text risks inventing an SKU.
            await self._capture_typed_requirement(sender, message)
            return True
        await self._execute_agent_intent(
            sender,
            intent,
            original_message=message,
        )
        return True

    async def _release_pending_dialogue(
        self,
        sender: str,
        session: WorkflowSession,
        *,
        confirmation_blocked: bool = False,
    ) -> None:
        """Release an unanswered question instead of replaying it on every turn."""

        await self._orchestrator.clear_pending_dialogue(sender)
        if confirmation_blocked and session.estate is not None and session.estate.pending_lines:
            products = "; ".join(
                line.source_product_title for line in session.estate.pending_lines[:6]
            )
            await self._send_text(
                sender,
                "Pricing is still paused because the exact product is unresolved for "
                f"{products}. I have stopped repeating the earlier question and made no "
                "selection. Choose a displayed SKU, send its full product name, or continue "
                "with another licensing question.",
            )
            return
        if session.capture_messages:
            await self._send_text(
                sender,
                "I could not safely use that reply to complete the unfinished licence entry. "
                "I have kept the details already supplied and stopped repeating the earlier "
                "question. Send the missing product or quantity in one message when ready.",
            )
            return
        await self._send_text(
            sender,
            "I could not safely connect that reply to the earlier question, so I have closed "
            "that prompt without changing the requirement. State the next licensing request "
            "naturally whenever you are ready.",
        )

    @classmethod
    def _is_assertive_validation_reply(
        cls,
        message: str,
        *,
        allow_bare_affirmative: bool,
    ) -> bool:
        """Accept approval assertions while rejecting questions, negation, and hypotheticals."""

        reply = " ".join(
            message.casefold().replace("\u2019", "'").strip(" ?!.,").split()
        )
        if not reply:
            return False
        if "?" in message or re.match(
            r"^(?:who|what|which|where|when|why|how|can|could|would|should|"
            r"do|does|did|is|are|will|may)\b",
            reply,
        ):
            return False
        if re.match(r"^(?:if|unless|when|whether|suppose|assuming)\b", reply):
            return False
        if cls._is_negated_action(
            reply,
            r"(?:confirm|approve|proceed|price|finali[sz]e)",
        ) or re.search(
            r"\b(?:not|never|no)\s+(?:yet\s+)?(?:confirmed|approved|correct|finalized|finalised)\b",
            reply,
        ):
            return False
        if allow_bare_affirmative and reply in AFFIRMATIVE_REPLIES:
            return True
        if reply in REQUIREMENT_CONFIRMATION_REPLIES:
            return True
        return bool(
            re.fullmatch(
                r"(?:(?:yes|yeah|ok|okay)[, ]+)?"
                r"(?:(?:i|we)\s+(?:hereby\s+)?(?:confirm|approve)(?:\s+(?:it|this|"
                r"the\s+(?:complete\s+)?(?:requirement|details|configuration|"
                r"proposal|pricing)(?:\s+for\s+pricing)?|"
                r"the\s+analysis(?:\s+and\s+pricing)?|all\s+details))?(?:\s+now)?|"
                r"(?:please\s+)?(?:confirm|approve)\s+(?:the\s+)?(?:complete\s+)?"
                r"(?:requirement|details|configuration|list)(?:\s+and\s+"
                r"(?:calculate|show|prepare)\s+(?:the\s+)?renew(?:al)?\s+as[ -]?is"
                r"(?:\s+(?:annual\s+)?(?:price|cost|proposal))?)?|"
                r"(?:please\s+)?(?:confirm|approve)(?:\s+(?:it|this|the\s+requirement|"
                r"the\s+details|for\s+pricing))?|"
                r"(?:please\s+)?proceed(?:\s+with)?\s+pricing|"
                r"(?:the\s+)?(?:requirement|details|configuration|list)\s+(?:is|are)\s+correct|"
                r"(?:(?:yes|yeah|ok|okay)\s+)?finali[sz]e(?:\s+(?:it|this|the\s+proposal))?)",
                reply,
            )
        )

    @classmethod
    def _is_requirement_confirmation_reply(cls, message: str) -> bool:
        return cls._is_assertive_validation_reply(
            message,
            allow_bare_affirmative=True,
        )

    @classmethod
    def _is_explicit_requirement_confirmation(cls, message: str) -> bool:
        """Distinguish an explicit approval from a bare answer such as 'yes'."""

        return cls._is_assertive_validation_reply(
            message,
            allow_bare_affirmative=False,
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

    @staticmethod
    def _pending_is_recommendation_guidance(pending: PendingDialogue) -> bool:
        context = " ".join(
            (pending.context_message + " " + pending.question).casefold().split()
        )
        return any(
            marker in context
            for marker in {
                "recommend",
                "suggest",
                "business capability",
                "user group",
                "licence support",
                "license support",
                "which capability",
            }
        )

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

        # A newer incomplete addition is the active conversational question. Let its
        # product/quantity capture finish before returning to older unresolved catalogue
        # lines; otherwise a bare quantity is consumed by the old match gate.
        if session.capture_messages:
            return False

        remove_line = re.fullmatch(
            r"(?:please\s+)?remove(?:\s+that)?\s+(l\d+)(?:\s+.*)?",
            reply,
        )
        if remove_line is not None:
            await self._remove_requirement_line(sender, remove_line.group(1))
            return True
        if reply in UNCERTAIN_REPLIES:
            await self._pause_pending_requirement_match(sender, pending_lines)
            return True
        if "?" in message and re.fullmatch(
            r"(?:cancel|cancel\s+(?:this|that|the\s+question)|never\s*mind)",
            reply,
        ):
            await self._send_text(
                sender,
                "That question did not cancel or pause the unresolved SKU selection. If you "
                "want to close it, say *Cancel the product selection* without a question "
                "mark; otherwise choose a displayed option or send the complete product name.",
            )
            return True
        if self._requests_pending_change_cancel(message):
            await self._orchestrator.set_pending_match_prompt_suspended(sender, True)
            await self._orchestrator.clear_pending_dialogue(sender)
            await self._send_text(
                sender,
                "Okay — I cancelled the product-selection prompt. No licence was removed or "
                "priced. The unresolved product remains in the draft until you choose an "
                "exact SKU, provide its complete product name, or explicitly remove that "
                "licence.",
            )
            return True

        selectable_lines = [
            line
            for line in pending_lines
            if not line.candidate_narrowing_required
        ]
        line_id, candidate_number = self._candidate_selection_from_reply(
            message,
            selectable_lines,
        )
        selected_line = next(
            (line for line in selectable_lines if line.line_id == line_id),
            None,
        )
        if (
            selected_line is not None
            and candidate_number is not None
            and 1 <= candidate_number <= len(selected_line.candidates)
        ):
            estate = await self._confirm_requirement_candidate(
                sender,
                capture_token=session.estate.capture_token[:16],
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
                (
                    normalized_message == normalize_product_title(candidate.sku_title)
                    and self._is_direct_candidate_title_reply(message)
                )
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
                capture_token=session.estate.capture_token[:16],
                line_id=line.line_id,
                candidate_number=index,
                allow_hidden_exact_title=True,
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
                capture_token=session.estate.capture_token[:16],
                line_id=pending_lines[0].line_id,
                candidate_number=1,
            )
            await self._after_requirement_match_confirmation(sender, estate)
            return True

        if session.pending_match_prompt_suspended and (
            re.search(
                r"\b(?:show|display|list|repeat|reopen|restore|view)\b",
                reply,
            )
            and re.search(
                r"\b(?:options?|choices?|matches?|products?|skus?|l\d+)\b",
                reply,
            )
        ):
            await self._send_pending_match_requests(sender, session.estate)
            return True
        if session.pending_match_prompt_suspended:
            # Explicit option and exact-title replies above can still resolve the line.
            # Everything else returns to normal intent routing instead of replaying the
            # catalogue gate.
            return False

        intent = await self._interpret_pending_message(message, session)
        if intent is not None:
            intent = self._intent_with_seller_supported_targets(
                intent,
                message,
                session,
                pending_slot_completion=False,
            )
        if intent is not None:
            if intent.action in CONVERSATIONAL_ACTIONS | {"reset_requirement"}:
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
            } or await self._is_catalog_backed_requirement_statement(message):
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
                if intent.action == "confirm_matches":
                    # Any direct numbered/title selection was handled above. A model label
                    # alone must not confirm a catalogue option from a question, negation,
                    # reported statement, or other narrative text.
                    await self._send_text(
                        sender,
                        "I did not treat that message as a direct product selection, so the "
                        "unresolved line is unchanged. Reply with a displayed option number "
                        "or send the complete product name when you want to confirm it.",
                    )
                    return True
                await self._execute_agent_intent(
                    sender,
                    intent,
                    original_message=message,
                )
                return True

        if intent is None and self._is_clear_non_requirement_turn(message):
            await self._send_non_requirement_boundary(sender)
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
                await self._pause_pending_requirement_match(sender, pending_lines)
                return True
        await self._pause_pending_requirement_match(sender, pending_lines)
        return True

    async def _pause_pending_requirement_match(self, sender: str, pending_lines: list) -> None:
        """Keep unresolved data safe while preventing a catalogue-choice loop."""

        await self._orchestrator.set_pending_match_prompt_suspended(sender, True)
        broad = [line for line in pending_lines if line.candidate_narrowing_required]
        if broad:
            questions = [
                candidate_narrowing_question(
                    line.source_product_title,
                    len(line.candidates),
                )
                for line in broad[:3]
            ]
            await self._send_text_chunks(
                sender,
                "\n\n".join(questions)
                + "\n\nAll captured quantities and source lines remain unchanged, and "
                "pricing is paused until the exact SKU is confirmed.",
            )
            return
        products = "; ".join(line.source_product_title for line in pending_lines[:6])
        await self._send_text(
            sender,
            "No product was selected, and I have paused the repeated SKU prompt. The "
            f"unresolved product(s) {products} remain unchanged and will not be priced until an "
            "exact match is confirmed. You can still choose a displayed option, send the "
            "full product name, remove the line, or continue with another licensing question.",
        )

    async def _repeat_or_close_pending_sku_change(
        self,
        sender: str,
        message: str,
    ) -> None:
        """Repeat a SKU choice once, then release it without applying anything."""

        attempts = await self._orchestrator.record_pending_sku_change_failure(sender)
        if attempts >= 2:
            await self._orchestrator.cancel_sku_change(sender)
            await self._send_text(
                sender,
                "I closed the earlier product choice after two unsupported replies so it "
                "will not keep intercepting the conversation. No SKU or commercial value "
                "was changed. Restate the product change when you are ready.",
            )
            return
        await self._send_text(sender, message)

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
        if self._requests_pending_change_cancel(message):
            await self._orchestrator.cancel_sku_change(sender)
            await self._send_text(
                sender,
                "Okay - I cancelled that product change. The proposal is unchanged, and "
                "the repeated choice has been closed.",
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
            await self._orchestrator.cancel_sku_change(sender)
            await self._send_text(
                sender,
                "No product change was applied. I closed the pending choice instead of "
                "repeating it. Send the invoice product name, a screenshot, or the business "
                "capability you need, and I will help narrow the SKU down safely.",
            )
            return True

        number = None if "?" in message else self._single_candidate_number(message)
        if number is None and re.fullmatch(
            r"(?:(?:choose|select|use|pick)\s+)?(?:the\s+)?last"
            r"(?:\s+(?:one|option))?",
            reply,
        ):
            number = len(pending.candidates)
        normalized_message = normalize_product_title(message)
        title_numbers = [
            index
            for index, candidate in enumerate(pending.candidates, start=1)
            if (
                (
                    normalized_message == normalize_product_title(candidate.sku_title)
                    and self._is_direct_candidate_title_reply(message)
                )
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
        elif pending.candidate_narrowing_required:
            # Numbered options were intentionally not displayed for this broad query.
            # Never let a guessed or stale option number select from the hidden set.
            number = None
        if number is None or number < 1 or number > len(pending.candidates):
            intent = await self._interpret_pending_message(message, session)
            if intent is not None:
                intent = self._intent_with_seller_supported_targets(
                    intent,
                    message,
                    session,
                    pending_slot_completion=False,
                )
            if intent is not None:
                if intent.action in CONVERSATIONAL_ACTIONS | {"reset_requirement"}:
                    if intent.action in {"acknowledge", "out_of_scope"}:
                        # A conversational close or a clearly unrelated new subject ends the
                        # unconfirmed choice. This prevents the next short reply from being
                        # applied to stale catalogue options the seller is no longer discussing.
                        await self._orchestrator.cancel_sku_change(sender)
                    if intent.action in {"help", "answer_question"}:
                        try:
                            await self._execute_agent_intent(
                                sender,
                                intent,
                                original_message=message,
                            )
                        finally:
                            latest = await self._orchestrator.get_session(sender)
                            preserve_dialogue = bool(
                                intent.action == "answer_question"
                                and str(
                                    getattr(intent, "detail_label", "")
                                ).strip().casefold()
                                == "official_product_question"
                                and latest is not None
                                and latest.pending_dialogue is not None
                                and latest.pending_dialogue.detail_value
                                == "official_product_clarification"
                            )
                            await self._orchestrator.restore_pending_sku_change(
                                sender,
                                pending,
                                preserve_pending_dialogue=preserve_dialogue,
                            )
                    else:
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
                } or (
                    intent.action == "clarify"
                    and await self._is_catalog_backed_requirement_statement(message)
                ):
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
                elif intent.action == "confirm_sku":
                    if pending.candidate_narrowing_required:
                        await self._repeat_or_close_pending_sku_change(
                            sender,
                            candidate_narrowing_question(
                                pending.product_query,
                                len(pending.candidates),
                            )
                            + " No product was selected and nothing was changed. Restate the "
                            "change with a more specific product name.",
                        )
                        return True
                    explicit_candidate = self._single_candidate_number(message)
                    if (
                        explicit_candidate is None
                        or explicit_candidate != intent.candidate_number
                        or not 1 <= intent.candidate_number <= len(pending.candidates)
                    ):
                        await self._repeat_or_close_pending_sku_change(
                            sender,
                            "Please choose one of the displayed product options, or send the "
                            "complete Microsoft product name. Nothing has been changed.",
                        )
                        return True
                    result = await self._orchestrator.confirm_sku_change(
                        sender,
                        intent.candidate_number,
                    )
                    await self._send_sku_change_result(sender, result)
                    return True
                elif intent.action == "cancel_sku":
                    if not self._requests_pending_change_cancel(message):
                        await self._send_text(
                            sender,
                            "I have not cancelled the pending product change. The proposal "
                            "and the displayed choice remain unchanged.",
                        )
                        return True
                    await self._orchestrator.cancel_sku_change(sender)
                    await self._send_text(
                        sender,
                        "Okay — I cancelled that product change. The proposal remains "
                        "unchanged.",
                    )
                    return True
                elif intent.action != "clarify":
                    await self._orchestrator.cancel_sku_change(sender)
                    await self._execute_agent_intent(
                        sender,
                        intent,
                        original_message=message,
                    )
                    return True
            if intent is None and self._is_clear_non_requirement_turn(message):
                # A new question supersedes an uncommitted proposal change. This avoids
                # repeatedly showing an old choice when intent interpretation is down.
                await self._orchestrator.cancel_sku_change(sender)
                await self._send_non_requirement_boundary(sender)
                return True
            referenced_candidate = any(
                normalize_product_title(candidate.sku_title) in normalized_message
                for candidate in pending.candidates
                if normalize_product_title(candidate.sku_title)
            )
            if referenced_candidate:
                await self._repeat_or_close_pending_sku_change(
                    sender,
                    "I did not treat that sentence as your direct product selection, so no "
                    "SKU was changed. Choose a displayed option directly, or send the complete "
                    "product name you want.",
                )
                return True
            if pending.candidate_narrowing_required:
                await self._repeat_or_close_pending_sku_change(
                    sender,
                    candidate_narrowing_question(
                        pending.product_query,
                        len(pending.candidates),
                    )
                    + " Nothing has been changed. Restate the change with a more specific "
                    "product name when ready.",
                )
                return True
            # Never turn a plan number into an option number, and never trap the seller by
            # replaying the same candidates. No proposal mutation has occurred yet, so the
            # unconfirmed edit is safe to cancel.
            await self._repeat_or_close_pending_sku_change(
                sender,
                "I did not apply the pending product change because the reply did not "
                "identify one of the displayed SKUs. The proposal is unchanged. Choose one "
                "of the displayed options, send the complete product name, or ask me to help "
                "choose a suitable SKU.",
            )
            return True
        result = await self._orchestrator.confirm_sku_change(sender, number)
        await self._send_sku_change_result(sender, result)
        return True

    @staticmethod
    def _single_candidate_number(message: str) -> int | None:
        reply = " ".join(message.casefold().strip(" ?!.,").split())
        match = re.fullmatch(
            r"(?:(?:(?:i|we)\s+)?(?:choose|select|use|pick|want|prefer|take)\s+"
            r"(?:the\s+)?|go\s+with\s+(?:the\s+)?)?"
            r"(?:option\s+|number\s+)?(\d+)",
            reply,
        )
        if match:
            return int(match.group(1))
        ordinal_match = re.fullmatch(
            r"(?:(?:(?:i|we)\s+)?(?:choose|select|use|pick|want|prefer|take)\s+"
            r"(?:the\s+)?|go\s+with\s+(?:the\s+)?)?"
            r"(?:option\s+)?(?:the\s+)?"
            r"(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)"
            r"(?:\s+(?:one|option))?",
            reply,
        )
        if ordinal_match is None:
            return None
        return {
            "first": 1,
            "second": 2,
            "third": 3,
            "fourth": 4,
            "fifth": 5,
            "sixth": 6,
            "seventh": 7,
            "eighth": 8,
            "ninth": 9,
            "tenth": 10,
        }[ordinal_match.group(1)]

    def _candidate_selection_from_reply(
        self,
        message: str,
        pending_lines: list,
    ) -> tuple[str | None, int | None]:
        if "?" in message:
            return None, None
        reply = " ".join(message.casefold().strip(" ?!.,").split())
        selection = re.fullmatch(
            r"(?:(?:(?:i|we)\s+)?(?:choose|select|use|pick|want|prefer|take)\s+"
            r"(?:the\s+)?|go\s+with\s+(?:the\s+)?)?"
            r"(?:(l\d+)\s+)?(?:option\s+|number\s+)?(\d+)"
            r"(?:\s+(?:for\s+)?(l\d+))?",
            reply,
        )
        line_id = None
        number = None
        line_match = None
        if selection is not None:
            line_value = selection.group(1) or selection.group(3)
            line_id = line_value.upper() if line_value else None
            number = int(selection.group(2))
        if number is None:
            bare = self._single_candidate_number(message)
            if (
                bare is not None
                and len(pending_lines) == 1
                and bare <= len(pending_lines[0].candidates)
            ):
                number = bare
        if (
            number is None
            and len(pending_lines) == 1
            and re.fullmatch(
                r"(?:(?:choose|select|use|pick)\s+)?(?:the\s+)?last"
                r"(?:\s+(?:one|option))?",
                reply,
            )
        ):
            number = len(pending_lines[0].candidates)
        if line_id is None and len(pending_lines) == 1:
            line_id = pending_lines[0].line_id
        return line_id, number

    @classmethod
    def _candidate_selection_is_seller_grounded(
        cls,
        message: str,
        *,
        line_id: str,
        candidate_number: int,
        single_pending_line: bool,
    ) -> bool:
        """Verify that a structured candidate choice is present in the seller's words."""

        if candidate_number <= 0 or "?" in message:
            return False
        normalized = " ".join(
            message.casefold().replace("\u2019", "'").strip(" ?!.,").split()
        )
        if not normalized:
            return False
        if cls._is_negated_action(
            normalized,
            r"(?:choose|select|use|pick|confirm)",
        ) or re.search(
            r"\b(?:customer|client|seller|user|manager|analyst)\b.{0,48}"
            r"\b(?:said|says|asked|asks|selected|chose|requested|requests?)\b",
            normalized,
        ):
            return False
        if re.search(
            r"\b(?:not|except|other\s+than)\s+(?:the\s+)?(?:option\s+|number\s+)?"
            r"\d+\b|\b(?:was|is|has\s+been)\s+(?:selected|chosen|picked)\s+by\s+"
            r"(?:the\s+)?(?:customer|client|seller|user|manager|analyst)\b",
            normalized,
        ):
            return False
        if single_pending_line and cls._single_candidate_number(message) == candidate_number:
            return True
        line = re.escape(line_id.casefold())
        number = re.escape(str(candidate_number))
        return bool(
            re.search(
                rf"\b{line}\b.{{0,24}}\b(?:option\s+|number\s+)?{number}\b",
                normalized,
            )
            or re.search(
                rf"\b(?:option\s+|number\s+)?{number}\b.{{0,24}}"
                rf"\b(?:for\s+)?{line}\b",
                normalized,
            )
        )

    async def _confirm_requirement_candidate(
        self,
        sender: str,
        *,
        capture_token: str,
        line_id: str,
        candidate_number: int,
        allow_hidden_exact_title: bool = False,
    ) -> LicenseEstate:
        session = await self._orchestrator.get_session(sender)
        if session is None or session.estate is None:
            raise ValueError("There is no requirement awaiting product confirmation.")
        if capture_token != session.estate.capture_token[:16]:
            raise ValueError(
                "That product-selection menu belongs to an earlier requirement and is no "
                "longer active. Review the latest requirement and use its current choices."
            )
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
        if line.candidate_narrowing_required and not allow_hidden_exact_title:
            raise ValueError(
                "That product request is still too broad for numbered selection. Add a "
                "product family, workload, edition, plan, or exact catalogue ID first."
            )
        index = candidate_number - 1
        if index < 0 or index >= len(line.candidates):
            raise ValueError(
                f"Choose an option from 1 to {len(line.candidates)} for {line.line_id}."
            )
        candidate = line.candidates[index]
        return await self._orchestrator.confirm_matches(
            sender,
            {
                line.line_id: (
                    candidate.product_id,
                    candidate.sku_id,
                    candidate.sku_title,
                )
            },
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
        await self._orchestrator.set_pending_match_prompt_suspended(sender, False)
        await self._send_text_chunks(sender, format_pending_matches(estate))
        await self._send_pending_match_lists(
            sender,
            estate.pending_lines,
            capture_token=estate.capture_token,
        )

    async def _send_pending_match_lists(
        self,
        sender: str,
        pending_lines: list,
        *,
        capture_token: str | None = None,
    ) -> None:
        token = (capture_token or "legacy")[:16]
        for line in pending_lines:
            if line.candidate_narrowing_required:
                continue
            total = len(line.candidates)
            for offset in range(0, total, 10):
                page = line.candidates[offset : offset + 10]
                rows = [
                    InteractiveRow(
                        id=(
                            f"licensing|match_confirm|{token}|{line.line_id}|"
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
                                f"Choose {line.line_id} from options {first}-{last} of {total}. "
                                "The complete product names and IDs are shown above."
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

    async def _try_handle_bulk_requirement_removal(
        self,
        sender: str,
        message: str,
        session: WorkflowSession,
    ) -> bool:
        """Apply an explicit multi-line removal once, against the original line IDs."""

        if (
            session.estate is None
            or session.confirmed_as_is is not None
            or session.stage
            not in {
                WorkflowStage.AWAITING_MATCH_CONFIRMATION,
                WorkflowStage.AWAITING_INITIAL_VALIDATION,
                WorkflowStage.AWAITING_SCENARIO,
            }
        ):
            return False
        normalized = " ".join(message.casefold().strip(" ?!.,").split())
        removal_requested = bool(
            re.search(r"\b(?:remove|removed|delete|drop)\b", normalized)
            or normalized in {"both", "remove both", "delete both"}
        )
        if not removal_requested:
            return False
        direct_bulk_removal = normalized == "both" or self._is_assertive_action_request(
            message,
            r"(?:remove|delete|drop)(?:\s+.+)",
        )
        if not direct_bulk_removal:
            return False
        ids = re.findall(r"\bl\d+\b", normalized)
        if len(set(ids)) < 2 and session.pending_dialogue is not None:
            pending_text = (
                session.pending_dialogue.context_message
                + " "
                + session.pending_dialogue.question
            ).casefold()
            ids.extend(re.findall(r"\bl\d+\b", pending_text))
        normalized_ids = list(dict.fromkeys(value.upper() for value in ids))
        if len(normalized_ids) < 2:
            return False
        current_ids = {line.line_id for line in session.estate.lines}
        unknown_ids = [line_id for line_id in normalized_ids if line_id not in current_ids]
        if unknown_ids:
            await self._send_text(
                sender,
                "I did not remove anything because "
                + ", ".join(unknown_ids)
                + " is not in the current requirement. Name the products to remove or use "
                "the current line labels shown in the latest table.",
            )
            return True
        if set(normalized_ids) == current_ids:
            await self._orchestrator.reset_session(sender)
            await self._send_text(
                sender,
                "Removed all selected lines. The requirement is now empty; send the first "
                "licence and quantity when you are ready.",
            )
            return True
        estate = await self._orchestrator.remove_requirement_lines(
            sender,
            normalized_ids,
            expected_pending_dialogue=session.pending_dialogue,
        )
        await self._send_text(
            sender,
            "Removed " + ", ".join(normalized_ids) + " from the requirement.",
        )
        await self._send_updated_requirement(sender, estate)
        return True

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
            if "images" in locals():
                raise
            logger.exception("Unable to render estate table image; using text fallback")
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
        is_intro_request = lowered in {"/help", "/start", "/about", "/analyze"} or intro_request in {
            "hi",
            "hello",
            "hey",
            "help",
            "start",
            "starting",
            "begin",
            "confusing",
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
        }
        if is_intro_request:
            # A greeting or capability question is still a complete conversational turn.
            # Route it through an active structured dialogue first so the answer is given
            # without losing (or silently replacing) the commercial question in progress.
            if (
                session is not None
                and session.pending_dialogue is not None
                and await self._resolve_pending_dialogue(sender, command, session)
            ):
                return
            saved_session_reentry = lowered == "/start" or intro_request in {
                "hi",
                "hello",
                "hey",
                "start",
                "good morning",
                "good afternoon",
                "good evening",
            }
            if (
                saved_session_reentry
                and self._configuration.workflow_mode == "simple_pricing"
                and session is not None
                and session.estate is not None
            ):
                # This explicit re-entry path both installs and visibly renders the choice.
                # Capability/help answers remain read-only and never create hidden state.
                await self._show_saved_session_choice(sender, session)
                return
            if self._intent_interpreter is not None:
                try:
                    intent = await self._intent_interpreter.interpret(command, session)
                except IntentInterpretationError:
                    logger.warning("Dynamic introduction interpretation failed")
                else:
                    await self._execute_agent_intent(
                        sender,
                        intent,
                        original_message=command,
                    )
                    return
            # Emergency fallback for environments where the language service is disabled
            # or temporarily unavailable. The normal production path above is model-driven.
            await self._send_text(sender, HELP_TEXT)
            return
        if self._requests_fresh_start(command):
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
            and self._requests_restricted_pricing(command)
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
        if (
            self._configuration.workflow_mode == "simple_pricing"
            and session is not None
            and session.pending_sku_change is not None
            and session.pending_dialogue is None
            and self._requests_pending_change_cancel(command)
            and await self._try_handle_pending_sku_change_reply(
                sender,
                command,
                session,
            )
        ):
            # A combined correction such as "don't replace it; compare instead" first
            # cancels the uncommitted choice. The global multi-action guard below must not
            # leave that stale replacement armed.
            return
        if not lowered.startswith("/") and self._contains_multiple_mutation_clauses(command):
            unconfirmed = session is None or session.confirmed_as_is is None
            initial_multi_add = unconfirmed and bool(
                re.match(r"^(?:please\s+)?(?:add|include)\b", intro_request)
            )
            draft_bulk_removal = bool(
                unconfirmed
                and session is not None
                and session.estate is not None
                and re.search(r"\b(?:remove|delete|drop)\b", intro_request)
                and len(set(re.findall(r"\bl\d+\b", intro_request))) >= 2
            )
            if initial_multi_add or draft_bulk_removal:
                # Initial capture supports multiple extracted lines, and the draft-only
                # multi-line removal path applies explicit line IDs atomically. The same
                # grammar is rejected once a commercial baseline exists because proposal
                # intents represent only one mutation.
                pass
            else:
                await self._send_text(
                    sender,
                    "I found more than one independent licensing change in that message and "
                    "did not apply only part of it. Send one product change at a time; I will "
                    "show the updated requirement or proposal before you send the next change.",
                )
                return
        session = await self._orchestrator.get_session(sender)
        if (
            self._configuration.workflow_mode != "simple_pricing"
            and session is not None
            and session.pending_dialogue is not None
            and await self._resolve_pending_dialogue(sender, command, session)
        ):
            # Structured slot completion is a conversation-state invariant, not a
            # simple-pricing feature. A visible quantity/product question must consume the
            # seller's answer consistently in every supported workflow profile.
            return
        if self._configuration.workflow_mode == "simple_pricing" and session is not None:
            if (
                session.pending_dialogue is not None
                and session.pending_dialogue.kind == "resume_session"
                and await self._resolve_pending_dialogue(sender, command, session)
            ):
                return
            if (
                session.pending_dialogue is not None
                and self._requests_pending_change_cancel(command)
                and not (
                    session.pending_sku_change is not None
                    and session.pending_dialogue.detail_value
                    == "official_product_clarification"
                )
                and await self._resolve_pending_dialogue(sender, command, session)
            ):
                return
            if await self._try_handle_bulk_requirement_removal(
                sender,
                command,
                session,
            ):
                return
            if (
                session.pending_sku_change is not None
                and session.pending_dialogue is not None
                and await self._resolve_pending_dialogue_preserving_sku(
                    sender,
                    command,
                    session,
                )
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
                and self._requests_pending_change_cancel(command)
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
                not lowered.startswith("/")
                and await self._has_open_requirement_draft(sender)
                and self._intent_interpreter is None
                and await self._is_catalog_backed_requirement_statement(command)
            ):
                # The seller is still assembling the unconfirmed requirement. A new
                # catalogue-backed product such as "add ME3" is another requirement line,
                # not a request to build or edit a priced scenario. This seller-grounded
                # boundary deliberately outranks any model-supplied scenario/edit label.
                await self._capture_typed_requirement(sender, command)
                return
            if (
                session.pending_dialogue is not None
                and (
                    session.pending_dialogue.operation != "none"
                    or session.pending_dialogue.awaiting_slot != "none"
                )
                and await self._resolve_pending_dialogue(sender, command, session)
            ):
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
                    or self._is_explicit_requirement_confirmation(command)
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
            not lowered.startswith("/")
            and session is not None
            and session.confirmed_as_is is not None
            and session.pending_dialogue is None
            and session.pending_sku_change is None
            and session.stage
            in {WorkflowStage.AWAITING_SCENARIO, WorkflowStage.REVIEWING_SCENARIO}
        ):
            bare_scenario = self._scenario_from_bare_reply(command)
            if bare_scenario is not None:
                scenario = await self._orchestrator.build_scenario(sender, bare_scenario)
                if self._configuration.workflow_mode == "simple_pricing":
                    await self._send_simple_revised(sender, scenario)
                else:
                    await self._send_scenario(sender, scenario)
                return
        if (
            self._configuration.workflow_mode == "simple_pricing"
            and not lowered.startswith("/")
        ):
            if session is None or session.estate is None:
                if self._intent_interpreter is not None:
                    try:
                        intent = await self._intent_interpreter.interpret(command, session)
                    except IntentInterpretationError:
                        logger.warning("Pre-upload intent interpretation failed")
                        if not await self._is_catalog_backed_requirement_statement(command):
                            await self._send_text(
                                sender,
                                "I could not determine whether that is a licensing requirement "
                                "or a question. Are you providing SKUs and quantities, or asking "
                                "about the process?",
                            )
                            return
                        intent = AgentIntent.model_construct(action="capture_requirement")
                    if intent.action != "capture_requirement":
                        if await self._is_catalog_backed_requirement_statement(command):
                            await self._capture_typed_requirement(sender, command)
                            return
                        await self._execute_agent_intent(
                            sender,
                            intent,
                            original_message=command,
                        )
                        return
                    if self._is_clear_non_requirement_turn(command):
                        await self._send_non_requirement_boundary(sender)
                        return
                elif not (
                    self._looks_like_requirement_fragment(command)
                    and not self._is_licensing_question_message(command)
                    and not re.match(
                        r"^(?:who|what|which|where|when|why|how|did|does|is|are|"
                        r"can|could|would)\b",
                        intro_request,
                    )
                ):
                    await self._send_text(
                        sender,
                        "Tell me the Microsoft product and quantity you want to review, "
                        "or ask a licensing question.",
                    )
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
            await self._request_finalization(sender, seller_message=command)
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
                if (
                    self._configuration.workflow_mode == "simple_pricing"
                    and session is not None
                    and session.stage
                    in {
                        WorkflowStage.AWAITING_MATCH_CONFIRMATION,
                        WorkflowStage.AWAITING_INITIAL_VALIDATION,
                    }
                    and await self._is_catalog_backed_requirement_statement(command)
                ):
                    await self._capture_typed_requirement(sender, command)
                    return
                await self._send_text(
                    sender,
                    "I could not interpret that request safely. Please rephrase the "
                    "licensing change and include the product or line and quantity where "
                    "relevant.",
                )
                return
            if (
                self._configuration.workflow_mode == "simple_pricing"
                and session is not None
                and session.stage
                in {
                    WorkflowStage.AWAITING_MATCH_CONFIRMATION,
                    WorkflowStage.AWAITING_INITIAL_VALIDATION,
                }
                and await self._is_catalog_backed_requirement_statement(command)
            ):
                # While the requirement is unconfirmed, an explicit catalogue-backed
                # seller statement owns the turn even if the model called it a scenario,
                # edit, recommendation, or clarification. Questions, negation, reported
                # speech, and existing-line operations fail the predicate above.
                await self._capture_typed_requirement(sender, command)
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
        pending_slot_completion: bool = False,
    ) -> None:
        if (
            original_message
            and intent.action == "set_requirement_detail"
            and not str(getattr(intent, "detail_value", "") or "").strip()
        ):
            # A model can mistake a bare catalogue title for proposal metadata.  Resolve the
            # seller's own text against the catalogue before the generic mutation guard so
            # the response explains the ambiguity instead of asking for an invented field
            # value.  This branch is read-only and never captures or prices the SKU.
            matches = await self._orchestrator.catalog_candidates(original_message.strip())
            if matches and matches[0].confidence >= 90:
                await self._send_text(
                    sender,
                    "I recognized that as a Microsoft product rather than proposal "
                    "metadata, so I have not added or changed anything. Are you asking "
                    "about its features, or do you want to add that exact SKU?",
                )
                return
        if original_message and intent.action in ASSERTIVE_COMMERCIAL_ACTIONS:
            semantic_session = await self._orchestrator.get_session(sender)
            intent = self._intent_with_seller_supported_targets(
                intent,
                original_message,
                semantic_session,
                pending_slot_completion=pending_slot_completion,
            )
            if not self._is_assertive_commercial_instruction(
                original_message,
                intent,
                semantic_session,
                pending_slot_completion=pending_slot_completion,
            ):
                await self._send_text(
                    sender,
                    "I treated that as a question or example and did not change the "
                    "requirement or proposal. State the licensing change directly, including "
                    "the product and quantity or target proposal where relevant.",
                )
                return
        if intent.action == "help":
            response = self._professional_agent_text(
                str(getattr(intent, "response_text", ""))
            )
            # Help/capability answers are read-only. The explicit greeting/start re-entry
            # branch in ``_handle_text`` is the only place that may create a resume gate,
            # and it renders the choice in the same turn.
            await self._send_text(sender, response[:1200] if response else HELP_TEXT)
            return
        if intent.action == "acknowledge":
            response = self._professional_agent_text(intent.response_text)
            session = await self._orchestrator.get_session(sender)
            if not response:
                if session is not None and session.capture_messages:
                    response = (
                        "You’re welcome. I’ve kept the unfinished licence details; continue "
                        "whenever you are ready."
                    )
                elif session is not None and session.estate is not None:
                    response = (
                        "You’re welcome. The current licensing draft remains saved and "
                        "unchanged."
                    )
                else:
                    response = (
                        "You’re welcome. Send a Microsoft licensing requirement whenever "
                        "you are ready."
                    )
            await self._send_text(sender, response[:500])
            return
        if intent.action == "reset_requirement":
            if original_message and not self._requests_fresh_start(
                original_message
            ):
                await self._send_text(
                    sender,
                    "I have not cleared the saved requirement. Ask what starting fresh does "
                    "if you want an explanation, or explicitly ask me to start a new "
                    "requirement when you are ready.",
                )
                return
            await self._orchestrator.reset_session(sender)
            await self._send_text(
                sender,
                "Done — I cleared the previous draft. Send the first licence and quantity "
                "when you are ready; you can provide them together or one detail at a time.",
            )
            return
        if intent.action == "answer_question":
            topic = str(getattr(intent, "detail_label", "")).strip().casefold()
            if topic == "catalog_budget":
                await self._send_catalog_budget_options(sender, intent)
                return
            if topic == "official_product_question":
                await self._send_official_product_answer(
                    sender,
                    intent,
                    original_message=original_message,
                )
                return
            if original_message:
                normalized_question = " ".join(
                    original_message.casefold().strip(" ?!.,").split()
                )
                fact_session = await self._orchestrator.get_session(sender)
                grounded_fact = self._deterministic_session_fact_answer(
                    fact_session,
                    original_message,
                )
                if grounded_fact:
                    await self._send_text(sender, grounded_fact[:1200])
                    return
                capability_question = bool(
                    re.fullmatch(
                        r"(?:(?:who|what)\s+are\s+you|what\s+do\s+you\s+do|"
                        r"how\s+can\s+you\s+help(?:\s+me)?|what\s+can\s+you\s+do)",
                        normalized_question,
                    )
                )
                if not capability_question and not await self._message_has_licensing_context(
                    original_message
                ):
                    await self._send_non_requirement_boundary(sender)
                    return
            answer = self._professional_agent_text(intent.response_text)
            await self._send_text(
                sender,
                answer[:1000]
                if answer
                else "What would you like to know about the licensing review?",
            )
            follow_up = self._trailing_question(answer)
            if follow_up:
                await self._orchestrator.set_pending_dialogue(
                    sender,
                    PendingDialogue(
                        kind="agent_clarification",
                        question=follow_up[:500],
                        context_message=(original_message or "")[:2000],
                    ),
                )
            return
        if intent.action == "out_of_scope":
            # The model may classify the turn but cannot turn this company endpoint into
            # a general-purpose assistant or relay an unrelated generated answer.
            await self._send_non_requirement_boundary(sender)
            return
        if (
            original_message
            and intent.action in FINALIZATION_CORRECTION_ACTIONS
            and self._contains_multiple_mutation_clauses(original_message)
        ):
            await self._send_text(
                sender,
                "I found more than one independent proposal change in that message. I have "
                "not applied only part of it. Send the first change you want applied; after "
                "I show the revised proposal, send the next one.",
            )
            return
        if intent.action in FINALIZATION_CORRECTION_ACTIONS:
            current = await self._orchestrator.get_session(sender)
            if (
                current is not None
                and current.stage == WorkflowStage.AWAITING_FINAL_VALIDATION
            ):
                # The final-validation prompt explicitly invites corrections. Reopen the
                # proposal first, apply the same parsed correction in this turn, and require
                # a fresh final confirmation afterward.
                await self._orchestrator.cancel_finalization(sender)
        if intent.action == "compare_enterprise_options":
            if original_message and not self._is_comparison_request(original_message):
                await self._send_text(
                    sender,
                    "I have not generated or changed a comparison from that message. "
                    "Explicitly ask me to compare the required options when you want the "
                    "commercial comparison prepared.",
                )
                return
            await self._ensure_operation_allowed(
                sender,
                agent_action=intent.action,
            )
            await self._send_enterprise_comparison(sender)
            return
        if intent.action == "capture_requirement":
            if original_message and self._is_licensing_question_message(original_message):
                await self._send_text(
                    sender,
                    "I treated that as a licensing question and did not add it to the "
                    "requirement. Ask the product question directly, or state the product "
                    "and quantity when you want it added.",
                )
                return
            if original_message and await self._has_open_requirement_draft(sender):
                await self._capture_typed_requirement(sender, original_message)
                return
            product = self._product_from_reply(original_message or "", intent)
            if product:
                quantity = int(getattr(intent, "quantity", -1) or -1)
                if quantity < 0:
                    quantity = self._quantity_from_reply(original_message or "")
                await self._execute_agent_intent(
                    sender,
                    self._agent_intent_with(
                        intent,
                        action="add_sku",
                        product_query=product,
                        quantity=quantity,
                    ),
                    original_message=original_message,
                )
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
            resolved_question = (
                question[:500]
                if question
                else "Which proposal, line, or quantity would you like to change?"
            )
            normalized_question = " ".join(resolved_question.casefold().split())
            original_dimension = self._change_dimension(original_message or "")
            pending_operation = "none"
            if all(
                marker in normalized_question
                for marker in ("quantity", "sku", "disposition")
            ):
                pending_operation = "choose_change"
            elif (
                original_dimension == "sku"
                and re.search(r"\b(?:product|sku|replace)\b", normalized_question)
            ):
                pending_operation = "replace_sku"
            elif (
                original_dimension == "quantity"
                and "quantity" in normalized_question
            ):
                pending_operation = "set_quantity"
            pending = await self._pending_dialogue_for_intent(
                sender,
                intent,
                question=resolved_question,
                original_message=original_message,
                operation=pending_operation,
            )
            if (
                pending_operation != "none"
                and clarification_session is not None
            ):
                inferred_line = self._pending_line_id(
                    pending,
                    clarification_session,
                    original_message or resolved_question,
                    intent,
                )
                pending = pending.model_copy(
                    update={"source_line_id": inferred_line}
                )
            await self._orchestrator.set_pending_dialogue(sender, pending)
            await self._send_text(
                sender,
                resolved_question,
            )
            return
        if intent.action == "confirm_validation":
            if original_message and not self._is_requirement_confirmation_reply(
                original_message
            ):
                await self._send_text(
                    sender,
                    "I have not confirmed, priced, or finalized anything from that message. "
                    "When the displayed requirement is complete and correct, state explicitly "
                    "that you confirm it for pricing; otherwise describe the correction.",
                )
                return
            await self._confirm_validation(sender)
            return
        if intent.action == "reject_validation":
            if original_message and not self._is_explicit_validation_rejection(
                original_message
            ):
                await self._send_text(
                    sender,
                    "I have not rejected or cancelled the validation from that message. "
                    "State directly that the displayed requirement or proposal is not "
                    "correct if you want to reject it, or describe the correction.",
                )
                return
            await self._reject_validation(sender)
            return
        if intent.action == "request_recommendation":
            session = await self._orchestrator.get_session(sender)
            if session is None or session.estate is None:
                question = self._professional_agent_text(
                    str(getattr(intent, "clarification", ""))
                )
                question = question[:500] if question else (
                    "What business capability and user group should it support, for "
                    "example endpoint protection, analytics, collaboration, identity, "
                    "or compliance?"
                )
                await self._orchestrator.set_pending_dialogue(
                    sender,
                    PendingDialogue(
                        kind="agent_clarification",
                        question=question,
                        context_message=(original_message or "Recommend a suitable licence")[:2000],
                    ),
                )
                await self._send_text(
                    sender,
                    "I can help narrow the right Microsoft licence without guessing a SKU. "
                    + question,
                )
                return
            if session.confirmed_as_is is None:
                titles = [
                    line.display_title
                    for line in session.estate.lines
                    if line.display_title
                ]
                visible_titles = ", ".join(titles[:4])
                if len(titles) > 4:
                    visible_titles += f", and {len(titles) - 4} more"
                question = self._professional_agent_text(
                    str(getattr(intent, "clarification", ""))
                )
                if not question or "confirm" in question.casefold():
                    question = (
                        "What business capability and user group should the licence support, "
                        "for example endpoint protection, analytics, collaboration, identity, "
                        "or compliance?"
                    )
                draft_context = (
                    f" Your current draft includes {visible_titles}." if visible_titles else ""
                )
                await self._orchestrator.set_pending_dialogue(
                    sender,
                    PendingDialogue(
                        kind="agent_clarification",
                        question=question[:500],
                        context_message=(
                            original_message or "Recommend a suitable licence"
                        )[:2000],
                    ),
                )
                await self._send_text(
                    sender,
                    "Yes, I can help narrow the right licence without changing or pricing the "
                    f"draft.{draft_context} {question}",
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
            await self._ensure_operation_allowed(
                sender,
                agent_action=intent.action,
            )
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
            if not label or not value:
                await self._send_text(
                    sender,
                    "Which proposal detail should I record, and what is its exact value? "
                    "For example: Customer name is Contoso.",
                )
                return
            possible_product = " ".join(part for part in (label, value) if part).strip()
            if possible_product:
                matches = await self._orchestrator.catalog_candidates(possible_product)
                if matches and matches[0].confidence >= 90:
                    await self._send_text(
                        sender,
                        "I recognized that as a Microsoft product rather than proposal "
                        "metadata, so I have not added or changed anything. Are you asking "
                        "about its features, or do you want to add that exact SKU?",
                    )
                    return
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
        if await self._pause_for_missing_intent_detail(
            sender,
            intent,
            original_message,
        ):
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
                line_id = await self._resolve_line_reference(
                    sender,
                    intent.line_id,
                    requirement=True,
                    operation="change",
                    original_message=original_message,
                    pending_intent=intent,
                )
                if line_id is None:
                    return
                estate = await self._orchestrator.edit_requirement_quantity(
                    sender,
                    line_id,
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
                line_id = await self._resolve_line_reference(
                    sender,
                    intent.line_id,
                    requirement=True,
                    operation="replace",
                    original_message=original_message,
                    pending_intent=intent,
                )
                if line_id is None:
                    return
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
                line_id = await self._resolve_line_reference(
                    sender,
                    intent.line_id,
                    requirement=True,
                    operation="remove",
                    original_message=original_message,
                    pending_intent=intent,
                )
                if line_id is None:
                    return
                await self._remove_requirement_line(
                    sender,
                    line_id,
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
        if intent.action == "finalize":
            # Finalize is also the explicit approval when the proposal is already at
            # the final-validation gate. Route it before the general operation guard,
            # which intentionally blocks ordinary edits while approval is pending.
            await self._request_finalization(sender, seller_message=original_message)
            return
        await self._ensure_operation_allowed(sender, agent_action=intent.action)
        if await self._select_or_request_scenario_target(
            sender,
            intent,
            original_message,
        ):
            return
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
            line_id = await self._resolve_line_reference(
                sender,
                intent.line_id,
                requirement=False,
                operation="change",
                original_message=original_message,
                pending_intent=intent,
            )
            if line_id is None:
                return
            quantity = self._required_quantity(intent.quantity)
            scenario = await self._orchestrator.edit_quantity(
                sender, line_id, quantity
            )
            await self._send_scenario(sender, scenario)
            return
        if intent.action == "set_copilot":
            quantity = self._required_quantity(intent.copilot_quantity)
            current = await self._orchestrator.get_session(sender)
            active = (
                current.scenarios.get(current.active_scenario)
                if current is not None and current.active_scenario is not None
                else None
            )
            all_lines = list(active.lines) if active is not None else []
            visible_lines = [line for line in all_lines if line.proposed_quantity > 0]
            synthetic = next(
                (line for line in all_lines if line.line_id == "COPILOT"),
                None,
            )
            real_copilot_lines = [
                line
                for line in visible_lines
                if line.line_id != "COPILOT"
                and "copilot" in normalize_product_title(line.sku_title)
            ]
            target_line_id = synthetic.line_id if synthetic is not None else ""
            if not target_line_id and len(real_copilot_lines) == 1:
                target_line_id = real_copilot_lines[0].line_id
            if not target_line_id and len(real_copilot_lines) > 1:
                choices = "; ".join(line.sku_title for line in real_copilot_lines)
                question = (
                    "Which Copilot licence should I change? Reply with the product name: "
                    f"{choices}"
                )
                pending = await self._pending_dialogue_for_intent(
                    sender,
                    self._agent_intent_with(
                        intent,
                        action="set_quantity",
                        quantity=quantity,
                    ),
                    question=question,
                    original_message=original_message,
                    operation="set_quantity",
                    scope="scenario",
                )
                await self._orchestrator.set_pending_dialogue(sender, pending)
                await self._send_text(sender, question)
                return
            if not target_line_id:
                question = (
                    "There is no Copilot licence in the active proposal. Which exact "
                    "Microsoft Copilot product should I add?"
                )
                pending = await self._pending_dialogue_for_intent(
                    sender,
                    self._agent_intent_with(
                        intent,
                        action="add_sku",
                        product_query="",
                        quantity=quantity,
                    ),
                    question=question,
                    original_message=original_message,
                    operation="add_sku",
                    scope="scenario",
                )
                await self._orchestrator.set_pending_dialogue(sender, pending)
                await self._send_text(sender, question)
                return
            scenario = await self._orchestrator.edit_quantity(
                sender,
                target_line_id,
                quantity,
            )
            await self._send_scenario(sender, scenario)
            return
        if intent.action == "set_disposition":
            line_id = await self._resolve_line_reference(
                sender,
                intent.line_id,
                requirement=False,
                operation=("remove" if intent.disposition == "remove" else "update"),
                original_message=original_message,
                pending_intent=intent,
            )
            if line_id is None:
                return
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
            line_id = await self._resolve_line_reference(
                sender,
                intent.line_id,
                requirement=False,
                operation="replace",
                original_message=original_message,
                pending_intent=intent,
            )
            if line_id is None:
                return
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
            seller_grounded = bool(intent.match_selections) and all(
                self._candidate_selection_is_seller_grounded(
                    original_message or "",
                    line_id=item.line_id,
                    candidate_number=item.candidate_number,
                    single_pending_line=len(pending) == 1,
                )
                for item in intent.match_selections
            )
            if original_message and not seller_grounded:
                await self._send_text(
                    sender,
                    "I did not treat that message as a direct product selection, so no SKU "
                    "was confirmed. Reply with a displayed option number (and the line when "
                    "more than one line is unresolved), or send the complete product name.",
                )
                return
            selected_ids = {item.line_id.upper() for item in intent.match_selections}
            if not selected_ids or not selected_ids.issubset(set(pending)):
                raise ValueError("Choose one of the displayed options for a pending line.")
            selections: dict[str, tuple[str, str, str]] = {}
            for item in intent.match_selections:
                line_id = item.line_id.upper()
                candidates = pending[line_id].candidates
                index = item.candidate_number - 1
                if index < 0 or index >= len(candidates):
                    raise ValueError(
                        f"Candidate {item.candidate_number} is invalid for {line_id}."
                    )
                candidate = candidates[index]
                selections[line_id] = (
                    candidate.product_id,
                    candidate.sku_id,
                    candidate.sku_title,
                )
            estate = await self._orchestrator.confirm_matches(sender, selections)
            await self._after_requirement_match_confirmation(sender, estate)
            return
        if intent.action == "confirm_sku":
            explicit_candidate = (
                self._single_candidate_number(original_message)
                if original_message and "?" not in original_message
                else None
            )
            if (
                intent.candidate_number <= 0
                or explicit_candidate != intent.candidate_number
            ):
                raise ValueError("Choose one of the numbered SKU candidates.")
            result = await self._orchestrator.confirm_sku_change(
                sender,
                intent.candidate_number,
            )
            await self._send_sku_change_result(sender, result)
            return
        if intent.action == "cancel_sku":
            if original_message and not self._requests_pending_change_cancel(
                " ".join(original_message.casefold().strip(" ?!.,").split())
            ):
                await self._send_text(
                    sender,
                    "I have not cancelled the pending product change. The proposal remains "
                    "unchanged.",
                )
                return
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
            await self._request_finalization(sender, seller_message=original_message)
            return
        if intent.action == "compare":
            if original_message and not self._is_comparison_request(original_message):
                await self._send_text(
                    sender,
                    "I have not generated a comparison from that message. Explicitly ask me "
                    "to compare the proposals when you want the commercial comparison.",
                )
                return
            await self._send_comparison(sender)
            return
        raise ValueError("The interpreted action is not supported.")

    async def _handle_interactive(self, sender: str, reply_id: str) -> None:
        parts = reply_id.split("|")
        if len(parts) < 3 or parts[0] != "licensing":
            raise ValueError("That menu selection is no longer recognized.")
        action, value = parts[1], parts[2]
        if action in {"validate_initial", "validate_final"}:
            # These tokenless IDs were emitted by an older WhatsApp UI. They carry no
            # requirement/proposal identity, so an old button can arrive after a fresh
            # session and would otherwise approve unrelated commercial state. Current
            # validation is deliberately performed by a natural-language confirmation;
            # no active message generator emits these legacy IDs.
            await self._send_text(
                sender,
                "That approval selection belongs to an earlier proposal and is no longer "
                "active. Nothing was confirmed or finalized. Review the latest requirement "
                "or proposal and confirm it in a new message.",
            )
            return
        if action in {"recommend", "scenario", "compare", "finalize", "scenarios"}:
            # Legacy buttons did not carry a proposal/version token. A delayed click could
            # otherwise mutate a newer session, so these IDs are read as stale regardless of
            # their label. Current commercial actions are confirmed in natural language or
            # use token-bound SKU/requirement selection IDs above.
            await self._send_text(
                sender,
                "That menu selection belongs to an earlier proposal and is no longer "
                "active. Nothing was changed. State the requested proposal action in a new "
                "message.",
            )
            return
        if action == "match_confirm":
            if len(parts) != 5 or not parts[4].isdigit():
                raise ValueError(
                    "That product-selection menu is no longer active. Review the latest "
                    "requirement and use its current choices."
                )
            estate = await self._confirm_requirement_candidate(
                sender,
                capture_token=value,
                line_id=parts[3],
                candidate_number=int(parts[4]),
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
            if "images" in locals():
                raise
            logger.exception(
                "Unable to render scenario table image; using text fallback"
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
        if pending.candidate_narrowing_required:
            await self._send_text(
                sender,
                "*I need a more specific product description*\n\n"
                + candidate_narrowing_question(
                    pending.product_query,
                    len(pending.candidates),
                )
                + "\n\n"
                "I retained the requested action, quantity, and source product. Reply "
                "with the missing qualifier; no proposal change has been applied.",
            )
            return
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
            f"{self._seller_safe_research_text(insight.recommendation)}\n\n"
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
            if "images" in locals():
                raise
            logger.exception(
                "Unable to render comparison table image; using text fallback"
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
                scenario = await self._orchestrator.confirm_requirement_and_price_as_is(
                    sender,
                    promo_eligible=False,
                )
                unavailable = [line for line in scenario.lines if line.price_unavailable]
                if unavailable:
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

    async def _request_finalization(
        self,
        sender: str,
        *,
        seller_message: str | None = None,
    ) -> None:
        current = await self._orchestrator.get_session(sender)
        if seller_message is not None:
            allowed = (
                self._is_explicit_finalization_approval(seller_message)
                if current is not None
                and current.stage == WorkflowStage.AWAITING_FINAL_VALIDATION
                else self._is_assertive_finalization_request(seller_message)
            )
            if not allowed:
                await self._send_text(
                    sender,
                    "I have not opened, approved, or finalized the proposal from that "
                    "message. Ask about finalization if you need guidance, or directly ask "
                    "me to finalize when the displayed proposal is correct.",
                )
                return
        if current is not None and current.stage == WorkflowStage.AWAITING_FINAL_VALIDATION:
            # Only an explicit approval of the visible final-validation gate may complete
            # it. The semantic check above rejects questions, hypotheticals, reported
            # speech, and negation before reaching this transition.
            await self._confirm_finalization_and_send(sender)
            return
        if (
            current is not None
            and current.stage == WorkflowStage.FINALIZED
            and current.active_scenario is not None
            and current.active_scenario in current.scenarios
        ):
            finalized = current.scenarios[current.active_scenario]
            await self._send_text(
                sender,
                "This proposal is already finalized. I am sending the finalized output "
                "again; no commercial value or configuration has been changed.",
            )
            if self._configuration.workflow_mode == "simple_pricing":
                await self._send_simple_revised(sender, finalized)
                await self._send_simple_commercial_pdf(sender)
            else:
                await self._send_scenario(sender, finalized)
                if self._configuration.workflow_mode in {
                    "renewal_only",
                    "upgrade_comparison",
                }:
                    await self._send_active_proposal_pdf(sender)
            return
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
        if session.stage == WorkflowStage.AWAITING_MATCH_CONFIRMATION:
            raise ValueError(
                "The exact Microsoft SKU is still unresolved for one or more licences. No "
                "pricing or comparison was prepared. Confirm a displayed catalogue option, "
                "provide the complete product name, or explicitly remove that licence first."
            )
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
            direct_correction = bool(
                direct_command
                and direct_command.startswith(
                    (
                        "/set ",
                        "/copilot ",
                        "/add ",
                        "/replace ",
                        "/retain ",
                        "/remove ",
                        "/migrate ",
                        "/included ",
                        "/term ",
                        "/billing ",
                        "/segment ",
                        "/comment ",
                    )
                )
            )
            if direct_correction or agent_action in FINALIZATION_CORRECTION_ACTIONS:
                await self._orchestrator.cancel_finalization(sender)
                return
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

    @classmethod
    def _requests_monthly_billing(cls, message: str) -> bool:
        normalized = " ".join(message.casefold().strip(" ?!.,").split())
        monthly = r"(?:monthly|month-to-month|per\s+month|p1m)"
        if re.fullmatch(rf"{monthly}(?:\s+billing)?", normalized):
            return True
        return cls._is_assertive_action_request(
            message,
            rf"(?:(?:set|change|switch|use|apply|make)"
            rf"(?:\s+(?:the\s+)?billing(?:\s+plan)?)?(?:\s+to)?\s+{monthly}|"
            rf"bill(?:\s+it)?\s+{monthly})",
        )

    @classmethod
    def _requests_restricted_pricing(cls, message: str) -> bool:
        normalized = " ".join(message.casefold().strip(" ?!.,").split())
        restricted_atom = (
            r"(?:promos?|promotions?|promotional(?:\s+pricing)?|discounts?|"
            r"adjustments?|margins?|partner(?:'s)?[ -]best(?:\s+price)?|"
            r"best\s+(?:offer|price)|distributor\s+margins?)"
        )
        restricted = (
            rf"{restricted_atom}(?:\s*(?:,|and|or)\s*"
            rf"(?:any\s+|an?\s+|the\s+)?{restricted_atom})*"
        )
        if re.fullmatch(
            rf"(?:-?\d+(?:\.\d+)?\s*%\s+discount|"
            rf"(?:commercial\s+)?adjustment\s+(?:of\s+)?-?\d+(?:\.\d+)?)",
            normalized,
        ):
            return True
        return cls._is_assertive_action_request(
            message,
            rf"(?:(?:apply|add|set|change|remove|use|enable|disable|give(?:\s+me)?)"
            rf"(?:\s+(?:a|the))?\s+{restricted}(?:\s+of\s+-?\d+(?:\.\d+)?)?|"
            rf"(?:find|show)\s+(?:me\s+)?(?:the\s+)?best\s+(?:offer|price))",
        )

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
