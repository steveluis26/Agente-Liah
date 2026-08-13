"""Sender de WhatsApp vía Meta Cloud API (/messages).

Soporta texto libre (dentro de ventana 24h) y plantillas HSM (type=template)
para mensajes proactivos (recordatorios). En dev/local sin token real, el
envío se omite (dry-run) pero se registra el mensaje outbound.
"""
import os

import httpx

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Contact, Message, WhatsappChannel


async def send_message(
    session: AsyncSession,
    tenant_id: str,
    contact_id: str,
    contact_wa_id: str,
    text: str,
    dry_run: bool = False,
) -> str | None:
    """Envía texto al contacto y persiste el mensaje outbound.

    Devuelve meta_message_id si se envió; None en dry-run.
    """
    channel = (
        await session.execute(
            select(WhatsappChannel).where(WhatsappChannel.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()

    msg = Message(
        tenant_id=tenant_id,
        contact_id=contact_id,
        direction="outbound",
        content=text,
    )
    session.add(msg)
    await session.commit()
    await session.refresh(msg)

    if dry_run or not channel or not channel.token_secret_ref:
        return None

    phone_number_id = channel.phone_number_id
    token = _resolve_token(channel.token_secret_ref)
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"https://graph.facebook.com/v19.0/{phone_number_id}/messages",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "messaging_product": "whatsapp",
                "to": contact_wa_id,
                "type": "text",
                "text": {"body": text},
            },
        )
        r.raise_for_status()
        meta_id = r.json().get("messages", [{}])[0].get("id")
        msg.meta_message_id = meta_id
        await session.commit()
        return meta_id


async def send_template(
    session: AsyncSession,
    tenant_id: str,
    contact_id: str,
    contact_wa_id: str,
    template_name: str,
    language: str,
    components: list[dict],
    dry_run: bool = False,
) -> str | None:
    """Envía una plantilla HSM aprobada (Meta /messages type=template).

    Requerido para mensajes fuera de la ventana de 24h (recordatorios).
    Devuelve meta_message_id si se envió; None en dry-run.
    """
    channel = (
        await session.execute(
            select(WhatsappChannel).where(WhatsappChannel.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()

    payload = {
        "messaging_product": "whatsapp",
        "to": contact_wa_id,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language},
            "components": components,
        },
    }

    # persistencia de traza del intento (aunque falle, queda registro)
    body_repr = f"[template:{template_name}]"
    msg = Message(
        tenant_id=tenant_id,
        contact_id=contact_id,
        direction="outbound",
        content=body_repr,
    )
    session.add(msg)
    await session.commit()
    await session.refresh(msg)

    if dry_run or not channel or not channel.token_secret_ref:
        return None

    phone_number_id = channel.phone_number_id
    token = _resolve_token(channel.token_secret_ref)
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"https://graph.facebook.com/v19.0/{phone_number_id}/messages",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )
        r.raise_for_status()
        meta_id = r.json().get("messages", [{}])[0].get("id")
        msg.meta_message_id = meta_id
        await session.commit()
        return meta_id


def _resolve_token(secret_ref: str) -> str:
    # En prod resuelve desde secret manager; en dev lee env/var.
    return os.getenv(f"WA_TOKEN_{secret_ref}", secret_ref)
