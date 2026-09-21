"""Sender de WhatsApp vía Meta Cloud API (/messages).

Soporta texto libre (dentro de ventana 24h) y plantillas HSM (type=template)
para mensajes proactivos (recordatorios).

Endurecimiento Fase 1:
- Idempotencia: `idempotency_key` deduplica ANTES de persistir (action_log).
- Retry con backoff exponencial ante 429/5xx y errores de transporte.
- `_resolve_token` falla explícitamente si no hay secreto configurado; jamás
  usa el ref como token.
- Versión de Graph API configurable (`WHATSAPP_GRAPH_VERSION`), no pineada.

En dev/local se usa `dry_run=True`: el envío se omite pero se registra el
mensaje outbound. Con `dry_run=False`, canal o secreto ausente es un error
explícito (nunca se finge un envío).
"""
import asyncio
import logging
import os

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.ports import SenderPort, SendResult
from app.core.config import get_settings
from app.models import ActionLog, Contact, Message, WhatsappChannel

logger = logging.getLogger("liah.sender")

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3
_BACKOFF_BASE_S = 1.0


def _graph_base() -> str:
    version = get_settings().whatsapp_graph_version
    return f"https://graph.facebook.com/{version}"


async def _post_with_retry(
    client: httpx.AsyncClient, url: str, headers: dict, payload: dict
) -> httpx.Response:
    """POST con backoff exponencial ante 429/5xx y errores de red."""
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            r = await client.post(url, headers=headers, json=payload)
            if r.status_code in _RETRYABLE_STATUS and attempt < _MAX_RETRIES:
                delay = _BACKOFF_BASE_S * (2 ** attempt)
                logger.warning(
                    "Meta API %s (intento %d/%d); reintentando en %.1fs",
                    r.status_code, attempt + 1, _MAX_RETRIES + 1, delay,
                )
                await asyncio.sleep(delay)
                continue
            r.raise_for_status()
            return r
        except httpx.HTTPStatusError as e:
            # 4xx no-reintentables (ni 429, ya manejado) fallan de inmediato.
            raise
        except (httpx.TransportError, httpx.TimeoutException) as e:
            last_exc = e
            if attempt < _MAX_RETRIES:
                delay = _BACKOFF_BASE_S * (2 ** attempt)
                logger.warning(
                    "Error de transporte (%s); reintentando en %.1fs", e, delay
                )
                await asyncio.sleep(delay)
    raise last_exc or RuntimeError("fallo de envío sin respuesta")


def _resolve_token(secret_ref: str | None) -> str:
    """Resuelve el token de envío del canal.

    Lee `WA_TOKEN_<secret_ref>` del entorno (en prod vendrá de un secret
    manager). FALLA si no hay secreto configurado: jamás usa el ref como
    token (eso enviaría "PENDING" como Bearer y quemaría el intento).
    """
    if not secret_ref or secret_ref == "PENDING":
        raise RuntimeError(
            "El canal no tiene token configurado (token_secret_ref vacío/PENDING). "
            "Configura el secreto del tenant antes de enviar."
        )
    token = os.getenv(f"WA_TOKEN_{secret_ref}")
    if not token:
        raise RuntimeError(
            f"No se resolvió el secreto WA_TOKEN_{secret_ref}. "
            "Define la variable o conecta el secret manager."
        )
    return token


async def _claim_idempotency(
    session: AsyncSession,
    tenant_id,
    contact_id,
    action: str,
    idempotency_key: str | None,
) -> ActionLog | None:
    """Dedupe ANTES de persistir: si la clave ya existe, no se re-ejecuta."""
    if not idempotency_key:
        return None
    existing = (
        await session.execute(
            select(ActionLog).where(ActionLog.idempotency_key == idempotency_key)
        )
    ).scalar_one_or_none()
    return existing


async def _record_action(
    session: AsyncSession,
    tenant_id,
    contact_id,
    action: str,
    idempotency_key: str | None,
    status: str,
    result: dict,
) -> None:
    if not idempotency_key:
        return
    session.add(
        ActionLog(
            tenant_id=tenant_id,
            contact_id=contact_id,
            action=action,
            idempotency_key=idempotency_key,
            status=status,
            result=result,
        )
    )
    try:
        await session.flush()
    except IntegrityError:
        # Otro worker ganó la carrera: su registro prevalece.
        await session.rollback()


async def send_message(
    session: AsyncSession,
    tenant_id: str,
    contact_id: str,
    contact_wa_id: str,
    text: str,
    dry_run: bool = False,
    *,
    idempotency_key: str | None = None,
) -> str | None:
    """Envía texto al contacto y persiste el mensaje outbound.

    Idempotente por `idempotency_key`. Devuelve meta_message_id si se envió;
    None en dry-run o si fue duplicado.
    """
    result = await _send_impl(
        session, tenant_id, contact_id, contact_wa_id,
        {"messaging_product": "whatsapp", "to": contact_wa_id,
         "type": "text", "text": {"body": text}},
        body_repr=text,
        action="send_message",
        idempotency_key=idempotency_key,
        dry_run=dry_run,
    )
    return result.get("meta_message_id")


async def send_template(
    session: AsyncSession,
    tenant_id: str,
    contact_id: str,
    contact_wa_id: str,
    template_name: str,
    language: str,
    components: list[dict],
    dry_run: bool = False,
    *,
    idempotency_key: str | None = None,
) -> str | None:
    """Envía una plantilla HSM aprobada (Meta /messages type=template).

    Requerido para mensajes fuera de la ventana de 24h (recordatorios).
    Idempotente por `idempotency_key`. Devuelve meta_message_id si se envió;
    None en dry-run o si fue duplicado.
    """
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
    result = await _send_impl(
        session, tenant_id, contact_id, contact_wa_id, payload,
        body_repr=f"[template:{template_name}]",
        action="send_template",
        idempotency_key=idempotency_key,
        dry_run=dry_run,
    )
    return result.get("meta_message_id")


async def _send_impl(
    session: AsyncSession,
    tenant_id: str,
    contact_id: str,
    contact_wa_id: str,
    payload: dict,
    *,
    body_repr: str,
    action: str,
    idempotency_key: str | None,
    dry_run: bool,
) -> SendResult:
    # 1) Dedupe por clave ANTES de persistir nada.
    claimed = await _claim_idempotency(
        session, tenant_id, contact_id, action, idempotency_key
    )
    if claimed is not None:
        logger.info("Envío duplicado omitido (key=%s)", idempotency_key)
        prev = dict(claimed.result or {})
        return {
            "message_id": prev.get("message_id"),
            "meta_message_id": prev.get("meta_message_id"),
            "status": "skipped_duplicate",
            "error": None,
        }

    channel = (
        await session.execute(
            select(WhatsappChannel).where(WhatsappChannel.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()

    msg = Message(
        tenant_id=tenant_id,
        contact_id=contact_id,
        direction="outbound",
        content=body_repr,
    )
    session.add(msg)
    await session.flush()

    if dry_run:
        await _record_action(
            session, tenant_id, contact_id, action, idempotency_key, "ok",
            {"message_id": str(msg.id), "meta_message_id": None,
             "status": "dry_run"},
        )
        await session.commit()
        return {"message_id": str(msg.id), "meta_message_id": None,
                "status": "dry_run", "error": None}

    # Sin dry-run, canal o secreto ausente es un error explícito: jamás se
    # finge un envío que no ocurrió.
    if not channel or not channel.token_secret_ref:
        raise RuntimeError(
            "No se puede enviar a WhatsApp: el tenant no tiene canal/token "
            "configurado (token_secret_ref vacío) y dry_run=False."
        )

    # Falla explícito si no hay secreto (no se envía nada a medias).
    token = _resolve_token(channel.token_secret_ref)
    url = f"{_graph_base()}/{channel.phone_number_id}/messages"
    try:
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            r = await _post_with_retry(
                client, url, {"Authorization": f"Bearer {token}"}, payload
            )
            meta_id = r.json().get("messages", [{}])[0].get("id")
    except Exception as e:
        await _record_action(
            session, tenant_id, contact_id, action, idempotency_key, "failed",
            {"message_id": str(msg.id), "meta_message_id": None,
             "error": str(e)},
        )
        await session.commit()
        logger.exception("Fallo envío %s tenant=%s", action, tenant_id)
        raise

    msg.meta_message_id = meta_id
    await _record_action(
        session, tenant_id, contact_id, action, idempotency_key, "ok",
        {"message_id": str(msg.id), "meta_message_id": meta_id,
         "status": "sent"},
    )
    await session.commit()
    return {"message_id": str(msg.id), "meta_message_id": meta_id,
            "status": "sent", "error": None}


class WhatsAppCloudSender:
    """Adaptador WhatsApp del contrato SenderPort (desacoplamiento canal).

    El orquestador programa contra SenderPort; este adaptador traduce al
    API de Meta. Mañana un InstagramSender/FacebookSender implementa el mismo
    contrato sin tocar el motor.
    """

    def __init__(self, session: AsyncSession):
        self._session = session

    async def send_text(
        self,
        tenant_id: str,
        contact_id: str,
        channel_contact_id: str,
        text: str,
        *,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> SendResult:
        meta_id = await send_message(
            self._session, tenant_id, contact_id, channel_contact_id, text,
            dry_run=dry_run, idempotency_key=idempotency_key,
        )
        status = "dry_run" if dry_run else ("sent" if meta_id else "failed")
        return {"message_id": None, "meta_message_id": meta_id,
                "status": status, "error": None}
