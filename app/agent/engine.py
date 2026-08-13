"""Motor del agente: loop de tool-calling con límite anti-bucle.

run_agent(): carga system prompt + historial, itera LLM <-> tools hasta
respuesta final (MAX_ITER). No envía nada; devuelve el texto final para que
quien llama lo despache (webhook/sender).

Pilares de la arquitectura (no confiar ciegamente en el LLM):
- Guard RAG: si la consulta es de conocimiento, el engine garantiza el uso de
  la base de conocimiento (no depende de que el modelo decida llamar la tool).
- Guard de agendamiento: antes de book_appointment, el engine valida contra la
  fuente de verdad (calendar). Si no hay cupo, BLOQUEA el book y obliga al
  modelo a ofrecer alternativas con el dato real.
"""
import json
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import tools as toolmod
from app.agent.ports import LLMPort
from app.models import Contact, Message, TenantConfig

MAX_ITER = 5


async def run_agent(
    session: AsyncSession,
    llm: LLMPort,
    tenant_id: uuid.UUID,
    contact_id: uuid.UUID,
    user_message: str,
) -> str:
    # System prompt + config del tenant
    cfg = (
        await session.execute(select(TenantConfig).where(TenantConfig.tenant_id == tenant_id))
    ).scalar_one_or_none()
    system_prompt = (
        cfg.system_prompt
        if cfg
        else "Eres un asistente de atención al cliente por WhatsApp."
    )

    # Historial (más antiguo primero)
    hist_rows = (
        await session.execute(
            select(Message)
            .where(Message.tenant_id == tenant_id, Message.contact_id == contact_id)
            .order_by(Message.created_at.asc())
            .limit(20)
        )
    ).scalars().all()
    messages = [{"role": "system", "content": system_prompt}]
    for m in hist_rows:
        role = "user" if m.direction == "inbound" else "assistant"
        messages.append({"role": role, "content": m.content})
    messages.append({"role": "user", "content": user_message})

    ctx = toolmod.AgentContext(session, tenant_id, contact_id, get_embedder())

    # Inyecta sesión/tenant al LLM si los soporta (p.ej. _StubLLM para RAG en dev).
    for attr, val in (("session", session), ("tenant_id", tenant_id)):
        if hasattr(llm, attr):
            setattr(llm, attr, val)

    # Heurística de conocimiento: en la primera iteración, si la consulta del
    # usuario parece una pregunta de conocimiento (FAQ), forzamos la tool RAG.
    # Esto garantiza que las FAQs usen la base de conocimiento y evita que un
    # LLM pequeño "invente" o diga "no sé" sin consultar.
    _kb_triggers = ("?", "cuanto", "cuánto", "precio", "costo", "coste", "horario",
                    "salsa", "bachata", "ballet", "clase", "estilo", "edad", "reglamento",
                    "direccion", "dirección", "disponible", "informacion", "información")
    force_rag = any(t in user_message.lower() for t in _kb_triggers)
    first_iter_tool_choice = (
        {"type": "function", "function": {"name": "search_knowledge_base"}}
        if force_rag else None
    )

    for i in range(MAX_ITER):
        resp = await llm.chat(
            messages,
            tools=toolmod.TOOLS,
            tool_choice=first_iter_tool_choice if i == 0 else None,
        )

        # Guard de infraestructura (no confiamos ciegamente en el LLM):
        # si forzamos RAG en la primera iteración y el modelo NO devolvió un
        # tool_call (algunos LLM locales ignoran tool_choice), ejecutamos
        # search_knowledge_base nosotros y re-inyectamos el resultado.
        if i == 0 and force_rag and not resp.is_tool_call:
            rag_result = await toolmod.run_tool(
                "search_knowledge_base", {"query": user_message, "threshold": 0.0}, ctx
            )
            if not rag_result.get("results"):
                rag_result = {"results": [{"content": "(sin contexto recuperado)",
                                           "source_id": None, "similarity": 0.0}]}
            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "rag_guard", "type": "function",
                    "function": {"name": "search_knowledge_base",
                                 "arguments": json.dumps({"query": user_message})},
                }],
            })
            messages.append({
                "role": "tool", "tool_call_id": "rag_guard",
                "content": json.dumps(rag_result, default=str),
            })
            continue

        if not resp.is_tool_call:
            return resp.content or ""

        # Registra los tool_calls del assistant para el feed posterior
        messages.append({
            "role": "assistant",
            "content": resp.content,
            "tool_calls": [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])},
                }
                for tc in resp.tool_calls
            ],
        })

        for tc in resp.tool_calls:
            # Guard de orquestación (no confiamos en el LLM para la verdad del
            # calendario): antes de agendar, el engine valida contra la fuente
            # de verdad (MemoryCalendarAdapter). Si el LLM intenta
            # book_appointment sin cupo (o sin haber chequeado), ejecutamos
            # check_availability real y, si no hay cupo, BLOQUEAMOS el book e
            # inyectamos un resultado que obliga al modelo a ofrecer
            # alternativas con el dato real.
            if tc["name"] == "book_appointment":
                args = tc["arguments"]
                date = args.get("date")
                slot = args.get("time_slot")
                pre_check = await toolmod.run_tool(
                    "check_availability", {"date": date, "time_slot": slot}, ctx
                )
                if not pre_check.get("available"):
                    blocked = {
                        "ok": False,
                        "event_id": None,
                        "start_at": None,
                        "error": "sin cupo confirmado por la fuente de verdad; "
                                 "NO se agendó. Ofrece alternativas al cliente.",
                    }
                    messages.append({
                        "role": "tool", "tool_call_id": tc["id"],
                        "content": json.dumps(blocked, default=str),
                    })
                    continue

            result = await toolmod.run_tool(tc["name"], tc["arguments"], ctx)
            messages.append({
                "role": "tool", "tool_call_id": tc["id"],
                "content": json.dumps(result, default=str),
            })

    # Si agotó iteraciones sin texto final, respuesta de resguardo.
    return (
        "Lo siento, no pude completar tu solicitud en este momento. "
        "¿Quieres que un humano te atienda?"
    )


def get_embedder():
    from app.agent.embedder import FakeEmbedder

    return _embedder_holder["embedder"] or FakeEmbedder()


_embedder_holder = {"embedder": None}


def set_embedder(embedder):
    _embedder_holder["embedder"] = embedder
