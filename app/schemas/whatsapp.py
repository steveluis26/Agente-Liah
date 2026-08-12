"""Modelos Pydantic del webhook de Meta WhatsApp Cloud API.

Documentación del payload entrante de Meta:
https://developers.facebook.com/docs/whatsapp/cloud-api/webhooks/payloads
"""
from typing import Any, Literal

from pydantic import BaseModel, Field


class WhatsappText(BaseModel):
    body: str


class WhatsappMessage(BaseModel):
    from_: str = Field(alias="from")
    id: str
    type: str
    text: WhatsappText | None = None
    timestamp: str | None = None


class WhatsappMetadata(BaseModel):
    display_phone_number: str
    phone_number_id: str


class WhatsappValue(BaseModel):
    messaging_product: Literal["whatsapp"]
    metadata: WhatsappMetadata
    contacts: list[Any] = Field(default_factory=list)
    messages: list[WhatsappMessage] = Field(default_factory=list)


class WhatsappChange(BaseModel):
    field: str
    value: WhatsappValue | None = None


class WhatsappEntry(BaseModel):
    id: str
    changes: list[WhatsappChange] = Field(default_factory=list)


class WhatsappWebhookPayload(BaseModel):
    object: Literal["whatsapp_business_account"]
    entry: list[WhatsappEntry] = Field(default_factory=list)
