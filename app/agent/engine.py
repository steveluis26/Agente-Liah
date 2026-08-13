"""Motor del agente: loop de tool-calling con límite anti-bucle.

run_agent(): carga system prompt + historial, itera LLM <-> tools hasta
respuesta final (MAX_ITER). No envía nada; devuelve el texto final para que
quien llama lo despache (webhook/sender).
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
    # LLM pequeño "invente" o diga "no sé" sin consultar. No confiamos a ciegas
    # en que el modelo decidirá usar la herramienta.
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
        # search_knowledge_base nosotros y re-inyectamos el resultado para que
        # el LLM redacte con contexto real. Esto garantiza que las FAQs usen la
        # base de conocimiento sin depender de la tool-adherence del proveedor.
        if i == 0 and force_rag and not resp.is_tool_call:
            # RAG con threshold relajado: el embedder puede ser FakeEmbedder
            # (demo local sin OpenAI) donde la similitud coseno es ruidosa.
            # Con OpenAIEmbedder real el tool search_knowledge_base usa 0.75 y
            # funciona bien; aquí garantizamos recuperación local sin depender
            # del proveedor. El LLM recibe el contexto y redacta.
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

        # Ejecuta cada tool_call y feeding results al modelo
        messages.append(
            {
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
            }
        )
        for tc in resp.tool_calls:
            result = await toolmod.run_tool(tc["name"], tc["arguments"], ctx)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(result, default=str),
                }
            )

    # Si agotó iteraciones sin texto final, respuesta de resguardo.
    return (
        "Lo siento, no pude completar tu solicitud en este momento. "
        "¿Quieres que un humano te atienda?"
    )


def get_embedder():
    # El embedder real (OpenAI) se inyecta desde quien llama; para el loop
    # usamos el FakeEmbedder por defecto y se sobreescribe vía set_embedder.
    from app.agent.embedder import FakeEmbedder

    return _embedder_holder["embedder"] or FakeEmbedder()


_embedder_holder = {"embedder": None}


def set_embedder(embedder):
    _embedder_holder["embedder"] = embedder
