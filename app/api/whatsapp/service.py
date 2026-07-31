import json
import secrets
from asyncio import get_running_loop
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from app.config import get_logger
from app.core.agent.main import PricingAgentAPIError, PricingAgentClient
from app.core.whatsapp import WhatsAppAPIError, WhatsAppClient
from app.schema.whatsapp import (
    IncomingWhatsAppDocument,
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

HELP_TEXT = """Pricing Agent Bot commands:

/help or /start - Show this command list.
/about - Learn what this bot can do.
/quote product query | target quantity | existing quantity [| product id | sku id | term duration | billing plan] - Create a final quote.
/analyze - Upload a tenant document to generate quotes for every detected license.

You can also send a normal message to chat with the pricing agent."""

ABOUT_TEXT = (
    "Pricing Agent Bot helps analyze Microsoft tenant documents, create pricing quotes, "
    "and answer Microsoft licensing questions. Send /help to see available commands."
)


class CommandType(StrEnum):
    HELP = "help"
    ABOUT = "about"
    ANALYZE = "analyze"
    ANALYZE_AND_QUOTE = "analyze-and-quote"
    QUOTE = "quote"


COMMAND_ALIASES = {
    "/help": CommandType.HELP,
    "/start": CommandType.HELP,
    "/commands": CommandType.HELP,
    "/about": CommandType.ABOUT,
    "/analyze": CommandType.ANALYZE,
    "/analyze-and-quote": CommandType.ANALYZE_AND_QUOTE,
    "/quote": CommandType.QUOTE,
}


@dataclass(frozen=True)
class QuoteResults:
    filename: str
    product_count: int
    final_quotes: list[dict[str, Any]]
    failures: list[str]


@dataclass(frozen=True)
class QuoteSession:
    sender: str
    results: QuoteResults
    expires_at: datetime


class WhatsAppWebhookService:
    """Processes inbound WhatsApp text messages through the pricing agent."""

    def __init__(
        self,
        whatsapp_client: WhatsAppClient,
        pricing_agent_client: PricingAgentClient,
    ) -> None:
        self._whatsapp_client = whatsapp_client
        self._pricing_agent_client = pricing_agent_client
        self._processed_message_ids: set[str] = set()
        self._message_id_order: deque[str] = deque(maxlen=1000)
        self._quote_sessions: dict[str, QuoteSession] = {}

    async def handle(self, webhook: WhatsAppWebhookPayload) -> None:
        message_count = sum(
            len(change.value.messages)
            for entry in webhook.entry
            for change in entry.changes
        )
        logger.info("Message processing started count=%d", message_count)
        for entry in webhook.entry:
            for change in entry.changes:
                for message in change.value.messages:
                    if message.id in self._processed_message_ids:
                        logger.info("Duplicate message ignored message_id=%s", message.id)
                        continue
                    if len(self._message_id_order) == self._message_id_order.maxlen:
                        self._processed_message_ids.discard(self._message_id_order[0])
                    self._message_id_order.append(message.id)
                    self._processed_message_ids.add(message.id)
                    logger.info("Processing message type=%s", message.type)
                    if message.type == "text" and message.text is not None:
                        await self._handle_text_message(message.sender, message.text.body)
                    elif message.type == "document" and message.document is not None:
                        await self._handle_document_message(message.sender, message.document)
                    elif message.type == "interactive" and message.interactive is not None:
                        reply = (
                            message.interactive.list_reply
                            or message.interactive.button_reply
                        )
                        if reply is not None:
                            await self._handle_interactive_reply(message.sender, reply.id)
                    else:
                        logger.info("Unsupported message ignored type=%s", message.type)
        logger.info("Message processing completed count=%d", message_count)

    async def _handle_text_message(self, sender: str, body: str) -> None:
        command_type = self._command_type(body)
        logger.info("Text message classified command=%s", command_type or "chat")
        if command_type is CommandType.HELP:
            await self._send_text(sender, HELP_TEXT)
            return
        if command_type is CommandType.ABOUT:
            await self._send_text(sender, ABOUT_TEXT)
            return
        if command_type is CommandType.ANALYZE:
            await self._send_text(sender, "Upload a tenant document to generate all quotes automatically.")
            return
        if command_type is CommandType.ANALYZE_AND_QUOTE:
            await self._send_text(
                sender,
                "Upload a tenant document. I will analyze every license and generate all pricing options.",
            )
            return

        try:
            quote_parameters = self._quote_parameters(body, "/quote", include_existing=True)
            if command_type is CommandType.QUOTE:
                if quote_parameters is None:
                    await self._send_text(
                        sender,
                        "Use /quote product query | target quantity | existing quantity "
                        "[| product id | sku id | term duration | billing plan]",
                    )
                    return
                agent_response = await self._pricing_agent_client.create_final_quote(
                    **quote_parameters
                )
            else:
                agent_response = await self._pricing_agent_client.chat(body)
            response_body = self._response_text(agent_response)
            logger.info("Text message agent processing completed")
        except PricingAgentAPIError as error:
            logger.error(
                "Pricing agent failed while processing text status=%s response=%s cause=%r",
                error.status_code,
                error.response_body,
                error.__cause__,
            )
            response_body = self._quote_selection_text(error) or (
                "I could not process your request right now. Please try again."
            )

        await self._send_text(sender, response_body)

    async def _handle_document_message(
        self, sender: str, document: IncomingWhatsAppDocument
    ) -> None:
        logger.info(
            "Document processing started filename=%s mime_type=%s",
            document.filename,
            document.mime_type,
        )
        try:
            media = await self._whatsapp_client.download_media(
                media_id=document.id,
                filename=document.filename,
                content_type=document.mime_type,
            )
        except WhatsAppAPIError as error:
            logger.error(
                "Unable to download WhatsApp document media_id=%s status=%s "
                "network_error=%s response=%s cause=%r",
                document.id,
                error.status_code,
                error.network_error,
                error.response_body,
                error.__cause__,
            )
            if not error.network_error:
                await self._send_text(
                    sender, "I could not download that document. Please try again."
                )
            else:
                logger.error(
                    "WhatsApp failure notification skipped because Meta is unreachable"
                )
            return
        logger.info("Document downloaded filename=%s bytes=%d", media.filename, len(media.content))

        try:
            results = await self._document_quote_results(
                file=media.content,
                filename=media.filename,
                content_type=media.content_type,
            )
        except PricingAgentAPIError as error:
            logger.exception("Pricing agent failed while processing document %s", document.id)
            response_body = self._quote_selection_text(error) or (
                "I could not process that document right now. Please try again."
            )
            await self._send_text(sender, response_body)
            return

        if results.final_quotes:
            session_id = self._create_quote_session(sender, results)
            if not await self._send_product_list(sender, session_id, 0):
                await self._send_text_chunks(
                    sender,
                    self._final_quotes_text(
                        filename=results.filename,
                        product_count=results.product_count,
                        final_quotes=results.final_quotes,
                        failures=results.failures,
                    ),
                )
        else:
            await self._send_text_chunks(
                sender,
                self._final_quotes_text(
                    filename=results.filename,
                    product_count=results.product_count,
                    final_quotes=results.final_quotes,
                    failures=results.failures,
                ),
            )
        logger.info("Document processing completed filename=%s", media.filename)

    async def _send_text(self, sender: str, body: str) -> None:
        if not getattr(self._whatsapp_client, "credentials_valid", True):
            logger.error(
                "WhatsApp send skipped because authentication is invalid; "
                "replace WHATSAPP_ACCESS_TOKEN and restart"
            )
            return
        logger.info("Sending WhatsApp text characters=%d", len(body))
        try:
            await self._whatsapp_client.send_message(
                WhatsAppTextMessage(
                    to=sender,
                    text=TextContent(body=body),
                )
            )
            logger.info("WhatsApp text sent")
        except WhatsAppAPIError as error:
            logger.error(
                "Unable to send WhatsApp response status=%s response=%s cause=%r",
                error.status_code,
                error.response_body,
                error.__cause__,
            )

    async def _send_text_chunks(self, sender: str, body: str) -> None:
        chunks = self._text_chunks(body)
        logger.info("Sending WhatsApp response chunks=%d", len(chunks))
        for index, chunk in enumerate(chunks, start=1):
            logger.info("Sending WhatsApp response chunk=%d/%d", index, len(chunks))
            await self._send_text(sender, chunk)

    async def _document_quote_text(
        self,
        file: bytes,
        filename: str,
        content_type: str,
    ) -> str:
        results = await self._document_quote_results(file, filename, content_type)
        return self._final_quotes_text(
            filename=results.filename,
            product_count=results.product_count,
            final_quotes=results.final_quotes,
            failures=results.failures,
        )

    async def _document_quote_results(
        self,
        file: bytes,
        filename: str,
        content_type: str,
    ) -> QuoteResults:
        logger.info("Tenant analysis started filename=%s", filename)
        tenant_analysis = await self._pricing_agent_client.analyze_tenant_file(
            file=file,
            filename=filename,
            content_type=content_type,
        )
        final_quotes: list[dict[str, Any]] = []
        failures: list[str] = []
        logger.info(
            "Tenant analysis completed filename=%s products=%d",
            filename,
            len(tenant_analysis["licenses"]),
        )

        for product_index, license_info in enumerate(tenant_analysis["licenses"], start=1):
            product_title = license_info["product_title"]
            target_quantity = license_info["assigned_licenses"]
            logger.info(
                "Product quote analysis started product=%d/%d title=%s target_quantity=%d",
                product_index,
                len(tenant_analysis["licenses"]),
                product_title,
                target_quantity,
            )
            try:
                analysis = await self._pricing_agent_client.analyze_and_quote(
                    file=file,
                    filename=filename,
                    content_type=content_type,
                    product_query=product_title,
                    target_quantity=target_quantity,
                )
            except PricingAgentAPIError as error:
                if error.quote_selection is None:
                    failures.append(f"{product_title}: quote analysis failed")
                    logger.exception("Quote analysis failed for %s", product_title)
                    continue

                logger.info(
                    "Product quote selection required title=%s options=%d",
                    product_title,
                    len(error.quote_selection.detail.available_options),
                )
                for option in error.quote_selection.detail.available_options:
                    try:
                        logger.info(
                            "Selected quote analysis started title=%s sku_id=%s term=%s billing=%s",
                            product_title,
                            option.sku_id,
                            option.term_duration,
                            option.billing_plan,
                        )
                        selected_analysis = await self._pricing_agent_client.analyze_and_quote(
                            file=file,
                            filename=filename,
                            content_type=content_type,
                            product_query=product_title,
                            target_quantity=target_quantity,
                            product_id=option.product_id,
                            sku_id=option.sku_id,
                            term_duration=option.term_duration,
                            billing_plan=option.billing_plan,
                        )
                        final_quote = selected_analysis.final_quote.model_dump(mode="json")
                        final_quote["_requested_product"] = product_title
                        final_quotes.append(final_quote)
                        logger.info(
                            "Selected quote analysis completed title=%s sku_id=%s",
                            product_title,
                            option.sku_id,
                        )
                    except PricingAgentAPIError as selection_error:
                        failures.append(
                            f"{product_title} ({option.sku_id}, {option.term_duration}, "
                            f"{option.billing_plan}): final quote failed"
                        )
                        logger.error(
                            "Selected quote failed for %s: status=%s response=%s cause=%r",
                            product_title,
                            selection_error.status_code,
                            selection_error.response_body,
                            selection_error.__cause__,
                        )
                continue

            # The API can return a final quote directly when no selection is required.
            final_quote = analysis.final_quote.model_dump(mode="json")
            final_quote["_requested_product"] = product_title
            final_quotes.append(final_quote)
            logger.info("Product quote analysis completed title=%s", product_title)

        logger.info(
            "Document quote generation completed filename=%s quotes=%d failures=%d",
            filename,
            len(final_quotes),
            len(failures),
        )
        return QuoteResults(
            filename=filename,
            product_count=len(tenant_analysis["licenses"]),
            final_quotes=final_quotes,
            failures=failures,
        )

    def _create_quote_session(self, sender: str, results: QuoteResults) -> str:
        self._remove_expired_sessions()
        session_id = secrets.token_urlsafe(8)
        self._quote_sessions[session_id] = QuoteSession(
            sender=sender,
            results=results,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
        get_running_loop().call_later(30 * 60, self._expire_quote_session, session_id)
        logger.info(
            "Quote session created session_id=%s products=%d quotes=%d",
            session_id,
            results.product_count,
            len(results.final_quotes),
        )
        return session_id

    def _expire_quote_session(self, session_id: str) -> None:
        if self._quote_sessions.pop(session_id, None) is not None:
            logger.info("Quote session expired session_id=%s", session_id)

    def _get_quote_session(self, sender: str, session_id: str) -> QuoteSession | None:
        self._remove_expired_sessions()
        session = self._quote_sessions.get(session_id)
        if session is None or session.sender != sender:
            return None
        return session

    def _remove_expired_sessions(self) -> None:
        now = datetime.now(UTC)
        expired = [
            session_id
            for session_id, session in self._quote_sessions.items()
            if session.expires_at <= now
        ]
        for session_id in expired:
            del self._quote_sessions[session_id]
        if expired:
            logger.info("Expired quote sessions removed count=%d", len(expired))

    async def _handle_interactive_reply(self, sender: str, reply_id: str) -> None:
        logger.info("Interactive reply received reply_id=%s", reply_id)
        parts = reply_id.split("|")
        if len(parts) < 4 or parts[0] != "quote":
            await self._send_text(sender, "That selection is not recognized. Please try again.")
            return

        session_id, action = parts[1], parts[2]
        session = self._get_quote_session(sender, session_id)
        if session is None:
            await self._send_text(
                sender,
                "This quote selection has expired. Please upload the tenant document again.",
            )
            return

        try:
            indexes = [int(value) for value in parts[3:]]
            sent = False
            if action == "products" and len(indexes) == 1:
                sent = await self._send_product_list(sender, session_id, indexes[0])
            elif action == "product" and len(indexes) == 1:
                sent = await self._send_variant_list(sender, session_id, indexes[0], 0)
            elif action == "variants" and len(indexes) == 2:
                sent = await self._send_variant_list(
                    sender, session_id, indexes[0], indexes[1]
                )
            elif action == "variant" and len(indexes) == 2:
                sent = await self._send_option_list(
                    sender, session_id, indexes[0], indexes[1], 0
                )
            elif action == "options" and len(indexes) == 3:
                sent = await self._send_option_list(
                    sender, session_id, indexes[0], indexes[1], indexes[2]
                )
            elif action == "option" and len(indexes) == 3:
                sent = await self._send_quote_detail(
                    sender, session_id, indexes[0], indexes[1], indexes[2]
                )
            else:
                raise ValueError("unsupported quote action")
            if not sent:
                await self._send_text(
                    sender, "I could not display that quote selection. Please try again."
                )
        except (IndexError, ValueError):
            logger.warning("Invalid interactive quote selection reply_id=%s", reply_id)
            await self._send_text(sender, "That quote option is no longer available.")

    @staticmethod
    def _grouped_quotes(
        results: QuoteResults,
    ) -> list[tuple[str, list[dict[str, Any]]]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for quote in results.final_quotes:
            product = quote.get("_requested_product")
            if not isinstance(product, str):
                product = str(quote.get("sku_title", "Product"))
            groups.setdefault(product, []).append(quote)
        return list(groups.items())

    @staticmethod
    def _variant_groups(
        quotes: list[dict[str, Any]],
    ) -> list[tuple[str, list[dict[str, Any]]]]:
        variants: dict[str, list[dict[str, Any]]] = {}
        for quote in quotes:
            title = str(quote.get("sku_title", "Pricing option"))
            variants.setdefault(title, []).append(quote)
        return list(variants.items())

    async def _send_product_list(
        self, sender: str, session_id: str, page: int
    ) -> bool:
        session = self._get_quote_session(sender, session_id)
        if session is None:
            return False
        products = self._grouped_quotes(session.results)
        rows = self._paged_rows(
            items=products,
            page=page,
            row_factory=lambda index, item: InteractiveRow(
                id=f"quote|{session_id}|product|{index}",
                title=self._short_text(item[0], 24),
                description=self._short_text(
                    f"{item[1][0]['target_quantity']} requested, "
                    f"{item[1][0]['existing_quantity']} existing, {len(item[1])} options",
                    72,
                ),
            ),
            previous_id=f"quote|{session_id}|products|{page - 1}",
            next_id=f"quote|{session_id}|products|{page + 1}",
        )
        body = (
            f"Quote ready for {session.results.filename}\n"
            f"{session.results.product_count} products and "
            f"{len(session.results.final_quotes)} pricing options.\n\n"
            "Select a product to review."
        )
        return await self._send_interactive_list(sender, body, "View products", rows)

    async def _send_variant_list(
        self, sender: str, session_id: str, product_index: int, page: int
    ) -> bool:
        session = self._get_quote_session(sender, session_id)
        if session is None:
            return False
        product, quotes = self._grouped_quotes(session.results)[product_index]
        variants = self._variant_groups(quotes)
        variant_titles = self._edition_row_titles(
            product, [variant[0] for variant in variants]
        )
        rows = self._paged_rows(
            items=variants,
            page=page,
            row_factory=lambda index, item: InteractiveRow(
                id=f"quote|{session_id}|variant|{product_index}|{index}",
                title=variant_titles[index],
                description=self._short_text(item[0], 72),
            ),
            previous_id=f"quote|{session_id}|variants|{product_index}|{page - 1}",
            next_id=f"quote|{session_id}|variants|{product_index}|{page + 1}",
        )
        body = (
            f"{product}\n"
            f"{quotes[0]['target_quantity']} requested | "
            f"{quotes[0]['existing_quantity']} existing\n\n"
            "Select a license edition."
        )
        return await self._send_interactive_list(sender, body, "View editions", rows)

    async def _send_option_list(
        self,
        sender: str,
        session_id: str,
        product_index: int,
        variant_index: int,
        page: int,
    ) -> bool:
        session = self._get_quote_session(sender, session_id)
        if session is None:
            return False
        _, product_quotes = self._grouped_quotes(session.results)[product_index]
        variant, quotes = self._variant_groups(product_quotes)[variant_index]
        term_labels = {"P1M": "1 month", "P1Y": "1 year", "P3Y": "3 years"}

        def option_row(index: int, quote: dict[str, Any]) -> InteractiveRow:
            term = term_labels.get(
                str(quote["term_duration"]), str(quote["term_duration"])
            )
            billing = str(quote["billing_plan"])
            title = f"{term} - {billing}"
            description = f"INR {self._formatted_amount(quote['total_quote_amount'])} total"
            promo = self._promo_text(quote, compact=True)
            if promo:
                description += f" | {promo}"
            return InteractiveRow(
                id=f"quote|{session_id}|option|{product_index}|{variant_index}|{index}",
                title=self._short_text(title, 24),
                description=self._short_text(description, 72),
            )

        rows = self._paged_rows(
            items=quotes,
            page=page,
            row_factory=option_row,
            previous_id=(
                f"quote|{session_id}|options|{product_index}|{variant_index}|{page - 1}"
            ),
            next_id=(
                f"quote|{session_id}|options|{product_index}|{variant_index}|{page + 1}"
            ),
        )
        return await self._send_interactive_list(
            sender,
            f"{variant}\n\nSelect a term and billing plan.",
            "View pricing",
            rows,
        )

    async def _send_quote_detail(
        self,
        sender: str,
        session_id: str,
        product_index: int,
        variant_index: int,
        quote_index: int,
    ) -> bool:
        session = self._get_quote_session(sender, session_id)
        if session is None:
            return False
        _, product_quotes = self._grouped_quotes(session.results)[product_index]
        variant, quotes = self._variant_groups(product_quotes)[variant_index]
        quote = quotes[quote_index]
        term_labels = {"P1M": "1 month", "P1Y": "1 year", "P3Y": "3 years"}
        term = term_labels.get(str(quote["term_duration"]), str(quote["term_duration"]))
        regular_unit_price = quote.get(
            "initial_quote_without_promo", quote["total_quote_amount"]
        )
        body = (
            f"*{variant}*\n\n"
            f"Quantity: {quote['target_quantity']}\n"
            f"Existing licenses: {quote['existing_quantity']}\n"
            f"Term: {term}\n"
            f"Billing: {quote['billing_plan']}\n"
            f"Regular unit price: INR {self._formatted_amount(regular_unit_price)}"
        )
        promo_unit_price = quote.get("initial_quote_with_promo")
        if promo_unit_price is not None:
            body += (
                f"\nPromo unit price: INR {self._formatted_amount(promo_unit_price)}"
            )
        else:
            body += "\nPromo unit price: Not available"
        if self._promo_is_applied(quote):
            try:
                savings = (
                    Decimal(str(regular_unit_price))
                    * Decimal(str(quote["target_quantity"]))
                    - Decimal(str(quote["total_quote_amount"]))
                )
            except (InvalidOperation, ValueError):
                savings = Decimal(0)
            if savings > 0:
                body += f"\nTotal savings: INR {self._formatted_amount(savings)}"
            promo_quantity = quote.get("promo_quantity")
            if isinstance(promo_quantity, int) and promo_quantity > 0:
                body += f"\nPromo applied to: {promo_quantity} licenses"
        body += f"\nFinal total: *INR {self._formatted_amount(quote['total_quote_amount'])}*"
        promo = self._promo_text(quote)
        if promo:
            body += f"\n{promo}"
        buttons = [
            InteractiveButton(
                reply=InteractiveReply(
                    id=f"quote|{session_id}|variant|{product_index}|{variant_index}",
                    title="Other prices",
                )
            ),
            InteractiveButton(
                reply=InteractiveReply(
                    id=f"quote|{session_id}|products|0",
                    title="Other products",
                )
            ),
        ]
        return await self._send_interactive_buttons(sender, body, buttons)

    async def _send_interactive_list(
        self, sender: str, body: str, button: str, rows: list[InteractiveRow]
    ) -> bool:
        message = WhatsAppInteractiveMessage(
            to=sender,
            interactive=InteractiveList(
                body=InteractiveText(text=self._short_text(body, 1024)),
                footer=InteractiveText(text="Selections expire after 30 minutes"),
                action=InteractiveListAction(
                    button=button,
                    sections=[InteractiveSection(rows=rows)],
                ),
            ),
        )
        return await self._send_interactive(message)

    async def _send_interactive_buttons(
        self, sender: str, body: str, buttons: list[InteractiveButton]
    ) -> bool:
        message = WhatsAppInteractiveMessage(
            to=sender,
            interactive=InteractiveButtons(
                body=InteractiveText(text=self._short_text(body, 1024)),
                footer=InteractiveText(text="Prices shown in INR"),
                action=InteractiveButtonAction(buttons=buttons),
            ),
        )
        return await self._send_interactive(message)

    async def _send_interactive(self, message: WhatsAppInteractiveMessage) -> bool:
        if not getattr(self._whatsapp_client, "credentials_valid", True):
            logger.error(
                "WhatsApp interactive send skipped because authentication is invalid; "
                "replace WHATSAPP_ACCESS_TOKEN and restart"
            )
            return False
        try:
            await self._whatsapp_client.send_message(message)
            logger.info("WhatsApp interactive message sent type=%s", message.interactive.type)
            return True
        except WhatsAppAPIError as error:
            logger.error(
                "Unable to send WhatsApp interactive response status=%s response=%s cause=%r",
                error.status_code,
                error.response_body,
                error.__cause__,
            )
            return False

    @staticmethod
    def _paged_rows(
        items: list[Any],
        page: int,
        row_factory: Any,
        previous_id: str,
        next_id: str,
    ) -> list[InteractiveRow]:
        page_size = 8
        start = page * page_size
        if page < 0 or start >= len(items):
            raise IndexError("page out of range")
        rows = [
            row_factory(index, item)
            for index, item in enumerate(
                items[start : start + page_size], start=start
            )
        ]
        if page > 0:
            rows.append(InteractiveRow(id=previous_id, title="Previous page"))
        if start + page_size < len(items):
            rows.append(InteractiveRow(id=next_id, title="Next page"))
        return rows

    @staticmethod
    def _short_text(value: object, limit: int) -> str:
        text = str(value)
        return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."

    @staticmethod
    def _distinct_row_titles(names: list[str]) -> list[str]:
        titles = [WhatsAppWebhookService._short_text(name, 24) for name in names]
        collisions = {title for title in titles if titles.count(title) > 1}
        if not collisions:
            return titles

        for index, (name, title) in enumerate(zip(names, titles, strict=True)):
            if title not in collisions:
                continue
            suffix = name[-24:] if len(name) > 24 else name
            titles[index] = suffix.lstrip(" -")

        # A numeric prefix guarantees uniqueness when the meaningful suffixes also match.
        for title in set(titles):
            duplicate_indexes = [
                index for index, candidate in enumerate(titles) if candidate == title
            ]
            if len(duplicate_indexes) > 1:
                for sequence, index in enumerate(duplicate_indexes, start=1):
                    titles[index] = f"{sequence}. {title}"[:24]
        return titles

    @staticmethod
    def _edition_row_titles(product: str, editions: list[str]) -> list[str]:
        labels: list[str] = []
        normalized_product = product.strip().casefold()
        for edition in editions:
            normalized_edition = edition.strip().casefold()
            if normalized_edition == normalized_product:
                labels.append("Standard")
                continue

            if normalized_edition.startswith(normalized_product):
                suffix = edition.strip()[len(product.strip()) :].strip(" -():")
                if suffix:
                    suffix = suffix[0].upper() + suffix[1:]
                    labels.append(WhatsAppWebhookService._short_text(suffix, 24))
                    continue

            labels.append(WhatsAppWebhookService._short_text(edition, 24))

        if len(set(labels)) == len(labels):
            return labels
        return WhatsAppWebhookService._distinct_row_titles(editions)

    @staticmethod
    def _command_type(body: str) -> CommandType | None:
        command = body.strip().split(maxsplit=1)[0].lower() if body.strip() else ""
        return COMMAND_ALIASES.get(command)

    @staticmethod
    def _quote_parameters(
        command: str, prefix: str, include_existing: bool
    ) -> dict[str, str | int | None] | None:
        normalized_command = command.strip()
        if not normalized_command.lower().startswith(prefix):
            return None

        fields = [field.strip() for field in normalized_command[len(prefix) :].split("|")]
        required_fields = 3 if include_existing else 2
        if len(fields) < required_fields or not all(fields[:required_fields]):
            return None

        try:
            parameters: dict[str, str | int | None] = {
                "product_query": fields[0],
                "target_quantity": int(fields[1]),
            }
            if include_existing:
                parameters["existing_quantity"] = int(fields[2])
        except ValueError:
            return None

        if parameters["target_quantity"] <= 0 or (
            include_existing and parameters["existing_quantity"] < 0
        ):
            return None

        optional_names = ("product_id", "sku_id", "term_duration", "billing_plan")
        optional_fields = fields[3:] if include_existing else fields[2:]
        parameters.update(
            {
                name: value or None
                for name, value in zip(optional_names, optional_fields, strict=False)
            }
        )
        return parameters

    @staticmethod
    def _response_text(agent_response: dict[str, Any]) -> str:
        tenant_analysis = WhatsAppWebhookService._tenant_analysis_text(agent_response)
        if tenant_analysis is not None:
            return tenant_analysis

        for key in ("response", "message", "answer"):
            value = agent_response.get(key)
            if isinstance(value, str) and value.strip():
                return value[:4096]

        string_values = [
            value for value in agent_response.values() if isinstance(value, str) and value.strip()
        ]
        if len(string_values) == 1:
            return string_values[0][:4096]

        return json.dumps(agent_response, ensure_ascii=True, indent=2)[:4096]

    @staticmethod
    def _tenant_analysis_text(agent_response: dict[str, Any]) -> str | None:
        licenses = agent_response.get("licenses")
        if not isinstance(licenses, list):
            return None

        source_file = agent_response.get("source_file")
        total_products = agent_response.get("total_products_detected")
        commercial_products = agent_response.get("commercial_products_detected")
        excluded_products = agent_response.get("excluded_products_detected")
        active = agent_response.get("total_active_commercial_licenses")
        assigned = agent_response.get("total_assigned_commercial_licenses")
        if not all(isinstance(value, int) for value in (total_products, commercial_products, excluded_products, active, assigned)):
            return None

        utilisation = (assigned / active * 100) if active else 0
        lines = [
            "Tenant license analysis",
            f"File: {source_file}" if isinstance(source_file, str) else "File analyzed",
            f"Products: {total_products} detected, {commercial_products} commercial, {excluded_products} excluded",
            f"Licenses: {assigned:,}/{active:,} assigned ({utilisation:.1f}% utilization)",
        ]

        over_assigned: list[str] = []
        no_capacity: list[str] = []
        for license_info in licenses:
            if not isinstance(license_info, dict):
                continue
            title = license_info.get("product_title")
            assigned_licenses = license_info.get("assigned_licenses")
            active_licenses = license_info.get("active_licenses")
            available_licenses = license_info.get("available_licenses")
            if not isinstance(title, str) or not isinstance(assigned_licenses, int):
                continue
            if isinstance(active_licenses, int) and assigned_licenses > active_licenses:
                over_assigned.append(title)
            elif available_licenses == 0:
                no_capacity.append(title)

        if over_assigned:
            lines.append("Over-assigned: " + ", ".join(over_assigned))
        if no_capacity:
            lines.append("No available licenses: " + ", ".join(no_capacity))

        return "\n".join(lines)[:4096]

    @staticmethod
    def _final_quotes_text(
        filename: str,
        product_count: int,
        final_quotes: list[dict[str, Any]],
        failures: list[str],
    ) -> str:
        grouped_quotes: dict[str, list[dict[str, Any]]] = {}
        for quote in final_quotes:
            requested_product = quote.get("_requested_product")
            if not isinstance(requested_product, str):
                requested_product = str(quote.get("sku_title", "Product"))
            grouped_quotes.setdefault(requested_product, []).append(quote)

        lines = [
            "*Final quote summary*",
            f"File: {filename}",
            f"{product_count} products | {len(final_quotes)} pricing options",
        ]

        term_labels = {"P1M": "1 month", "P1Y": "1 year", "P3Y": "3 years"}
        for requested_product, quotes in grouped_quotes.items():
            first_quote = quotes[0]
            lines.extend(
                [
                    "",
                    f"*{requested_product}*",
                    f"{first_quote['target_quantity']} requested | "
                    f"{first_quote['existing_quantity']} existing | "
                    f"{len(quotes)} options",
                ]
            )
            for index, quote in enumerate(quotes, start=1):
                term = term_labels.get(
                    str(quote["term_duration"]), str(quote["term_duration"])
                )
                billing = str(quote["billing_plan"])
                billing_label = (
                    "billing unavailable" if billing.lower() == "none" else billing.lower()
                )
                if WhatsAppWebhookService._promo_is_applied(quote):
                    promo_amount = WhatsAppWebhookService._formatted_amount(
                        quote.get("initial_quote_with_promo", quote["total_quote_amount"])
                    )
                    regular_amount = WhatsAppWebhookService._formatted_amount(
                        quote.get(
                            "initial_quote_without_promo", quote["total_quote_amount"]
                        )
                    )
                    price_text = f"INR {promo_amount} (regular INR {regular_amount})"
                else:
                    price_text = (
                        f"INR {WhatsAppWebhookService._formatted_amount(quote['total_quote_amount'])}"
                    )
                promo = WhatsAppWebhookService._promo_text(quote, compact=True)
                promo_suffix = f" | {promo}" if promo else ""
                lines.append(
                    f"{index}. {quote['sku_title']} - {term}, {billing_label} - "
                    f"{price_text}{promo_suffix}"
                )

        if failures:
            lines.extend(("", f"*Could not generate {len(failures)} options*", *failures))
        if not final_quotes and not failures:
            lines.extend(("", "No quotes could be generated for the analyzed licenses."))

        return "\n".join(lines)

    @staticmethod
    def _formatted_amount(value: object) -> str:
        try:
            return f"{Decimal(str(value)):,.2f}"
        except (InvalidOperation, ValueError):
            return str(value)

    @staticmethod
    def _promo_text(quote: dict[str, Any], compact: bool = False) -> str | None:
        percentage = WhatsAppWebhookService._promo_percentage(quote)
        if percentage <= 0:
            return None

        percentage_text = format(percentage.normalize(), "f")
        applied = WhatsAppWebhookService._promo_is_applied(quote)
        if applied:
            formatted_percentage = f"{percentage_text}% promo"
        else:
            existing_quantity = quote.get("existing_quantity")
            formatted_percentage = (
                f"{percentage_text}% promo unavailable ({existing_quantity} existing)"
                if isinstance(existing_quantity, int) and existing_quantity > 0
                else f"{percentage_text}% promo unavailable"
            )
        promo_code = quote.get("promo_code") or quote.get("promotion_code")
        if isinstance(promo_code, str) and promo_code.strip():
            return (
                f"{formatted_percentage} - code {promo_code.strip()}"
                if compact
                else (
                    f"Promotion: {percentage_text}% off"
                    + (
                        " (applied)"
                        if applied
                        else WhatsAppWebhookService._promo_unavailable_reason(quote)
                    )
                    + f"\nPromo code: *{promo_code.strip()}*"
                )
            )
        return (
            f"{formatted_percentage} - automatic"
            if compact and applied
            else (
                formatted_percentage
                if compact
                else (
                    f"Promotion: {percentage_text}% off (applied automatically)"
                    if applied
                    else (
                        f"Promotion available: {percentage_text}% off\n"
                        f"Promo status: Not applied"
                        f"{WhatsAppWebhookService._promo_unavailable_reason(quote)}"
                    )
                )
            )
        )

    @staticmethod
    def _promo_unavailable_reason(quote: dict[str, Any]) -> str:
        existing_quantity = quote.get("existing_quantity")
        promo_quantity = quote.get("promo_quantity")
        if (
            isinstance(existing_quantity, int)
            and existing_quantity > 0
            and promo_quantity == 0
        ):
            return (
                f" - API returned 0 promo-eligible licenses "
                f"({existing_quantity} existing licenses)"
            )
        return " - API returned 0 promo-eligible licenses"

    @staticmethod
    def _promo_percentage(quote: dict[str, Any]) -> Decimal:
        regular_price = quote.get("initial_quote_without_promo")
        promo_price = quote.get("initial_quote_with_promo")
        try:
            regular = Decimal(str(regular_price))
            promo = Decimal(str(promo_price))
            if regular > 0 and 0 <= promo < regular:
                return ((regular - promo) / regular * 100).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            pass

        try:
            percentage = Decimal(str(quote.get("promo_percentage", 0)))
        except (InvalidOperation, ValueError):
            return Decimal(0)
        return percentage * 100 if 0 < percentage < 1 else percentage

    @staticmethod
    def _has_promo_offer(quote: dict[str, Any]) -> bool:
        return WhatsAppWebhookService._promo_percentage(quote) > 0

    @staticmethod
    def _promo_is_applied(quote: dict[str, Any]) -> bool:
        promo_quantity = quote.get("promo_quantity")
        if isinstance(promo_quantity, int):
            return promo_quantity > 0
        try:
            return Decimal(str(quote.get("promo_amount", 0))) > 0
        except (InvalidOperation, ValueError):
            return False

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

    @staticmethod
    def _quote_selection_text(error: PricingAgentAPIError) -> str | None:
        if error.quote_selection is None:
            return None

        detail = error.quote_selection.detail
        lines = [
            detail.message,
            "",
            "Re-upload with the same query and quantity, followed by one option:",
        ]
        for option in detail.available_options:
            lines.append(
                f"{option.product_id} | {option.sku_id} | {option.term_duration} | "
                f"{option.billing_plan}"
                f" - {option.sku_title}"
            )
        return "\n".join(lines)[:4096]
