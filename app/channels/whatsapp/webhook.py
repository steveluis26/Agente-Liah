"""Webhook de Meta WhatsApp Cloud API (Fase 0).

GET  /webhook/whatsapp  -> verificación del token de suscripción (hub.challenge).
POST /webhook/whatsapp  -> recibe mensajes, resuelve tenant vía phone_number_id,
                           crea/actualiza contact e inserta mensaje inbound inmutable.
"""
import json
import logging

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.whatsapp.security import verify_meta_signature
from app.core.config import get_settings
from app.core.db import get_session
from app.core.tenant_ctx import clear_tenant_id, set_tenant_id
from app.models import Contact, Message, Tenant, WhatsappChannel

settings = get_settings()
logger = logging.getLogger("whatsapp.webhook")
router = APIRouter(prefix="/webhook/whatsapp", tags=["whatsapp"])


@router.get("")
async def verify(
    hub_mode: str | None = Query(None, alias="hub.mode"),
    hub_challenge: str | None = Query(None, alias="hub.challenge"),
    hub_verify_token: str | None = Query(None, alias="hub.verify_token"),
):
    if hub_mode == "subscribe" and hub_verify_token == settings.whatsapp_verify_token:
        return Response(content=hub_challenge or "", media_type="text/plain")
    return Response(status_code=status.HTTP_403_FORBIDDEN)


@router.post("")
async def receive(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    raw = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")

    if not verify_meta_signature(raw, signature):
        logger.warning("Webhook rechazado: firma inválida")
        return Response(status_code=status.HTTP_403_FORBIDDEN)

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return Response(status_code=status.HTTP_400_BAD_REQUEST)

    if payload.get("object") != "whatsapp_business_account":
        return Response(status_code=status.HTTP_400_BAD_REQUEST)

    # Extraer mensajes y resolver tenant por phone_number_id
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value") or {}
            phone_number_id = value.get("metadata", {}).get("phone_number_id")
            if not phone_number_id:
                continue
            channel = await _resolve_channel(session, phone_number_id)
            if channel is None:
                logger.warning("phone_number_id %s sin tenant", phone_number_id)
                continue
            set_tenant_id(channel.tenant_id)
            for msg in value.get("messages", []):
                await _persist_message(session, channel.tenant_id, msg)
            clear_tenant_id()

    return Response(status_code=status.HTTP_200_OK)


async def _resolve_channel(session: AsyncSession, phone_number_id: str) -> WhatsappChannel | None:
    result = await session.execute(
        select(WhatsappChannel).where(WhatsappChannel.phone_number_id == phone_number_id)
    )
    return result.scalar_one_or_none()


async def _persist_message(session: AsyncSession, tenant_id, msg: dict) -> None:
    wa_id = msg.get("from")
    contact = await session.execute(
        select(Contact).where(Contact.tenant_id == tenant_id, Contact.wa_id == wa_id)
    )
    contact = contact.scalar_one_or_none()
    if contact is None:
        contact = Contact(tenant_id=tenant_id, wa_id=wa_id)
        session.add(contact)
        await session.flush()
    else:
        contact.last_interaction_at = __import__("datetime").datetime.utcnow()

    body = ""
    if msg.get("type") == "text":
        body = (msg.get("text") or {}).get("body", "")

    session.add(
        Message(
            tenant_id=tenant_id,
            contact_id=contact.id,
            direction="inbound",
            content=body,
            meta_message_id=msg.get("id"),
        )
    )
    await session.commit()
