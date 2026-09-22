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
import re
import uuid
from enum import Enum

from pydantic import BaseModel, field_validator

from app.agent import calendar as calmod
from app.agent.ports import EmbedderPort
from app.agent.rag import RAG_THRESHOLD, search_knowledge
from app.models import Handoff
from app.models.conversations import MODE_HUMAN, set_conversation_mode


class AppointmentType(str, Enum):
    trial_class = "trial_class"
    consultation = "consultation"
    other = "other"


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SLOT_RE = re.compile(r"^\d{2}:\d{2}(-\d{2}:\d{2})?$")


class SearchArgs(BaseModel):
    query: str


class CheckAvailabilityArgs(BaseModel):
    date: str
    time_slot: str

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
                "description": "Consulta si hay cupo para agendar en una fecha y hora.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "date": {"type": "string", "description": "Fecha YYYY-MM-DD."},
                        "time_slot": {"type": "string",
                                      "description": "Hora HH:MM o rango HH:MM-HH:MM."},
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
                               "para quien escribe.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "date": {"type": "string", "description": "Fecha YYYY-MM-DD."},
                        "time_slot": {"type": "string",
                                      "description": "Hora HH:MM."},
                        "type": {"type": "string",
                                 "description": "Tipo: trial_class | consultation | other.",
                                 "enum": ["trial_class", "consultation", "other"]},
                    },
                    "required": ["date", "time_slot", "type"],
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
        return await cal.check_availability(parsed["date"], parsed["time_slot"])

    if name == "book_appointment":
        parsed = _validation_error(BookAppointmentArgs, args)
        if "_error" in parsed:
            return {"ok": False, "event_id": None, "start_at": None,
                    "error": parsed["_error"]}
        cal = calmod.MemoryCalendarAdapter(
            ctx.session, ctx.tenant_id, tz=ctx.timezone
        )
        return await cal.book(
            str(ctx.contact_id),
            parsed["date"],
            parsed["time_slot"],
            parsed["type"].value,
            idempotency_key=args.get("idempotency_key"),
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
