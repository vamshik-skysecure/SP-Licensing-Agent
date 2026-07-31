from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TextContent(BaseModel):
    body: str
    preview_url: bool = False


class WhatsAppTextMessage(BaseModel):
    messaging_product: Literal["whatsapp"] = "whatsapp"
    recipient_type: Literal["individual"] = "individual"
    to: str = Field(..., description="Recipient phone number with country code")
    type: Literal["text"] = "text"
    text: TextContent


class IncomingWhatsAppDocument(BaseModel):
    id: str
    filename: str = "document"
    mime_type: str = "application/octet-stream"
    caption: str | None = None


class IncomingWhatsAppMessage(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    sender: str = Field(alias="from")
    type: str
    text: TextContent | None = None
    document: IncomingWhatsAppDocument | None = None


class WhatsAppWebhookValue(BaseModel):
    messages: list[IncomingWhatsAppMessage] = Field(default_factory=list)


class WhatsAppWebhookChange(BaseModel):
    value: WhatsAppWebhookValue


class WhatsAppWebhookEntry(BaseModel):
    changes: list[WhatsAppWebhookChange] = Field(default_factory=list)


class WhatsAppWebhookPayload(BaseModel):
    object: str
    entry: list[WhatsAppWebhookEntry] = Field(default_factory=list)
