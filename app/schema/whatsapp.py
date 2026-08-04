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


class InteractiveText(BaseModel):
    text: str


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
    footer: InteractiveText | None = None
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
    footer: InteractiveText | None = None
    action: InteractiveButtonAction


class WhatsAppInteractiveMessage(BaseModel):
    messaging_product: Literal["whatsapp"] = "whatsapp"
    recipient_type: Literal["individual"] = "individual"
    to: str
    type: Literal["interactive"] = "interactive"
    interactive: InteractiveList | InteractiveButtons


class IncomingWhatsAppDocument(BaseModel):
    id: str
    filename: str = "document"
    mime_type: str = "application/octet-stream"
    caption: str | None = None


class IncomingInteractiveReply(BaseModel):
    id: str
    title: str
    description: str | None = None


class IncomingInteractive(BaseModel):
    type: str
    list_reply: IncomingInteractiveReply | None = None
    button_reply: IncomingInteractiveReply | None = None


class IncomingWhatsAppMessage(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    sender: str = Field(alias="from")
    type: str
    text: TextContent | None = None
    document: IncomingWhatsAppDocument | None = None
    interactive: IncomingInteractive | None = None


class WhatsAppWebhookValue(BaseModel):
    messages: list[IncomingWhatsAppMessage] = Field(default_factory=list)


class WhatsAppWebhookChange(BaseModel):
    value: WhatsAppWebhookValue


class WhatsAppWebhookEntry(BaseModel):
    changes: list[WhatsAppWebhookChange] = Field(default_factory=list)


class WhatsAppWebhookPayload(BaseModel):
    object: str
    entry: list[WhatsAppWebhookEntry] = Field(default_factory=list)
