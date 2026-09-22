"""Puerta de consentimiento de privacidad (Fase 7c).

Flujo de primer contacto (determinista, sin LLM):
- Contacto con `consent_status` en (none, pending) → el bot responde PRIMERO
  con el texto del aviso (`TenantPrivacyTerms.texto`) pidiendo aceptación;
  `consent_status = "pending"`.
- Respuesta afirmativa → `granted` + `consent_at` + `privacy_terms_version`
  = versión vigente, y el flujo normal continúa.
- Negativa clara → `revoked` + respuesta mínima (no se persiste nombre ni
  se agenda nada).
- Una versión NUEVA de los términos obliga a re-aceptar (comparación por
  versión, decisión Fase 7b): granted con versión vieja vuelve a pending.

Mientras no haya `granted`, no se persiste `name` del contacto (eso lo
aplica el drenador en `_get_or_create_contact`).

Sin fila `TenantPrivacyTerms` para el tenant, la puerta está ABIERTA
(tenants legacy / plantillas 1.0 sin aviso siguen funcionando).
"""
import logging
import re
import unicodedata
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import log_event
from app.models import Contact, TenantPrivacyTerms

logger = logging.getLogger("liah.privacy")

# Respuestas afirmativas / negativas (normalizadas: minúsculas, sin acentos).
_AFFIRMATIVE_RE = re.compile(
    r"\b(si|acepto|aceptar|de acuerdo|ok|confirmo)\b"
)
# Negativa clara: "no acepto" en cualquier parte, o un "no" pelado como
# mensaje completo. (Opt-out siempre gana ante empate, como en marketing.)
_NEGATIVE_CONTAINS_RE = re.compile(r"\bno\s+acepto\b")


def _normalize(text: str) -> str:
    text = (text or "").lower()
    text = "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _is_affirmative(text: str) -> bool:
    return _AFFIRMATIVE_RE.search(_normalize(text)) is not None


def _is_negative(text: str) -> bool:
    norm = _normalize(text)
    if not norm:
        return False
    if _NEGATIVE_CONTAINS_RE.search(norm):
        return True
    return norm == "no"


TERMS_REQUEST_SUFFIX = (
    '\n\n¿Aceptas estos términos? Responde "sí" para aceptar '
    'o "no" para rechazar.'
)

REVOKED_REPLY = (
    "Entendido. Sin tu aceptación no podemos atenderte por este medio "
    "ni guardar tus datos."
)

BOOK_WITHOUT_CONSENT_ERROR = (
    "Para agendar necesito que aceptes el aviso de privacidad vigente. "
    'Te lo acabo de enviar por este medio: responde "sí" para aceptar.'
)


async def current_terms(
    session: AsyncSession, tenant_id
) -> TenantPrivacyTerms | None:
    """Texto vigente de términos del tenant (None = no configurado)."""
    return (
        await session.execute(
            select(TenantPrivacyTerms).where(
                TenantPrivacyTerms.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()


def consent_is_valid(contact: Contact, terms: TenantPrivacyTerms | None) -> bool:
    """True si el contacto puede operar: granted + versión vigente.

    Sin términos configurados la puerta está abierta (tenants legacy).
    """
    if terms is None:
        return True
    return (
        contact.consent_status == "granted"
        and contact.privacy_terms_version == terms.version
    )


async def privacy_gate_error(
    session: AsyncSession, tenant_id, contact: Contact
) -> str | None:
    """Error de negocio si el contacto NO puede agendar, None si sí puede.

    Lo usan `book_appointment` y `reschedule_appointment` (tools.py).
    """
    terms = await current_terms(session, tenant_id)
    if consent_is_valid(contact, terms):
        return None
    return BOOK_WITHOUT_CONSENT_ERROR


def _now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _grant(session, tenant_id, contact: Contact, terms: TenantPrivacyTerms):
    contact.consent_status = "granted"
    contact.consent_at = _now_naive()
    contact.privacy_terms_version = terms.version
    await log_event(
        session, tenant_id, "privacy.granted",
        {"contact_id": str(contact.id), "version": terms.version},
    )
    logger.info("Consentimiento otorgado: contacto %s (v%s)",
                contact.id, terms.version)


async def _set_pending(session, tenant_id, contact: Contact, event: str):
    contact.consent_status = "pending"
    await log_event(
        session, tenant_id, event, {"contact_id": str(contact.id)}
    )


async def apply_privacy_gate(
    session: AsyncSession, tenant_id, contact: Contact, body: str
) -> dict:
    """Aplica la puerta de privacidad al mensaje inbound de un contacto.

    Devuelve {"handled": bool, "reply": str|None, "transition": str|None}:
    - handled=False → el flujo normal continúa (ya había consentimiento
      válido, o el mensaje otorgó el consentimiento).
    - handled=True → el drenador envía `reply` (si hay) y NO llama al agente.
    No hace commit: el llamador persiste (igual que `process_marketing_keyword`).
    """
    terms = await current_terms(session, tenant_id)
    if terms is None:
        return {"handled": False, "reply": None, "transition": None}
    if consent_is_valid(contact, terms):
        return {"handled": False, "reply": None, "transition": None}

    # Negativa siempre gana (aunque el texto también parezca afirmativo).
    if _is_negative(body):
        contact.consent_status = "revoked"
        await log_event(
            session, tenant_id, "privacy.revoked",
            {"contact_id": str(contact.id)},
        )
        logger.info("Consentimiento revocado: contacto %s", contact.id)
        return {"handled": True, "reply": REVOKED_REPLY,
                "transition": "revoked"}

    if _is_affirmative(body):
        await _grant(session, tenant_id, contact, terms)
        # El flujo normal continúa: el agente ve el "sí acepto" como un
        # mensaje más (el historial ya registró el otorgamiento).
        return {"handled": False, "reply": None, "transition": "granted"}

    # Sin respuesta clara: (re)enviar el aviso.
    if contact.consent_status == "granted":
        # Versión nueva de los términos: re-aceptar (decisión Fase 7b).
        await _set_pending(session, tenant_id, contact,
                           "privacy.reaccept_required")
        transition = "reaccept_required"
    elif contact.consent_status == "pending":
        await log_event(session, tenant_id, "privacy.terms_resent",
                        {"contact_id": str(contact.id)})
        transition = "terms_resent"
    else:
        await _set_pending(session, tenant_id, contact, "privacy.terms_sent")
        transition = "terms_sent"
    reply = (terms.texto or "").strip() + TERMS_REQUEST_SUFFIX
    return {"handled": True, "reply": reply, "transition": transition}
