"""Webhook de Meta WhatsApp Cloud API (Fase 0 + orquestación Fase 1).

GET  /webhook/whatsapp  -> verificación del token de suscripción (hub.challenge).
POST /webhook/whatsapp  -> recibe mensajes, resuelve tenant vía phone_number_id,
                           crea/actualiza contact e inserta mensaje inbound.
                           Si no hay handoff abierto, dispara run_agent en background.
"""
import json
import logging
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.engine import run_agent, set_embedder
from app.agent.embedder import FakeEmbedder
from app.agent.llm import OpenAILLM
from app.agent.sender import send_message
from app.channels.whatsapp.security import verify_meta_signature
from app.core.config import get_settings
from app.core.db import async_session_maker, get_session
from app.core.tenant_ctx import clear_tenant_id, set_tenant_id
from app.models import Contact, Handoff, Message, WhatsappChannel

settings = get_settings()
logger = logging.getLogger("whatsapp.webhook")
router = APIRouter(prefix="/webhook/whatsapp", tags=["whatsapp"])

# Embedder por defecto para el loop (Fake en dev; OpenAI en prod vía set_embedder).
set_embedder(FakeEmbedder())


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
    background: BackgroundTasks,
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
                contact = await _persist_message(session, channel.tenant_id, msg)
                background.add_task(
                    _handle_agent,
                    channel.tenant_id,
                    contact.id,
                    contact.wa_id,
                    (msg.get("text") or {}).get("body", ""),
                )
            clear_tenant_id()

    return Response(status_code=status.HTTP_200_OK)


async def _handle_agent(tenant_id, contact_id, wa_id, user_text: str):
    """Ejecuta el agente y envía la respuesta. Corre fuera del request scope."""
    async with async_session_maker() as session:
        # Gate: si hay handoff abierto, el bot queda en silencio (human-in-control).
        open_h = await session.execute(
            select(Handoff).where(
                Handoff.tenant_id == tenant_id,
                Handoff.contact_id == contact_id,
                Handoff.status == "open",
            )
        )
        if open_h.scalar_one_or_none() is not None:
            logger.info("Handoff abierto para %s: bot en silencio", contact_id)
            return

        try:
            llm = OpenAILLM()
        except RuntimeError:
            llm = _StubLLM()

        reply = await run_agent(session, llm, tenant_id, contact_id, user_text)
        if reply:
            await send_message(
                session, tenant_id, contact_id, wa_id, reply, dry_run=True
            )


class _StubLLM:
    """LLM de resguardo sin API key: RAG directo en un paso (dev/tests).

    En prod se usa OpenAILLM real (tool-calling). Aquí garantizamos que el
    pipeline responde aunque no haya OPENAI_API_KEY. run_agent inyecta
    self.session y self.tenant_id.
    """

    session = None
    tenant_id = None

    async def chat(self, messages, tools=None, tool_choice=None):
        from app.agent.ports import LLMResponse
        from app.agent.rag import search_knowledge

        user_q = messages[-1]["content"]
        hits = await search_knowledge(
            self.session, self.tenant_id, user_q, FakeEmbedder()
        )
        if hits:
            ctx = "\n".join(h["content"] for h in hits)
            return LLMResponse(
                content=f"Según la información del negocio:\n{ctx}",
                finish_reason="stop",
            )
        return LLMResponse(
            content="No encontré esa información. ¿Quieres que un humano te ayude?",
            finish_reason="stop",
        )


async def _resolve_channel(session: AsyncSession, phone_number_id: str) -> WhatsappChannel | None:
    result = await session.execute(
        select(WhatsappChannel).where(WhatsappChannel.phone_number_id == phone_number_id)
    )
    return result.scalar_one_or_none()


async def _persist_message(session: AsyncSession, tenant_id, msg: dict) -> Contact:
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
        contact.last_interaction_at = datetime.utcnow()

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
    return contact
