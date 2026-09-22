"""Modo staff por WhatsApp (Fase 7f).

El drenador (`app/channels/whatsapp/queue.py`) detecta al remitente staff
ANTES del flujo de cliente (y del consentimiento) y rutea aquí. Todo es
determinista y testeado; el punto de extensión a futuro es reemplazar
`_parse_intent` por un clasificador LLM sin cambiar los ejecutores.

Comandos (español, tolerantes a acentos/mayúsculas):
- "¿cuántas citas tengo hoy?" / "mi agenda de hoy" -> agenda_today
- "¿quién es el de las 10:30?" -> who_at (cita de hoy a esa hora)
- "¿qué huecos hay mañana?" -> free_tomorrow (escaneo del día siguiente)
- cualquier otra cosa -> help (ejemplos)

Alcance por rol:
- specialist: SOLO su agenda (citas enlazadas a su resource_id vía
  appointment_resources; en huecos, solo tipos de servicio que involucran
  su recurso). Fuera de alcance -> negativa amable.
- owner / receptionist: todo el tenant.

Cero literales de vertical: horas, nombres de servicio y venues salen de la
BD del tenant.
"""
import logging
import re
import unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import availability as availmod
from app.models import (
    Appointment,
    AppointmentResource,
    Contact,
    Resource,
    ServiceType,
    StaffMember,
    Tenant,
)

logger = logging.getLogger("liah.staff")

ROLE_SPECIALIST = "specialist"

# ── detección ─────────────────────────────────────────────────────────

async def get_staff_member(
    session: AsyncSession, tenant_id, wa_id: str
) -> StaffMember | None:
    """Staff del tenant por wa_id, o None si es un número de cliente."""
    return (
        await session.execute(
            select(StaffMember).where(
                StaffMember.tenant_id == tenant_id,
                StaffMember.wa_id == wa_id,
            )
        )
    ).scalar_one_or_none()


# ── utilidades de texto / tiempo ───────────────────────────────────────

def _norm(text: str) -> str:
    text = (text or "").strip().lower()
    text = "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )
    return " ".join(text.split())


def _tenant_now(tz_name: str | None) -> datetime:
    """now naive en la zona del tenant (misma convención que el calendario)."""
    try:
        tz = ZoneInfo(tz_name or "UTC")
    except Exception:  # noqa: BLE001 - zona inválida: no tumbar nada
        tz = ZoneInfo("UTC")
    return datetime.now(tz).replace(tzinfo=None)


async def _tenant_timezone(session: AsyncSession, tenant_id) -> str:
    t = await session.get(Tenant, tenant_id)
    return (t.timezone if t else None) or "America/Mexico_City"


# ── parsing de intents (determinista; punto de extensión p/ LLM) ────────
# Para un clasificador LLM futuro: reemplazar `_parse_intent` por una
# llamada que devuelva (intent, params) con el MISMO vocabulario de intents;
# los ejecutores `_cmd_*` no cambian.

_TIME_RE = re.compile(r"\b(\d{1,2})[:.](\d{2})\b")

INTENT_AGENDA = "agenda_today"
INTENT_WHO_AT = "who_at"
INTENT_FREE = "free_tomorrow"
INTENT_HELP = "help"


def _parse_intent(text: str) -> tuple[str, dict]:
    """Clasifica el mensaje del staff en (intent, params). Determinista."""
    t = _norm(text)
    m = _TIME_RE.search(t)
    hhmm = None
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            hhmm = f"{hh:02d}:{mm:02d}"
    if "quien" in t and hhmm:
        return INTENT_WHO_AT, {"hhmm": hhmm}
    if any(k in t for k in ("hueco", "libre", "disponib", "espacio")):
        return INTENT_FREE, {}
    if (
        "agenda" in t
        or "cuantas citas" in t
        or "mis citas" in t
        or "citas de hoy" in t
        or "que tengo hoy" in t
        or "tengo hoy" in t
    ):
        return INTENT_AGENDA, {}
    return INTENT_HELP, {}


HELP_TEXT = (
    "Soy el asistente del negocio. Puedo decirte:\n"
    "• «mi agenda de hoy» — tus citas confirmadas de hoy\n"
    "• «¿quién es el de las 10:30?» — quién tiene la cita de esa hora\n"
    "• «¿qué huecos hay mañana?» — espacios libres de mañana"
)


# ── agenda (compartida por comandos y briefing) ────────────────────────

async def _service_names(
    session: AsyncSession, tenant_id, slugs: set[str]
) -> dict[str, str]:
    if not slugs:
        return {}
    rows = (
        await session.execute(
            select(ServiceType.slug, ServiceType.nombre).where(
                ServiceType.tenant_id == tenant_id,
                ServiceType.slug.in_(slugs),
            )
        )
    ).all()
    return {slug: nombre for slug, nombre in rows}


async def today_appointments(
    session: AsyncSession, tenant_id, staff: StaffMember,
    *, day: datetime | None = None,
) -> list[dict]:
    """Citas confirmadas del día (default: hoy en la zona del tenant).

    Scoping por rol: specialist filtra por su resource_id vía
    appointment_resources; owner/receptionist ven todo. Sin resource_id en
    un specialist -> lista vacía + aviso (no se asume nada).
    Devuelve dicts {start_at, contact_name, service_name, venue}.
    """
    tz = await _tenant_timezone(session, tenant_id)
    now = day or _tenant_now(tz)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)

    stmt = (
        select(Appointment, Contact.name)
        .join(Contact, Contact.id == Appointment.contact_id)
        .where(
            Appointment.tenant_id == tenant_id,
            Appointment.status == "confirmed",
            Appointment.start_at >= day_start,
            Appointment.start_at < day_end,
        )
        .order_by(Appointment.start_at.asc())
    )
    if staff.role == ROLE_SPECIALIST:
        if staff.resource_id is None:
            return []
        stmt = stmt.join(
            AppointmentResource,
            AppointmentResource.appointment_id == Appointment.id,
        ).where(AppointmentResource.resource_id == staff.resource_id)

    rows = (await session.execute(stmt)).all()
    names = await _service_names(
        session, tenant_id, {a.service_type_slug for a, _ in rows if a.service_type_slug}
    )
    return [
        {
            "start_at": a.start_at,
            "contact_name": cname or "sin nombre",
            "service_name": names.get(a.service_type_slug, a.service_type_slug or a.type),
            "venue": a.venue,
        }
        for a, cname in rows
    ]


def _fmt_time(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def format_agenda(appts: list[dict], *, who: str | None, fecha_txt: str) -> str:
    """Texto de agenda: nº de citas + hora–contacto–servicio.

    `who=None` -> encabezado impersonal ("Tu agenda del ..."), usado por el
    briefing matutino.
    """
    head = f"{who}, tu agenda del {fecha_txt}:\n\n" if who else (
        f"Tu agenda del {fecha_txt}:\n\n"
    )
    if not appts:
        return head + "No hay citas confirmadas."
    lines = [f"Tienes {len(appts)} cita(s) confirmadas:"]
    for a in appts:
        line = f"• {_fmt_time(a['start_at'])} — {a['contact_name']} ({a['service_name']})"
        if a.get("venue"):
            line += f" en {a['venue']}"
        lines.append(line)
    return head + "\n".join(lines)


# ── comandos ──────────────────────────────────────────────────────────

async def _cmd_agenda(session, tenant_id, staff) -> str:
    tz = await _tenant_timezone(session, tenant_id)
    now = _tenant_now(tz)
    appts = await today_appointments(session, tenant_id, staff, day=now)
    fecha_txt = now.strftime("%d/%m")
    return format_agenda(appts, who=staff.nombre, fecha_txt=fecha_txt)


async def _cmd_who_at(session, tenant_id, staff, hhmm: str) -> str:
    tz = await _tenant_timezone(session, tenant_id)
    now = _tenant_now(tz)
    appts = await today_appointments(session, tenant_id, staff, day=now)
    hit = next((a for a in appts if _fmt_time(a["start_at"]) == hhmm), None)
    if hit is None:
        return f"No hay cita hoy a las {hhmm}."
    txt = f"A las {hhmm}: {hit['contact_name']} — {hit['service_name']}"
    if hit.get("venue"):
        txt += f" en {hit['venue']}"
    return txt + "."


async def _service_involves_resource(
    session: AsyncSession, tenant_id, st: ServiceType, resource: Resource
) -> bool:
    """¿El tipo de servicio usa el recurso del specialist? (scoping)."""
    for spec in st.recursos_requeridos or []:
        if not isinstance(spec, dict):
            continue
        if spec.get("recurso") == resource.slug:
            return True
        if spec.get("tipo") == resource.tipo:
            esp = spec.get("especialidad")
            if not esp or esp == resource.especialidad:
                return True
    return False


async def _cmd_free(session, tenant_id, staff) -> str:
    """Huecos libres mañana: escaneo del día por tipo de servicio.

    Alcance: 08:00–20:00 en pasos de 30 min, máx. 8 huecos por tipo de
    servicio, usando el motor de disponibilidad (recursos, buffers y
    traslados reales). El specialist solo ve tipos que usan su recurso.
    """
    tz = await _tenant_timezone(session, tenant_id)
    tomorrow = (_tenant_now(tz) + timedelta(days=1)).date().isoformat()
    fecha_txt = datetime.strptime(tomorrow, "%Y-%m-%d").strftime("%d/%m")

    resource = None
    if staff.role == ROLE_SPECIALIST:
        if staff.resource_id is None:
            return (
                "Tu rol de especialista no tiene un recurso asignado; "
                "pídele al administrador que lo configure."
            )
        resource = await session.get(Resource, staff.resource_id)
        if resource is None:
            # resource_id huérfano (el recurso se borró): no sobre-permitir
            # (ver todo) ni crashear; se degrada como sin asignar.
            return (
                "Tu rol de especialista no tiene un recurso válido asignado; "
                "pídele al administrador que lo revise."
            )

    sts = (
        await session.execute(
            select(ServiceType)
            .where(ServiceType.tenant_id == tenant_id)
            .order_by(ServiceType.slug)
        )
    ).scalars().all()

    bloques: list[str] = []
    for st in sts:
        if resource is not None and not await _service_involves_resource(
            session, tenant_id, st, resource
        ):
            continue
        libres: list[str] = []
        minuto = 8 * 60
        while minuto < 20 * 60 and len(libres) < 8:
            hhmm = f"{minuto // 60:02d}:{minuto % 60:02d}"
            chk = await availmod.check_resource_availability(
                session, tenant_id, st.slug, tomorrow, hhmm
            )
            if chk["available"]:
                libres.append(hhmm)
            minuto += 30
        if libres:
            bloques.append(f"• {st.nombre}: {', '.join(libres)}")

    if not bloques:
        return f"No hay huecos libres mañana ({fecha_txt})."
    return f"Huecos libres mañana ({fecha_txt}):\n" + "\n".join(bloques)


async def handle_staff_message(
    session: AsyncSession, tenant_id, staff: StaffMember, text: str
) -> str | None:
    """Procesa un mensaje del staff y devuelve el texto de respuesta.

    Devuelve None solo si no hay nada que decir (hoy siempre responde).
    Nunca levanta por input del usuario: un fallo inesperado se convierte
    en mensaje amable + log.
    """
    try:
        intent, params = _parse_intent(text)
        if intent == INTENT_AGENDA:
            return await _cmd_agenda(session, tenant_id, staff)
        if intent == INTENT_WHO_AT:
            return await _cmd_who_at(session, tenant_id, staff, params["hhmm"])
        if intent == INTENT_FREE:
            return await _cmd_free(session, tenant_id, staff)
        return HELP_TEXT
    except Exception:  # noqa: BLE001 - el staff siempre recibe respuesta
        logger.exception("Fallo manejando mensaje de staff")
        return (
            "Tuve un problema revisando eso. Intenta de nuevo o escribe "
            "«ayuda» para ver qué puedo hacer."
        )


__all__ = [
    "HELP_TEXT",
    "get_staff_member",
    "handle_staff_message",
    "today_appointments",
    "format_agenda",
]
