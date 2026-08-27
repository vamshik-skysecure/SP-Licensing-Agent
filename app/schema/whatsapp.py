from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TextContent(BaseModel):
    body: str = Field(min_length=1, max_length=32_768)
    preview_url: bool = False


class WhatsAppTextMessage(BaseModel):
    messaging_product: Literal["whatsapp"] = "whatsapp"
    recipient_type: Literal["individual"] = "individual"
    to: str = Field(..., description="Recipient phone number with country code")
    type: Literal["text"] = "text"
    text: TextContent


class DocumentContent(BaseModel):
    id: str
    filename: str
    caption: str | None = None


class WhatsAppDocumentMessage(BaseModel):
    messaging_product: Literal["whatsapp"] = "whatsapp"
    recipient_type: Literal["individual"] = "individual"
    to: str
    type: Literal["document"] = "document"
    document: DocumentContent


class ImageContent(BaseModel):
    id: str
    caption: str | None = None


class WhatsAppImageMessage(BaseModel):
    messaging_product: Literal["whatsapp"] = "whatsapp"
    recipient_type: Literal["individual"] = "individual"
    to: str
    type: Literal["image"] = "image"
    image: ImageContent


class InteractiveText(BaseModel):
    text: str = Field(max_length=1024)


class InteractiveFooter(BaseModel):
    text: str = Field(max_length=60)


class InteractiveRow(BaseModel):
    id: str = Field(max_length=200)
    title: str = Field(max_length=24)
    description: str | None = Field(default=None, max_length=72)


class InteractiveSection(BaseModel):
    title: str | None = Field(default=None, max_length=24)
    rows: list[InteractiveRow] = Field(min_length=1, max_length=10)


class InteractiveListAction(BaseModel):
    button: str = Field(max_length=20)
    sections: list[InteractiveSection] = Field(min_length=1)


class InteractiveList(BaseModel):
    type: Literal["list"] = "list"
    body: InteractiveText
    footer: InteractiveFooter | None = None
    action: InteractiveListAction


class InteractiveReply(BaseModel):
    id: str = Field(max_length=200)
    title: str = Field(max_length=20)


class InteractiveButton(BaseModel):
    type: Literal["reply"] = "reply"
    reply: InteractiveReply


class InteractiveButtonAction(BaseModel):
    buttons: list[InteractiveButton] = Field(min_length=1, max_length=3)


class InteractiveButtons(BaseModel):
    type: Literal["button"] = "button"
    body: InteractiveText
    footer: InteractiveFooter | None = None
    action: InteractiveButtonAction


class WhatsAppInteractiveMessage(BaseModel):
    messaging_product: Literal["whatsapp"] = "whatsapp"
    recipient_type: Literal["individual"] = "individual"
    to: str
    type: Literal["interactive"] = "interactive"
    interactive: InteractiveList | InteractiveButtons


class IncomingWhatsAppDocument(BaseModel):
    id: str = Field(min_length=1, max_length=512)
    filename: str = Field(default="document", min_length=1, max_length=255)
    mime_type: str = Field(default="application/octet-stream", max_length=128)
    caption: str | None = Field(default=None, max_length=4096)


class IncomingWhatsAppImage(BaseModel):
    id: str = Field(min_length=1, max_length=512)
    mime_type: str = Field(default="image/jpeg", max_length=128)
    caption: str | None = Field(default=None, max_length=4096)


class IncomingWhatsAppAudio(BaseModel):
    id: str = Field(min_length=1, max_length=512)
    mime_type: str = Field(default="audio/ogg", max_length=128)
    voice: bool = False


class IncomingInteractiveReply(BaseModel):
    id: str = Field(min_length=1, max_length=512)
    title: str = Field(max_length=200)
    description: str | None = Field(default=None, max_length=500)


class IncomingInteractive(BaseModel):
    type: str
    list_reply: IncomingInteractiveReply | None = None
    button_reply: IncomingInteractiveReply | None = None


class IncomingWhatsAppMessage(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(min_length=1, max_length=512)
    sender: str = Field(alias="from", min_length=8, max_length=32)
    timestamp: str | None = Field(default=None, max_length=32)
    type: str = Field(max_length=32)
    text: TextContent | None = None
    document: IncomingWhatsAppDocument | None = None
    image: IncomingWhatsAppImage | None = None
    audio: IncomingWhatsAppAudio | None = None
    interactive: IncomingInteractive | None = None


class IncomingWhatsAppMetadata(BaseModel):
    """Meta asset that received an inbound WhatsApp event.

    A webhook signature authenticates the Meta app, not a particular phone-number
    asset.  Keeping the target phone ID in the validated payload lets a single-number
    deployment reject events for other numbers attached to the same Meta app/WABA.
    """

    display_phone_number: str | None = Field(default=None, max_length=32)
    phone_number_id: str = Field(min_length=1, max_length=32, pattern=r"^\d+$")


class WhatsAppWebhookValue(BaseModel):
    metadata: IncomingWhatsAppMetadata | None = None
    messages: list[IncomingWhatsAppMessage] = Field(default_factory=list, max_length=100)


class WhatsAppWebhookChange(BaseModel):
    value: WhatsAppWebhookValue


class WhatsAppWebhookEntry(BaseModel):
    changes: list[WhatsAppWebhookChange] = Field(default_factory=list, max_length=100)


class WhatsAppWebhookPayload(BaseModel):
    object: str = Field(max_length=100)
    entry: list[WhatsAppWebhookEntry] = Field(default_factory=list, max_length=100)
