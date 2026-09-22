"""Webhook de Meta WhatsApp Cloud API (Fase 1).

Contrato:
- GET  /webhook/whatsapp -> verificación del token de suscripción.
- POST /webhook/whatsapp -> valida firma + payload, responde 200
  INMEDIATAMENTE y encola el trabajo en `webhook_jobs` (cola persistente en
  BD). El proceso diferido vive en app.channels.whatsapp.queue.
- POST /webhook/whatsapp/jobs/drain -> drena la cola (worker manual/cron).
- POST /webhook/whatsapp/jobs/{id}/reprocess -> reintenta un trabajo fallido.

Comportamiento ante payloads:
- `value.statuses`: auditoría + matcheo contra envíos de campaña (Fase 6:
  delivered/read/failed por wamid); no generan trabajo ni respuesta.
- mensajes no-texto: no crashean, no insertan vacíos. Las notas de voz
  (audio) se transcriben y entran al flujo normal como texto (Fase 7f); el
  resto recibe respuesta cortés en el drenador.
- `phone_number_id` desconocido: rechazo seguro (200 + log, sin procesar).
"""
import json
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.whatsapp.queue import drain_jobs, enqueue_job, reprocess_job
from app.channels.whatsapp.security import verify_meta_signature
from app.core.audit import log_event
from app.core.billing import is_tenant_active
from app.core.config import get_settings
from app.core.db import async_session_maker, get_session
from app.core.tenant_ctx import clear_tenant_id, set_tenant_id
from app.marketing.campaigns import process_delivery_statuses
from app.models import WhatsappChannel
from app.schemas.whatsapp import WhatsappWebhookPayload

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
        data = json.loads(raw)
    except json.JSONDecodeError:
        return Response(status_code=status.HTTP_400_BAD_REQUEST)

    try:
        payload = WhatsappWebhookPayload.model_validate(data)
    except ValidationError as e:
        logger.warning("Webhook rechazado: payload inválido: %s", e.errors())
        return Response(status_code=status.HTTP_400_BAD_REQUEST)

    # Recorremos el dict crudo en paralelo al modelo validado: el modelo
    # gobierna el control de flujo (statuses ya están tipados) y el dict
    # crudo se encola tal cual para no perder campos ni aliases de Meta.
    raw_entries = data.get("entry", [])
    for entry_raw, entry in zip(raw_entries, payload.entry):
        for change_raw, change in zip(entry_raw.get("changes", []), entry.changes):
            if change.field != "messages":
                continue
            value = change.value
            if value is None:
                continue
            value_raw = change_raw.get("value") or {}
            phone_number_id = value.metadata.phone_number_id
            channel = await _resolve_channel(session, phone_number_id)
            if channel is None:
                # Rechazo seguro: 200 para que Meta no reintente, sin procesar.
                logger.warning(
                    "phone_number_id %s sin tenant: payload descartado",
                    phone_number_id,
                )
                continue
            set_tenant_id(channel.tenant_id)
            try:
                # Fase 8: tenant suspendido (falta de pago) -> no se procesa
                # nada. 200 para que Meta no reintente; queda en el log.
                if not await is_tenant_active(session, channel.tenant_id):
                    logger.warning(
                        "tenant %s suspendido/inactivo: payload descartado",
                        channel.tenant_id,
                    )
                    continue
                # statuses tipados en el schema: auditoría + matcheo contra
                # envíos de campaña (Fase 6: delivered/read por wamid).
                if value.statuses:
                    await log_event(
                        session, channel.tenant_id, "webhook.statuses",
                        {"count": len(value.statuses),
                         "phone_number_id": phone_number_id},
                    )
                    await process_delivery_statuses(
                        session, channel.tenant_id,
                        [
                            {"id": st.id, "status": st.status,
                             "timestamp": st.timestamp,
                             "recipient_id": st.recipient_id}
                            for st in value.statuses
                        ],
                    )
                if value.messages:
                    job = await enqueue_job(
                        session, channel.tenant_id, phone_number_id, value_raw
                    )
                    await log_event(
                        session, channel.tenant_id, "webhook.received",
                        {"job_id": str(job.id),
                         "phone_number_id": phone_number_id,
                         "n_messages": len(value.messages),
                         "received_at": datetime.now(timezone.utc).isoformat()},
                    )
                await session.commit()
            finally:
                clear_tenant_id()

    return Response(status_code=status.HTTP_200_OK)


@router.post("/jobs/drain")
async def drain(limit: int = 50):
    """Drena la cola de trabajos pendientes (worker manual o cron)."""
    stats = await drain_jobs(async_session_maker, limit=limit)
    return {"status": "ok", **stats}


@router.post("/jobs/{job_id}/reprocess")
async def reprocess(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    """Devuelve un trabajo fallido a `pending` para reprocesarlo."""
    job = await reprocess_job(session, job_id)
    if job is None:
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    return {"job_id": str(job.id), "status": job.status}


async def _resolve_channel(
    session: AsyncSession, phone_number_id: str
) -> WhatsappChannel | None:
    result = await session.execute(
        select(WhatsappChannel).where(
            WhatsappChannel.phone_number_id == phone_number_id
        )
    )
    return result.scalar_one_or_none()
