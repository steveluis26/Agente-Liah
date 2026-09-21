"""Cola persistente del webhook de WhatsApp (Fase 1).

Diseño (ver docs/DECISIONES_FASE1.md):
- El endpoint POST solo valida y hace `enqueue_job()` (INSERT). Responde 200
  en milisegundos; si el proceso muere, el trabajo sigue en BD.
- `drain_jobs()` procesa pendientes fuera del request: puede llamarlo un
  worker, un cron, o el endpoint POST /webhook/whatsapp/jobs/drain.
- Cada trabajo es idempotente: reintentar el mismo wamid no duplica mensajes
  ni respuestas (UniqueConstraint + INSERT ON CONFLICT DO NOTHING +
  idempotency keys en el sender).
"""
import logging
import inspect
import os
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.embedder import FakeEmbedder
from app.agent.engine import run_agent
from app.agent.ports import EmbedderPort, LLMPort, LLMResponse
from app.agent.rag import search_knowledge
from app.agent.sender import WhatsAppCloudSender
from app.channels.adapter import (
    TEXT,
    UNSUPPORTED,
    ChannelAdapter,
    InboundEvent,
    get_adapter,
    register_adapter,
)
from app.core.audit import log_event
from app.core.tenant_ctx import clear_tenant_id, require_tenant, set_tenant_id
from app.models import (
    Contact,
    Handoff,
    Message,
    WebhookJob,
)
from app.models.webhook_jobs import MAX_ATTEMPTS

logger = logging.getLogger("whatsapp.queue")


class WhatsappAdapter:
    """ChannelAdapter de WhatsApp Cloud API.

    Convierte el `value.*` de un change de Meta en `InboundEvent` agnósticos.
    Todo lo específico de Meta (wamid, wa_id, tipos de mensaje) vive aquí y
    no sale de este módulo.
    """

    name = "whatsapp"

    def parse_events(self, change: dict) -> list[InboundEvent]:
        contacts_meta = (change.get("contacts") or [{}])[0]
        profile_name = (contacts_meta.get("profile") or {}).get("name")
        events: list[InboundEvent] = []
        for msg in change.get("messages", []):
            wa_id = msg.get("from")
            wamid = msg.get("id")
            if not wa_id or not wamid:
                continue
            msg_type = msg.get("type")
            body = (
                (msg.get("text") or {}).get("body", "")
                if msg_type == "text"
                else ""
            )
            if msg_type == "text" and body.strip():
                kind, text = TEXT, body
            else:
                kind, text = UNSUPPORTED, ""
            events.append(
                InboundEvent(
                    sender_external_id=wa_id,
                    external_message_id=wamid,
                    kind=kind,
                    text=text,
                    sender_name=profile_name,
                    raw={"msg_type": msg_type, "wamid": wamid},
                )
            )
        return events


register_adapter(WhatsappAdapter())

__all__ = ["WhatsappAdapter", "drain_jobs", "enqueue_job", "reprocess_job"]

UNSUPPORTED_REPLY = (
    "Por ahora solo puedo leer mensajes de texto. "
    "Escríbeme tu duda con palabras y te ayudo."
)


def _dry_run_default() -> bool:
    # Envíos reales solo cuando el operador lo habilita explícitamente.
    return os.getenv("LIAH_SEND_DRY_RUN", "1") == "1"


class _DevStubLLM:
    """LLM de resguardo sin API key: RAG directo en un paso (dev/tests).

    Recibe sesión/tenant/embedder por constructor (inyección explícita, sin
    duck-typing por setattr).
    """

    def __init__(self, session: AsyncSession, tenant_id: uuid.UUID,
                 embedder: EmbedderPort):
        self.session = session
        self.tenant_id = tenant_id
        self.embedder = embedder

    async def chat(self, messages, tools=None, tool_choice=None):
        user_q = messages[-1]["content"]
        hits = await search_knowledge(
            self.session, self.tenant_id, user_q, self.embedder
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


async def _default_llm(session: AsyncSession, tenant_id: uuid.UUID,
                 embedder: EmbedderPort) -> LLMPort:
    """LLM por defecto del drenador: factory por tenant (Fase 2).

    El tenant comercial usa OpenAI con su propia key (SecretProvider). Si el
    tenant no tiene key configurada (dev/tests), se usa el stub local sin
    API key en vez de fallar el job.
    """
    from app.agent.engine import build_llm_for_tenant

    try:
        return await build_llm_for_tenant(session, tenant_id)
    except RuntimeError as e:
        logger.info("Sin LLM comercial para el tenant %s (%s); uso stub dev",
                    tenant_id, e)
        return _DevStubLLM(session, tenant_id, embedder)


async def enqueue_job(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    phone_number_id: str,
    change_value: dict,
) -> WebhookJob:
    """Encola el `value.*` de un change de Meta para proceso diferido."""
    job = WebhookJob(
        tenant_id=tenant_id,
        payload={"channel": WhatsappAdapter.name,
                 "phone_number_id": phone_number_id,
                 "change": change_value},
        status="pending",
    )
    session.add(job)
    await session.flush()
    return job


async def drain_jobs(
    session_maker: async_sessionmaker,
    *,
    limit: int = 50,
    embedder: EmbedderPort | None = None,
    llm_factory=None,
    dry_run: bool | None = None,
) -> dict:
    """Procesa trabajos pendientes. Devuelve estadísticas.

    Cada trabajo corre en su propia sesión: un trabajo que falla no tumba
    el resto. Seguro de llamar concurrentemente (el UPDATE a `processing`
    con filtro de estado evita doble proceso en la práctica).
    """
    embedder = embedder or FakeEmbedder()
    dry_run = _dry_run_default() if dry_run is None else dry_run
    stats = {"processed": 0, "done": 0, "failed": 0, "skipped": 0}

    async with session_maker() as session:
        jobs = (
            await session.execute(
                select(WebhookJob)
                .where(WebhookJob.status == "pending")
                .order_by(WebhookJob.created_at.asc())
                .limit(limit)
            )
        ).scalars().all()
        job_ids = [j.id for j in jobs]

    for job_id in job_ids:
        stats["processed"] += 1
        async with session_maker() as session:
            job = await session.get(WebhookJob, job_id)
            if job is None or job.status != "pending":
                stats["skipped"] += 1
                continue
            job.status = "processing"
            job.attempts += 1
            await session.commit()
            try:
                set_tenant_id(job.tenant_id)
                require_tenant()  # el aislamiento no es opcional
                llm_or_coro = (llm_factory or _default_llm)(
                    session, job.tenant_id, embedder
                )
                llm = (
                    await llm_or_coro
                    if inspect.isawaitable(llm_or_coro)
                    else llm_or_coro
                )
                await _process_job(session, job, llm, embedder, dry_run)
                job.status = "done"
                job.error = None
                # Columna naive: UTC sin tzinfo (convención del esquema).
                job.processed_at = datetime.now(timezone.utc).replace(tzinfo=None)
                stats["done"] += 1
                await log_event(session, job.tenant_id, "job.done",
                                {"job_id": str(job.id)})
            except Exception as e:
                await session.rollback()
                # Re-lee el job en la sesión limpia para marcar el fallo.
                job = await session.get(WebhookJob, job_id)
                if job is not None:
                    job.status = "failed"
                    job.error = f"{type(e).__name__}: {e}"[:2000]
                    await log_event(session, job.tenant_id, "job.failed",
                                    {"job_id": str(job_id),
                                     "error": job.error,
                                     "attempts": job.attempts,
                                     "retryable": job.attempts < MAX_ATTEMPTS})
                stats["failed"] += 1
                logger.exception("Fallo procesando webhook job %s", job_id)
            finally:
                clear_tenant_id()
            await session.commit()
    return stats


async def reprocess_job(session: AsyncSession, job_id: uuid.UUID) -> WebhookJob | None:
    """Devuelve un trabajo failed a pending para reproceso manual."""
    job = await session.get(WebhookJob, job_id)
    if job is None:
        return None
    job.status = "pending"
    job.error = None
    await session.commit()
    return job


async def _process_job(
    session: AsyncSession,
    job: WebhookJob,
    llm: LLMPort,
    embedder: EmbedderPort,
    dry_run: bool,
) -> None:
    tenant_id = job.tenant_id
    payload = job.payload or {}
    adapter: ChannelAdapter = get_adapter(payload.get("channel") or "whatsapp")
    change = payload.get("change") or {}

    for event in adapter.parse_events(change):
        wa_id = event.sender_external_id
        wamid = event.external_message_id
        contact = await _get_or_create_contact(
            session, tenant_id, wa_id, event.sender_name
        )

        # Dedupe por wamid: el retry de Meta no duplica nada.
        exists = await session.execute(
            select(Message.id).where(
                Message.tenant_id == tenant_id,
                Message.meta_message_id == wamid,
            )
        )
        if exists.scalar_one_or_none() is not None:
            await log_event(session, tenant_id, "message.duplicate_skipped",
                            {"wamid": wamid})
            continue

        msg_type = event.raw.get("msg_type")
        if event.kind != TEXT:
            # Tipo no-texto: no se inserta vacío, no crashea; se responde
            # cortés y se audita.
            await log_event(session, tenant_id, "message.unsupported",
                            {"wamid": wamid, "msg_type": msg_type,
                             "contact_id": str(contact.id)})
            sender = WhatsAppCloudSender(session)
            await sender.send_text(
                str(tenant_id), str(contact.id), wa_id, UNSUPPORTED_REPLY,
                idempotency_key=f"webhook:{wamid}:unsupported",
                dry_run=dry_run,
            )
            await session.commit()
            continue

        # A partir de aquí kind == "text" con cuerpo no vacío (el adapter ya
        # filtró lo demás).
        body = event.text
        # INSERT idempotente (carrera entre dos drains del mismo wamid).
        stmt = (
            pg_insert(Message)
            .values(
                tenant_id=tenant_id,
                contact_id=contact.id,
                direction="inbound",
                content=body,
                meta_message_id=wamid,
            )
            .on_conflict_do_nothing()
        )
        res = await session.execute(stmt)
        if res.rowcount == 0:
            await log_event(session, tenant_id, "message.duplicate_skipped",
                            {"wamid": wamid})
            continue
        # Columnas naive en el esquema: UTC sin tzinfo.
        contact.last_interaction_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await log_event(session, tenant_id, "message.processed",
                        {"wamid": wamid, "contact_id": str(contact.id)})
        await session.commit()

        # Gate: si hay handoff abierto, el bot queda en silencio.
        open_h = await session.execute(
            select(Handoff).where(
                Handoff.tenant_id == tenant_id,
                Handoff.contact_id == contact.id,
                Handoff.status == "open",
            )
        )
        if open_h.scalar_one_or_none() is not None:
            logger.info("Handoff abierto para %s: bot en silencio", contact.id)
            continue

        reply = await run_agent(
            session, llm, tenant_id, contact.id, body, embedder=embedder
        )
        if reply:
            sender = WhatsAppCloudSender(session)
            await sender.send_text(
                str(tenant_id), str(contact.id), wa_id, reply,
                idempotency_key=f"webhook:{wamid}:reply",
                dry_run=dry_run,
            )
            await log_event(session, tenant_id, "agent.replied",
                            {"wamid": wamid, "contact_id": str(contact.id)})
            await session.commit()


async def _get_or_create_contact(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    wa_id: str,
    name: str | None,
) -> Contact:
    contact = (
        await session.execute(
            select(Contact).where(
                Contact.tenant_id == tenant_id, Contact.wa_id == wa_id
            )
        )
    ).scalar_one_or_none()
    if contact is None:
        contact = Contact(tenant_id=tenant_id, wa_id=wa_id, name=name)
        session.add(contact)
        try:
            await session.flush()
        except IntegrityError:
            # Carrera: otro worker lo creó primero.
            await session.rollback()
            contact = (
                await session.execute(
                    select(Contact).where(
                        Contact.tenant_id == tenant_id, Contact.wa_id == wa_id
                    )
                )
            ).scalar_one()
    elif name and not contact.name:
        contact.name = name
        await session.flush()
    return contact
