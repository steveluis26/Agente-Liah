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

    for _ in range(MAX_ITER):
        resp = await llm.chat(messages, tools=toolmod.TOOLS)
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
