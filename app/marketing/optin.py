"""Opt-in / opt-out de marketing por palabra clave (Fase 6).

Es el corazón del módulo: SIN opt-in no hay campaña ni aviso (ni siquiera
los avisos: un HSM no solicitado es spam y Meta banea el número; ver
docs/DECISIONES_FASE6.md).

Diseño: regla simple, determinista y testeada (NO LLM) que el drenador
aplica a cada mensaje inbound de texto ANTES de llamar al agente:

- El texto se normaliza (minúsculas, sin acentos, puntuación → espacios).
- Coincidencia por LÍMITES DE PALABRA (regex \\b): "baja" NO dispara dentro
  de "trabajan", "stop" NO dispara dentro de "estopa".
- Configurable por tenant vía tenant_configs.extra:
    "marketing_optin_keywords":  ["quiero recibir promociones", ...]
    "marketing_optout_keywords": ["baja", "stop", ...]
  (los defaults cubren español mexicano; el operador del tenant los puede
  ampliar sin tocar código).
- Opt-out SIEMPRE gana y es inmediato: si un mensaje matchea ambas listas,
  se aplica opt-out (el caso típico: "quiero darme de baja").
- El opt-out es irreversible por campaña: el dispatch filtra por
  marketing_opt_in=true en cada envío, no por snapshot al lanzar. Un contacto
  que se da de baja DESPUÉS del lanzamiento queda excluido automáticamente.

Criterio de conversión prospect → client (segmentación):
  `mark_contact_client()`: el engine lo llama cuando un book_appointment
  se confirma. prospect = llegó por el canal y aún no convierte; client =
  ya agendó (y a futuro: compró / pagó, cuando existan esos eventos).
"""
import logging
import re
import unicodedata
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import log_event
from app.models import Contact, TenantConfig

logger = logging.getLogger("liah.marketing.optin")

# Fuentes del opt-in (para marketing_opt_in_source).
SOURCE_KEYWORD = "keyword"
SOURCE_PANEL = "panel"
SOURCE_IMPORT = "import"
SOURCE_ONBOARDING = "onboarding"
OPTIN_SOURCES = (SOURCE_KEYWORD, SOURCE_PANEL, SOURCE_IMPORT, SOURCE_ONBOARDING)

# Defaults en español (mexicano). Sin vertical ni canal hardcodeados: son
# frases genéricas de consentimiento de marketing.
DEFAULT_OPTIN_KEYWORDS = (
    "quiero recibir promociones",
    "si quiero promos",
    "quiero promos",
    "quiero recibir promos",
    "quiero recibir ofertas",
    "mandenme promociones",
    "mándenme promociones",
    "envienme promociones",
    "envíenme promociones",
    "quiero suscribirme",
    "me suscribo",
    "suscribanme",
    "suscríbanme",
    "quiero recibir avisos",
    "quiero recibir noticias",
)

DEFAULT_OPTOUT_KEYWORDS = (
    "baja",
    "baja de promociones",
    "dame de baja",
    "denme de baja",
    "quitenme de la lista",
    "quítenme de la lista",
    "no me manden mas",
    "no me envien mas",
    "no me manden mensajes",
    "no me envien mensajes",
    "no quiero promociones",
    "no quiero recibir promociones",
    "no mas publicidad",
    "no más publicidad",
    "stop",
    "unsubscribe",
    "cancelar suscripcion",
)

# Respuestas enlatadas: el drenador las envía en vez de la respuesta del
# agente cuando el mensaje era solo un opt-in/opt-out (determinista).
OPTIN_ACK = (
    "Listo, quedaste registrado para recibir nuestras promociones y avisos "
    "por este medio. Puedes darte de baja cuando quieras escribiendo BAJA."
)
OPTOUT_ACK = (
    "Listo, ya no recibirás promociones ni avisos nuestros por este medio. "
    "Si cambias de opinión, escribe QUIERO PROMOS para volver a registrarte."
)


def _normalize(text: str) -> str:
    """Minúsculas, sin acentos, puntuación → espacios, espacios colapsados."""
    text = text.lower()
    text = "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _matches(text: str, keyword: str) -> bool:
    """True si `keyword` aparece en `text` con límites de palabra."""
    norm_kw = _normalize(keyword)
    if not norm_kw:
        return False
    return re.search(r"\b" + re.escape(norm_kw) + r"\b", text) is not None


def classify(text: str, *, optin_keywords=DEFAULT_OPTIN_KEYWORDS,
             optout_keywords=DEFAULT_OPTOUT_KEYWORDS) -> str | None:
    """Clasifica un mensaje: "optin" | "optout" | None.

    Opt-out gana en caso de empate (p.ej. "quiero darme de baja" contiene
    "quiero" pero lo que importa es "baja").
    """
    norm = _normalize(text or "")
    if not norm:
        return None
    if any(_matches(norm, kw) for kw in optout_keywords):
        return "optout"
    if any(_matches(norm, kw) for kw in optin_keywords):
        return "optin"
    return None


async def _tenant_keywords(session: AsyncSession, tenant_id) -> tuple:
    """Lee las listas configurables del tenant (extra), con defaults."""
    cfg = (
        await session.execute(
            select(TenantConfig).where(TenantConfig.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    extra = (cfg.extra or {}) if cfg else {}
    optin_kw = extra.get("marketing_optin_keywords") or list(DEFAULT_OPTIN_KEYWORDS)
    optout_kw = extra.get("marketing_optout_keywords") or list(DEFAULT_OPTOUT_KEYWORDS)
    return optin_kw, optout_kw


async def process_marketing_keyword(
    session: AsyncSession,
    tenant_id,
    contact: Contact,
    text: str,
) -> str | None:
    """Aplica la regla de opt-in/opt-out al mensaje inbound de un contacto.

    Devuelve "optin" | "optout" | None. Actualiza el contacto y audita en
    event_log. Idempotente: re-procesar el mismo mensaje no cambia nada más
    allá del primer set (el estado final es el mismo).

    Regla dura: opt-out SIEMPRE se aplica aunque el contacto no tuviera
    opt-in previo (un "baja" de quien nunca se suscribió simplemente lo deja
    en False, sin error).
    """
    optin_kw, optout_kw = await _tenant_keywords(session, tenant_id)
    verdict = classify(text, optin_keywords=optin_kw, optout_keywords=optout_kw)
    if verdict is None:
        return None

    now = datetime.now(timezone.utc).replace(tzinfo=None)  # columna naive
    if verdict == "optout":
        contact.marketing_opt_in = False
        contact.marketing_opt_in_at = now
        contact.marketing_opt_in_source = SOURCE_KEYWORD
        await log_event(
            session, tenant_id, "marketing.optout",
            {"contact_id": str(contact.id), "wa_id": contact.wa_id,
             "source": SOURCE_KEYWORD},
        )
        logger.info("Opt-out de marketing: contacto %s (keyword)", contact.id)
        return "optout"

    # optin
    contact.marketing_opt_in = True
    contact.marketing_opt_in_at = now
    contact.marketing_opt_in_source = SOURCE_KEYWORD
    await log_event(
        session, tenant_id, "marketing.optin",
        {"contact_id": str(contact.id), "wa_id": contact.wa_id,
         "source": SOURCE_KEYWORD},
    )
    logger.info("Opt-in de marketing: contacto %s (keyword)", contact.id)
    return "optin"


async def set_opt_in(
    session: AsyncSession,
    tenant_id,
    contact: Contact,
    opt_in: bool,
    source: str,
) -> None:
    """Toggle manual de opt-in (panel/import/onboarding). Valida la fuente."""
    if source not in OPTIN_SOURCES:
        raise ValueError(f"source inválido: {source!r} (válidos: {OPTIN_SOURCES})")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    contact.marketing_opt_in = bool(opt_in)
    contact.marketing_opt_in_at = now
    contact.marketing_opt_in_source = source
    await log_event(
        session, tenant_id,
        "marketing.optin" if opt_in else "marketing.optout",
        {"contact_id": str(contact.id), "wa_id": contact.wa_id, "source": source},
    )


async def mark_contact_client(
    session: AsyncSession,
    tenant_id,
    contact: Contact,
    reason: str = "book_appointment",
) -> bool:
    """Promueve prospect → client ante un evento de conversión.

    Criterio actual (documentado en DECISIONES_FASE6.md): el engine lo llama
    cuando un `book_appointment` se confirma (ok=true). A futuro, otros
    eventos de conversión (pago, compra) llamarán esta misma función.

    Devuelve True si hubo cambio (era prospect), False si ya era client.
    """
    if contact.contact_type == "client":
        return False
    contact.contact_type = "client"
    await log_event(
        session, tenant_id, "contact.became_client",
        {"contact_id": str(contact.id), "wa_id": contact.wa_id, "reason": reason},
    )
    logger.info("Contacto %s promovido a client (%s)", contact.id, reason)
    return True
