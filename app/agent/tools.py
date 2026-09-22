"""Herramientas (tools) expuestas al LLM.

Cada tool es un dict OpenAI (name/description/parameters) + una función Python
que lo ejecuta. El engine itera tool_calls y despacha por nombre.

Reglas Fase 1:
- Los argumentos del LLM se validan con pydantic (fechas YYYY-MM-DD, horas
  HH:MM, enums). Argumento inválido -> error de negocio, nunca crash.
- El `contact_id` que proponga el LLM se IGNORA siempre: la identidad del
  contacto la fija el contexto autenticado (ctx.contact_id). Esto evita que un
  prompt injection agende/lea a nombre de otro contacto.
- `build_tools()` permite filtrar tools por tenant/tier (p.ej. un tier base
  sin agendamiento). El catálogo por defecto sigue siendo TOOLS.
"""
import logging
import re
import uuid
from enum import Enum

from pydantic import BaseModel, field_validator

from app.agent import calendar as calmod
from app.agent.consent import privacy_gate_error
from app.agent.ports import EmbedderPort
from app.agent.rag import RAG_THRESHOLD, search_knowledge
from app.agent.staff_notify import notify_staff
from app.models import Contact, Handoff
from app.models.conversations import MODE_HUMAN, set_conversation_mode

logger = logging.getLogger("liah.tools")


class AppointmentType(str, Enum):
    """Tipos de cita genéricos (sin literales de ningún vertical).

    Fase 4: se eliminó `trial_class` (herencia del demo de academia de danza).
    Los tipos específicos de cada negocio viajan en el perfil del tenant, no
    en el enum del motor.
    """
    consultation = "consultation"
    followup = "followup"
    other = "other"


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SLOT_RE = re.compile(r"^\d{2}:\d{2}(-\d{2}:\d{2})?$")


class SearchArgs(BaseModel):
    query: str


class CheckAvailabilityArgs(BaseModel):
    date: str
    time_slot: str
    # Fase 7c: opcionales. Con `service_type` se usa el motor de recursos
    # (capacidad, buffers, traslados); sin él, el chequeo legacy por slot.
    service_type: str | None = None
    venue: str | None = None

    @field_validator("date")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        if not _DATE_RE.match(v):
            raise ValueError("date debe ser YYYY-MM-DD")
        return v

    @field_validator("time_slot")
    @classmethod
    def _valid_slot(cls, v: str) -> str:
        if not _SLOT_RE.match(v):
            raise ValueError("time_slot debe ser HH:MM o HH:MM-HH:MM")
        return v


class BookAppointmentArgs(CheckAvailabilityArgs):
    type: AppointmentType = AppointmentType.other
    # NOTA: no hay contact_id en el schema. Si el LLM lo manda igual en los
    # argumentos crudos, run_tool lo ignora y usa ctx.contact_id.


class EscalateArgs(BaseModel):
    reason: str = ""


class RescheduleAppointmentArgs(BaseModel):
    old_date: str
    old_time_slot: str
    new_date: str
    new_time_slot: str
    type: AppointmentType = AppointmentType.other
    # Fase 7c: aplican al NUEVO slot (motor de recursos + venue del evento).
    service_type: str | None = None
    venue: str | None = None

    @field_validator("old_date", "new_date")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        if not _DATE_RE.match(v):
            raise ValueError("la fecha debe ser YYYY-MM-DD")
        return v

    @field_validator("old_time_slot", "new_time_slot")
    @classmethod
    def _valid_slot(cls, v: str) -> str:
        if not _SLOT_RE.match(v):
            raise ValueError("el horario debe ser HH:MM o HH:MM-HH:MM")
        return v


# ── Definiciones (formato OpenAI tools) ──────────────
def build_tools(enabled: list[str] | set[str] | None = None) -> list[dict]:
    """Construye el catálogo de tools, opcionalmente filtrado por nombre.

    `enabled` viene de la config del tenant (p.ej.
    `tenant_configs.extra["enabled_tools"]`); None = catálogo completo.
    """
    catalog = [
        {
            "type": "function",
            "function": {
                "name": "search_knowledge_base",
                "description": "Busca en la base de conocimiento del negocio (precios, horarios, "
                               "políticas, FAQs) la respuesta a la duda del cliente.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string",
                                  "description": "Pregunta o tema a buscar."}
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "check_availability",
                "description": "Consulta si hay cupo para agendar en una fecha y hora. "
                               "Si se indica service_type (slug del tipo de servicio), "
                               "valida recursos, buffers y traslados.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "date": {"type": "string", "description": "Fecha YYYY-MM-DD."},
                        "time_slot": {"type": "string",
                                      "description": "Hora HH:MM o rango HH:MM-HH:MM."},
                        "service_type": {"type": "string",
                                         "description": "Opcional: slug del tipo de "
                                                        "servicio a agendar."},
                        "venue": {"type": "string",
                                  "description": "Opcional: sede/ubicación del evento "
                                                 "(negocios móviles)."},
                    },
                    "required": ["date", "time_slot"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "book_appointment",
                "description": "Reserva una cita en el calendario del negocio para el "
                               "contacto actual. No acepta contact_id: siempre agenda "
                               "para quien escribe. Requiere consentimiento de "
                               "privacidad vigente del contacto.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "date": {"type": "string", "description": "Fecha YYYY-MM-DD."},
                        "time_slot": {"type": "string",
                                      "description": "Hora HH:MM."},
                        "type": {"type": "string",
                                 "description": "Tipo: consultation | followup | other.",
                                 "enum": ["consultation", "followup", "other"]},
                        "service_type": {"type": "string",
                                         "description": "Opcional: slug del tipo de "
                                                        "servicio a agendar."},
                        "venue": {"type": "string",
                                  "description": "Opcional: sede/ubicación del evento "
                                                 "(negocios móviles)."},
                    },
                    "required": ["date", "time_slot", "type"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "cancel_appointment",
                "description": "Cancela la cita confirmada del contacto actual en "
                               "una fecha y hora. No acepta contact_id: siempre "
                               "cancela la cita de quien escribe.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "date": {"type": "string", "description": "Fecha YYYY-MM-DD."},
                        "time_slot": {"type": "string",
                                      "description": "Hora HH:MM."},
                    },
                    "required": ["date", "time_slot"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "reschedule_appointment",
                "description": "Reprograma una cita del contacto actual: cancela la "
                               "cita vieja y reserva el nuevo horario en una sola "
                               "operación atómica. No acepta contact_id. Requiere "
                               "consentimiento de privacidad vigente del contacto.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "old_date": {"type": "string", "description": "Fecha actual YYYY-MM-DD."},
                        "old_time_slot": {"type": "string", "description": "Hora actual HH:MM."},
                        "new_date": {"type": "string", "description": "Nueva fecha YYYY-MM-DD."},
                        "new_time_slot": {"type": "string", "description": "Nueva hora HH:MM."},
                        "type": {"type": "string",
                                 "description": "Tipo: consultation | followup | other.",
                                 "enum": ["consultation", "followup", "other"]},
                        "service_type": {"type": "string",
                                         "description": "Opcional: slug del tipo de "
                                                        "servicio (aplica al nuevo "
                                                        "horario)."},
                        "venue": {"type": "string",
                                  "description": "Opcional: sede/ubicación del evento "
                                                 "(negocios móviles)."},
                    },
                    "required": ["old_date", "old_time_slot", "new_date", "new_time_slot"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "escalate_to_human",
                "description": "Escala la conversación a un humano. Usar cuando el cliente "
                               "lo pida o el tema sea sensible.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reason": {"type": "string", "description": "Motivo de la escalación."}
                    },
                    "required": ["reason"],
                },
            },
        },
    ]
    if enabled is None:
        return catalog
    allowed = set(enabled)
    return [t for t in catalog if t["function"]["name"] in allowed]


TOOLS: list[dict] = build_tools()


# ── Ejecutores ───────────────────────────────────────
def _validation_error(model_cls, args: dict) -> dict | None:
    try:
        return model_cls(**{k: v for k, v in args.items()
                             if k in model_cls.model_fields}).model_dump()
    except Exception as e:
        return {"_error": str(e)}


async def _require_privacy_consent(ctx: "AgentContext") -> str | None:
    """Gating de privacidad (Fase 7c): agendar/reprogramar exige
    `consent_status == "granted"` Y `privacy_terms_version` == versión
    vigente del tenant. Devuelve el error de negocio o None si puede
    operar."""
    contact = await ctx.session.get(Contact, ctx.contact_id)
    if contact is None:
        return "contacto desconocido: no se puede agendar"
    return await privacy_gate_error(ctx.session, ctx.tenant_id, contact)


async def run_tool(name: str, args: dict, ctx: "AgentContext") -> dict:
    args = dict(args or {})
    # Defensa en profundidad: el LLM nunca fija la identidad del contacto.
    args.pop("contact_id", None)

    if name == "search_knowledge_base":
        parsed = _validation_error(SearchArgs, args)
        if "_error" in parsed:
            return {"results": [], "error": parsed["_error"]}
        results = await search_knowledge(
            ctx.session, ctx.tenant_id, parsed["query"], ctx.embedder,
            threshold=args.get("threshold", RAG_THRESHOLD),
        )
        return {"results": results}

    if name == "check_availability":
        parsed = _validation_error(CheckAvailabilityArgs, args)
        if "_error" in parsed:
            return {"available": False, "alternatives": [],
                    "error": parsed["_error"]}
        cal = calmod.MemoryCalendarAdapter(
            ctx.session, ctx.tenant_id, tz=ctx.timezone
        )
        return await cal.check_availability(
            parsed["date"], parsed["time_slot"],
            service_type_slug=parsed.get("service_type"),
            venue=parsed.get("venue"),
            contact_id=str(ctx.contact_id),
        )

    if name == "book_appointment":
        parsed = _validation_error(BookAppointmentArgs, args)
        if "_error" in parsed:
            return {"ok": False, "event_id": None, "start_at": None,
                    "error": parsed["_error"]}
        # Gating de privacidad (Fase 7c): sin consentimiento vigente no
        # se agenda. El guard va ANTES de tocar el calendario.
        consent_err = await _require_privacy_consent(ctx)
        if consent_err:
            return {"ok": False, "event_id": None, "start_at": None,
                    "error": consent_err}
        cal = calmod.MemoryCalendarAdapter(
            ctx.session, ctx.tenant_id, tz=ctx.timezone
        )
        return await cal.book(
            str(ctx.contact_id),
            parsed["date"],
            parsed["time_slot"],
            parsed["type"].value,
            idempotency_key=args.get("idempotency_key"),
            service_type_slug=parsed.get("service_type"),
            venue=parsed.get("venue"),
        )

    if name == "cancel_appointment":
        parsed = _validation_error(CheckAvailabilityArgs, args)
        if "_error" in parsed:
            return {"ok": False, "event_id": None, "error": parsed["_error"]}
        cal = calmod.MemoryCalendarAdapter(
            ctx.session, ctx.tenant_id, tz=ctx.timezone
        )
        return await cal.cancel(
            str(ctx.contact_id), parsed["date"], parsed["time_slot"]
        )

    if name == "reschedule_appointment":
        parsed = _validation_error(RescheduleAppointmentArgs, args)
        if "_error" in parsed:
            return {"ok": False, "event_id": None, "start_at": None,
                    "error": parsed["_error"]}
        # Gating de privacidad (Fase 7c): igual que book_appointment.
        consent_err = await _require_privacy_consent(ctx)
        if consent_err:
            return {"ok": False, "event_id": None, "start_at": None,
                    "error": consent_err}
        cal = calmod.MemoryCalendarAdapter(
            ctx.session, ctx.tenant_id, tz=ctx.timezone
        )
        return await cal.reschedule(
            str(ctx.contact_id),
            parsed["old_date"],
            parsed["old_time_slot"],
            parsed["new_date"],
            parsed["new_time_slot"],
            parsed["type"].value,
            idempotency_key=args.get("idempotency_key"),
            service_type_slug=parsed.get("service_type"),
            venue=parsed.get("venue"),
        )

    if name == "escalate_to_human":
        parsed = _validation_error(EscalateArgs, args)
        reason = parsed.get("reason", "") if "_error" not in parsed else ""
        handoff = Handoff(
            tenant_id=ctx.tenant_id,
            contact_id=ctx.contact_id,
            reason=reason,
            status="open",
        )
        ctx.session.add(handoff)
        # Fase 3: el handoff pone la conversación en modo humano (el drenador
        # silencia al bot mientras mode == "human").
        await set_conversation_mode(
            ctx.session, ctx.tenant_id, ctx.contact_id, MODE_HUMAN
        )
        await ctx.session.commit()
        # Fase 7f: alerta al staff (owner/receptionist) con el motivo. El
        # handoff ya está commiteado: notificar jamás lo revierte.
        try:
            motivo = (reason or "").strip() or "sin motivo indicado"
            await notify_staff(
                ctx.session,
                ctx.tenant_id,
                f"🙋 Handoff: un contacto necesita ayuda humana. "
                f"Motivo: {motivo}.",
                idempotency_key=f"staff-alert:handoff:{handoff.id}",
            )
        except Exception:  # noqa: BLE001 - notificar no revierte el handoff
            logger.exception(
                "Fallo alerta de staff tras handoff %s", handoff.id
            )
        return {"escalated": True, "handoff_id": str(handoff.id)}

    return {"error": f"tool desconocido: {name}"}


class AgentContext:
    """Estado compartido que pasamos a los ejecutores de tools.

    `contact_id` es la identidad autenticada del contacto (la fija el canal /
    el webhook, nunca el LLM). `timezone` es la zona del tenant para el
    calendario.
    """

    def __init__(
        self,
        session,
        tenant_id: uuid.UUID,
        contact_id: uuid.UUID,
        embedder: EmbedderPort,
        timezone: str = "America/Mexico_City",
    ):
        self.session = session
        self.tenant_id = tenant_id
        self.contact_id = contact_id
        self.embedder = embedder
        self.timezone = timezone
