from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.config import get_logger
from app.core.licensing.analysis import LicenseAnalysisError
from app.core.licensing.agent import (
    AgentIntent,
    IntentInterpretationError,
    IntentInterpreter,
)
from app.core.licensing.models import MigrationDisposition, ScenarioType, SkuChangeResult
from app.core.licensing.orchestrator import LicensingOrchestrator
from app.core.licensing.renderer import (
    format_comparison,
    format_estate,
    format_pending_matches,
    format_scenario,
    render_comparison_pdf,
    render_estate_pdf,
)
from app.core.licensing.scenarios import ScenarioError
from app.core.licensing.store import WorkflowConflictError
from app.core.whatsapp import WhatsAppAPIError, WhatsAppClient
from app.schema.whatsapp import (
    IncomingWhatsAppDocument,
    IncomingWhatsAppMessage,
    InteractiveButton,
    InteractiveButtonAction,
    InteractiveButtons,
    InteractiveList,
    InteractiveListAction,
    InteractiveReply,
    InteractiveRow,
    InteractiveSection,
    InteractiveText,
    TextContent,
    WhatsAppInteractiveMessage,
    WhatsAppTextMessage,
    WhatsAppWebhookPayload,
)

logger = get_logger(__name__)


HELP_TEXT = """*SP/SSP Licensing Agent*

1. Upload the customer's .csv or .xlsx licence file.
2. Select Renew As-Is, ME3 + Copilot, ME5 + Copilot, or ME7.
3. Review and edit the proposed configuration.
4. Use /compare for the commercial comparison and PDF.

Commands:
/scenario renew|me3|me5|me7 [| base qty | Copilot qty]
/set LINE QTY
/retain LINE, /remove LINE, /migrate LINE, /included LINE
/add PRODUCT TITLE | QTY
/replace LINE | PRODUCT TITLE | QTY
/confirm-sku NUMBER
/cancel-sku
/copilot QTY
/promo on|off
/discount PERCENT
/adjust AMOUNT
/term TERM
/billing PLAN
/segment SEGMENT
/currency CODE
/comment TEXT
/finalize
/compare
/help"""


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
    currency: str = "INR"


class WhatsAppWebhookService:
    def __init__(
        self,
        whatsapp_client: WhatsAppClient,
        orchestrator: LicensingOrchestrator,
        configuration: ServiceConfiguration,
        *,
        intent_interpreter: IntentInterpreter | None = None,
    ) -> None:
        self._whatsapp_client = whatsapp_client
        self._orchestrator = orchestrator
        self._configuration = configuration
        self._intent_interpreter = intent_interpreter

    async def handle(self, webhook: WhatsAppWebhookPayload) -> None:
        for entry in webhook.entry:
            for change in entry.changes:
                for message in change.value.messages:
                    await self._handle_message(message)

    async def _handle_message(self, message: IncomingWhatsAppMessage) -> None:
        sender = message.sender.lstrip("+")
        if (
            self._configuration.seller_allowlist
            and sender not in self._configuration.seller_allowlist
        ):
            logger.warning("Unauthorized WhatsApp sender rejected")
            await self._send_text(sender, "This WhatsApp number is not authorized.")
            return
        if await self._orchestrator.has_processed(sender, message.id):
            logger.info("Duplicate WhatsApp message ignored message_id=%s", message.id)
            return

        try:
            if message.type == "text" and message.text is not None:
                await self._handle_text(sender, message.text.body)
            elif message.type == "document" and message.document is not None:
                await self._handle_document(sender, message.document)
            elif message.type == "interactive" and message.interactive is not None:
                reply = message.interactive.list_reply or message.interactive.button_reply
                if reply is None:
                    raise ValueError("The interactive response was empty.")
                await self._handle_interactive(sender, reply.id)
            else:
                await self._send_text(
                    sender,
                    "Send a .csv/.xlsx licence file, text command, or menu selection.",
                )
            await self._orchestrator.mark_processed(sender, message.id)
        except (LicenseAnalysisError, ScenarioError, ValueError) as error:
            logger.info("User-correctable workflow error type=%s", type(error).__name__)
            await self._send_text(sender, f"I could not apply that request: {error}")
            await self._orchestrator.mark_processed(sender, message.id)
        except WorkflowConflictError:
            logger.warning("Workflow concurrency conflict message_id=%s", message.id)
            await self._send_text(sender, "The proposal changed concurrently. Please retry.")
        except Exception:
            logger.exception("Unexpected workflow failure message_id=%s", message.id)
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
        suffix = document.filename.lower().rsplit(".", 1)[-1]
        if suffix not in {"csv", "xlsx"}:
            raise LicenseAnalysisError("Upload a .csv or .xlsx licence file.")
        media = await self._whatsapp_client.download_media(
            media_id=document.id,
            filename=document.filename,
            content_type=document.mime_type,
        )
        if len(media.content) > self._configuration.max_document_bytes:
            raise LicenseAnalysisError(
                f"The file exceeds the {self._configuration.max_document_bytes // 1048576} MB limit."
            )
        estate = await self._orchestrator.analyze_document(
            sender=sender,
            filename=media.filename,
            content=media.content,
        )
        await self._send_estate_report(sender, estate)
        if estate.pending_lines:
            await self._send_text_chunks(sender, format_pending_matches(estate))
        else:
            await self._send_scenario_menu(sender)

    async def _send_estate_report(self, sender: str, estate) -> None:
        pdf = render_estate_pdf(estate)
        try:
            await self._whatsapp_client.send_document(
                to=sender,
                content=pdf,
                filename="customer-licence-estate.pdf",
                content_type="application/pdf",
                caption=(
                    "Customer licence estate grouped by product family, with expiry and "
                    "migration-review flags"
                ),
            )
        except WhatsAppAPIError as error:
            logger.error(
                "Unable to send estate PDF status=%s network_error=%s",
                error.status_code,
                error.network_error,
            )
            raise

    async def _handle_text(self, sender: str, body: str) -> None:
        command = body.strip()
        lowered = command.casefold()
        if lowered in {"/help", "/start", "/about", "/analyze"}:
            await self._send_text(sender, HELP_TEXT)
            return
        if lowered.startswith("/confirm "):
            selections = self._parse_confirmations(command[9:])
            estate = await self._orchestrator.confirm_matches(sender, selections)
            await self._send_text_chunks(sender, format_estate(estate))
            await self._send_scenario_menu(sender)
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
        if lowered.startswith("/scenario "):
            scenario_type, base, copilot = self._parse_scenario(command[10:])
            scenario = await self._orchestrator.build_scenario(
                sender,
                scenario_type,
                base_quantity=base,
                copilot_quantity=copilot,
            )
            await self._send_scenario(sender, scenario)
            return
        if lowered.startswith("/set "):
            line_id, quantity = self._line_quantity(command[5:])
            scenario = await self._orchestrator.edit_quantity(sender, line_id, quantity)
            await self._send_scenario(sender, scenario)
            return
        if lowered.startswith("/copilot "):
            quantity = self._positive_or_zero(command[9:].strip(), "Copilot quantity")
            scenario = await self._orchestrator.edit_quantity(sender, "COPILOT", quantity)
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
            scenario = await self._orchestrator.reconfigure_pricing(
                sender,
                term_duration=self._required_text(command[6:], "contract term"),
            )
            await self._send_scenario(sender, scenario)
            return
        if lowered.startswith("/billing "):
            scenario = await self._orchestrator.reconfigure_pricing(
                sender,
                billing_plan=self._required_text(command[9:], "billing plan"),
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
                    "Currency conversion is unavailable because the Outcome Sheet has no "
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
            scenario = await self._orchestrator.finalize(sender)
            await self._send_scenario(sender, scenario)
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
                    "I could not interpret that sentence safely. Use a menu option or "
                    "send /help for the equivalent auditable commands.",
                )
                return
            await self._execute_agent_intent(sender, intent)
            return

        session = await self._orchestrator.get_session(sender)
        if session is None or session.estate is None:
            await self._send_text(sender, HELP_TEXT)
            return
        await self._send_text(
            sender,
            "Natural-language routing is disabled in this environment. Use a menu option "
            "or send /help for the equivalent auditable commands.",
        )

    async def _execute_agent_intent(self, sender: str, intent: AgentIntent) -> None:
        if intent.action == "help":
            await self._send_text(sender, HELP_TEXT)
            return
        if intent.action == "clarify":
            question = intent.clarification.strip()
            await self._send_text(
                sender,
                f"I need one more detail: {question[:500]}"
                if question
                else "I need the exact scenario, line, or quantity before changing the proposal.",
            )
            return
        if intent.action == "build_scenario":
            if intent.scenario == "none":
                raise ValueError("Specify Renew As-Is, ME3 + Copilot, ME5 + Copilot, or ME7.")
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
            quantity = self._required_quantity(intent.quantity, allow_zero=False)
            result = await self._orchestrator.replace_sku(
                sender, line_id, product, quantity
            )
            await self._send_sku_change_result(sender, result)
            return
        if intent.action == "add_comment":
            comment = self._required_text(intent.comment, "comment")
            scenario = await self._orchestrator.add_comment(sender, comment)
            await self._send_scenario(sender, scenario)
            return
        if intent.action == "finalize":
            scenario = await self._orchestrator.finalize(sender)
            await self._send_scenario(sender, scenario)
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
        if action == "scenario":
            try:
                scenario_type = ScenarioType(value)
            except ValueError as error:
                raise ValueError("Unknown commercial scenario.") from error
            scenario = await self._orchestrator.build_scenario(sender, scenario_type)
            await self._send_scenario(sender, scenario)
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
        if action == "compare":
            await self._send_comparison(sender)
            return
        if action == "finalize":
            scenario = await self._orchestrator.finalize(sender)
            await self._send_scenario(sender, scenario)
            return
        if action == "scenarios":
            await self._send_scenario_menu(sender)
            return
        raise ValueError("Unknown commercial workflow action.")

    async def _send_scenario_menu(self, sender: str) -> None:
        rows = [
            InteractiveRow(
                id=f"licensing|scenario|{scenario.value}",
                title=scenario.label,
                description={
                    ScenarioType.RENEW_AS_IS: "Retain and price the current estate",
                    ScenarioType.ME3_COPILOT: "ME3 plus independent Copilot quantity",
                    ScenarioType.ME5_COPILOT: "ME5 plus independent Copilot quantity",
                    ScenarioType.ME7: "Migrate to the configured ME7 package",
                }[scenario],
            )
            for scenario in ScenarioType
        ]
        await self._send_interactive(
            sender,
            InteractiveList(
                body=InteractiveText(
                    text="Which commercial recommendation would you like to prepare?"
                ),
                action=InteractiveListAction(
                    button="Choose scenario",
                    sections=[InteractiveSection(title="Scenarios", rows=rows)],
                ),
            ),
        )

    async def _send_scenario(self, sender: str, scenario) -> None:
        text = format_scenario(scenario, self._configuration.currency)
        await self._send_text_chunks(sender, text)
        buttons = [
            InteractiveButton(
                reply=InteractiveReply(id="licensing|scenarios|all", title="Other scenario")
            ),
            InteractiveButton(
                reply=InteractiveReply(id="licensing|compare|all", title="Compare")
            ),
            InteractiveButton(
                reply=InteractiveReply(id="licensing|finalize|active", title="Finalize")
            ),
        ]
        await self._send_interactive(
            sender,
            InteractiveButtons(
                body=InteractiveText(text="Continue working with this proposal."),
                action=InteractiveButtonAction(buttons=buttons),
            ),
        )

    async def _send_sku_change_result(
        self,
        sender: str,
        result: SkuChangeResult,
    ) -> None:
        if result.state == "applied":
            if result.scenario is None:
                raise RuntimeError("Applied SKU change did not return a scenario.")
            await self._send_scenario(sender, result.scenario)
            return
        pending = result.confirmation
        if pending is None:
            raise RuntimeError("SKU confirmation state did not contain candidates.")
        lines = [
            "*Confirmation required*",
            f"No change was made for: {pending.product_query}",
            "Choose the intended Outcome Sheet SKU:",
        ]
        rows: list[InteractiveRow] = []
        for index, candidate in enumerate(pending.candidates, start=1):
            lines.append(
                f"{index}) {candidate.sku_title} "
                f"[{candidate.product_id}/{candidate.sku_id}] "
                f"({candidate.confidence:.1f}%)"
            )
            rows.append(
                InteractiveRow(
                    id=f"licensing|sku_confirm|{pending.id}|{index}",
                    title=candidate.sku_title[:24],
                    description=(
                        f"{candidate.confidence:.1f}% · "
                        f"{candidate.product_id}/{candidate.sku_id}"
                    )[:72],
                )
            )
        lines.extend(
            [
                "",
                "Reply /confirm-sku NUMBER, choose a row below, or send /cancel-sku.",
            ]
        )
        await self._send_text_chunks(sender, "\n".join(lines))
        await self._send_interactive(
            sender,
            InteractiveList(
                body=InteractiveText(
                    text="Confirm the exact SKU before changing the proposal."
                ),
                action=InteractiveListAction(
                    button="Choose exact SKU",
                    sections=[InteractiveSection(title="Top matches", rows=rows)],
                ),
            ),
        )

    async def _send_comparison(self, sender: str) -> None:
        estate, scenarios, comparison = await self._orchestrator.comparison(sender)
        await self._send_text_chunks(
            sender,
            format_comparison(comparison, self._configuration.currency),
        )
        pdf = render_comparison_pdf(
            estate,
            scenarios,
            comparison,
            currency=self._configuration.currency,
        )
        try:
            await self._whatsapp_client.send_document(
                to=sender,
                content=pdf,
                filename="licensing-commercial-comparison.pdf",
                content_type="application/pdf",
                caption="Customer-ready licensing commercial comparison",
            )
        except WhatsAppAPIError as error:
            logger.error(
                "Unable to send comparison PDF status=%s network_error=%s",
                error.status_code,
                error.network_error,
            )
            raise

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

    async def _send_text_chunks(self, sender: str, body: str) -> None:
        for chunk in self._text_chunks(body):
            await self._send_text(sender, chunk)

    async def _send_interactive(
        self,
        sender: str,
        interactive: InteractiveList | InteractiveButtons,
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
            split_at = remaining.rfind("\n\n", 0, limit + 1)
            if split_at <= 0:
                split_at = remaining.rfind("\n", 0, limit + 1)
            if split_at <= 0:
                split_at = limit
            chunks.append(remaining[:split_at].rstrip())
            remaining = remaining[split_at:].lstrip()
        if remaining:
            chunks.append(remaining)
        return chunks
