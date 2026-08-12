"""Herramientas (tools) expuestas al LLM.

Cada tool es un dict OpenAI (name/description/parameters) + una función Python
que lo ejecuta. El engine itera tool_calls y despacha por nombre.
"""
import json
import uuid

from app.agent import calendar as calmod
from app.agent.rag import search_knowledge
from app.models import Handoff


# ── Definiciones (formato OpenAI tools) ──────────────
TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "search_knowledge_base",
            "description": "Busca en la base de conocimiento del negocio (precios, horarios, "
                           "reglamento, FAQs) la respuesta a la duda del cliente.",
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
            "description": "Reserva una cita/clase muestra en el calendario del negocio.",
            "parameters": {
                "type": "object",
                "properties": {
                    "contact_id": {"type": "string", "description": "ID del contacto."},
                    "date": {"type": "string", "description": "Fecha YYYY-MM-DD."},
                    "time_slot": {"type": "string", "description": "Hora HH:MM."},
                    "type": {"type": "string",
                             "description": "Tipo: trial_class | consultation | other."},
                },
                "required": ["contact_id", "date", "time_slot", "type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "escalate_to_human",
            "description": "Escala la conversacion a un humano (recepcion/dueno). Usar cuando "
                           "el cliente lo pida, o la duda sea sensible/legal/medica.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "Motivo de la escalacion."}
                },
                "required": ["reason"],
            },
        },
    },
]


# ── Ejecutores ───────────────────────────────────────
async def run_tool(name: str, args: dict, ctx: "AgentContext") -> dict:
    if name == "search_knowledge_base":
        results = await search_knowledge(
            ctx.session, ctx.tenant_id, args["query"], ctx.embedder
        )
        return {"results": results}

    if name == "check_availability":
        cal = calmod.MemoryCalendarAdapter(ctx.session, ctx.tenant_id)
        return await cal.check_availability(args["date"], args["time_slot"])

    if name == "book_appointment":
        cal = calmod.MemoryCalendarAdapter(ctx.session, ctx.tenant_id)
        return await cal.book(
            args["contact_id"], args["date"], args["time_slot"], args["type"]
        )

    if name == "escalate_to_human":
        handoff = Handoff(
            tenant_id=ctx.tenant_id,
            contact_id=uuid.UUID(args["contact_id"]),
            reason=args.get("reason", ""),
            status="open",
        )
        ctx.session.add(handoff)
        await ctx.session.commit()
        return {"escalated": True}

    return {"error": f"tool desconocido: {name}"}


class AgentContext:
    """Estado compartido que pasamos a los ejecutores de tools."""

    def __init__(self, session, tenant_id: uuid.UUID, contact_id: uuid.UUID,
                 embedder):
        self.session = session
        self.tenant_id = tenant_id
        self.contact_id = contact_id
        self.embedder = embedder
