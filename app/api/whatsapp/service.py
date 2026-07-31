import json
from collections import deque
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from app.config import get_logger
from app.core.agent.main import PricingAgentAPIError, PricingAgentClient
from app.core.whatsapp import WhatsAppAPIError, WhatsAppClient
from app.schema.whatsapp import (
    IncomingWhatsAppDocument,
    TextContent,
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
        except WhatsAppAPIError:
            logger.exception("Unable to download WhatsApp document %s", document.id)
            await self._send_text(sender, "I could not download that document. Please try again.")
            return
        logger.info("Document downloaded filename=%s bytes=%d", media.filename, len(media.content))

        try:
            response_body = await self._document_quote_text(
                file=media.content,
                filename=media.filename,
                content_type=media.content_type,
            )
        except PricingAgentAPIError as error:
            logger.exception("Pricing agent failed while processing document %s", document.id)
            response_body = self._quote_selection_text(error) or (
                "I could not process that document right now. Please try again."
            )

        await self._send_text_chunks(sender, response_body)
        logger.info("Document processing completed filename=%s", media.filename)

    async def _send_text(self, sender: str, body: str) -> None:
        logger.info("Sending WhatsApp text characters=%d", len(body))
        try:
            await self._whatsapp_client.send_message(
                WhatsAppTextMessage(
                    to=sender,
                    text=TextContent(body=body),
                )
            )
            logger.info("WhatsApp text sent")
        except WhatsAppAPIError:
            logger.exception("Unable to send WhatsApp response to %s", sender)

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
        return self._final_quotes_text(
            filename=filename,
            product_count=len(tenant_analysis["licenses"]),
            final_quotes=final_quotes,
            failures=failures,
        )

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
                amount = WhatsAppWebhookService._formatted_amount(
                    quote["total_quote_amount"]
                )
                lines.append(
                    f"{index}. {quote['sku_title']} - {term}, {billing_label} - {amount}"
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
