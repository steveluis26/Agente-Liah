"""Motor del agente: loop de tool-calling con límite anti-bucle.

run_agent(): carga system prompt + historial, itera LLM <-> tools hasta
respuesta final (MAX_ITER). No envía nada; devuelve el texto final para que
quien llama lo despache (webhook/sender).

El motor es GENÉRICO: prohibido hardcodear literales de cualquier vertical
(ni estética, ni médico, ni academia) y prohibido hardcodear supuestos del
canal (WhatsApp u otro). Todo lo variable por negocio vive en la config del
tenant (`tenant_configs.extra`):
  - kb_triggers: lista de palabras que fuerzan la consulta RAG.
  - sensitive_keywords: lista de palabras que disparan escalación automática.
  - enabled_tools: subconjunto del catálogo de tools (por tier).

Pilares de la arquitectura (no confiar ciegamente en el LLM):
- Guard RAG: si la consulta es de conocimiento, el engine garantiza el uso de
  la base de conocimiento (no depende de que el modelo decida llamar la tool).
- Guard de agendamiento: antes de book_appointment, el engine valida contra la
  fuente de verdad (calendar). Si no hay cupo, BLOQUEA el book y obliga al
  modelo a ofrecer alternativas con el dato real.
- Escalamiento automático: baja confianza / iteraciones agotadas / tema
  sensible CREA un Handoff (no solo lo sugiere en texto).
"""
import json
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import tools as toolmod
from app.agent.ports import EmbedderPort, LLMPort
from app.core.audit import log_event
from app.models import Handoff, Message, Tenant, TenantConfig

MAX_ITER = 5

# Disparadores RAG genéricos (sin vertical): preguntas y temas transversales
# de negocio (precios, horarios, ubicación, servicios). Cada tenant puede
# sobreescribir vía tenant_configs.extra["kb_triggers"].
DEFAULT_KB_TRIGGERS = (
    "?", "cuanto", "cuánto", "cuesta", "precio", "costo", "coste",
    "horario", "horarios", "direccion", "dirección", "ubicacion", "ubicación",
    "informacion", "información", "servicio", "servicios", "telefono",
    "teléfono", "contacto",
)

HANDOFF_NOTICE = (
    "Voy a pasarte con una persona del equipo para atenderte mejor. "
    "En un momento te contactan por este mismo medio."
)


async def run_agent(
    session: AsyncSession,
    llm: LLMPort,
    tenant_id: uuid.UUID,
    contact_id: uuid.UUID,
    user_message: str,
    *,
    embedder: EmbedderPort,
) -> str:
    """Ejecuta el loop del agente. El embedder se inyecta explícitamente."""
    # Config del tenant (system prompt + política del agente).
    cfg = (
        await session.execute(
            select(TenantConfig).where(TenantConfig.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    system_prompt = (
        cfg.system_prompt
        if cfg
        else "Eres un asistente de atención al cliente."
    )
    policy = dict(cfg.extra or {}) if cfg else {}
    kb_triggers = tuple(policy.get("kb_triggers") or DEFAULT_KB_TRIGGERS)
    sensitive_keywords = tuple(
        kw.lower() for kw in (policy.get("sensitive_keywords") or [])
    )
    enabled_tools = policy.get("enabled_tools")  # None = catálogo completo
    tools = toolmod.build_tools(enabled_tools)
    tool_names = {t["function"]["name"] for t in tools}
    tenant_row = await session.get(Tenant, tenant_id)
    tenant_tz = (
        tenant_row.timezone
        if tenant_row and tenant_row.timezone
        else "America/Mexico_City"
    )

    # Escalación automática por tema sensible: se crea el Handoff ANTES del
    # loop, el bot no intenta responder el tema.
    lowered = user_message.lower()
    if sensitive_keywords and any(kw in lowered for kw in sensitive_keywords):
        await _create_handoff(
            session, tenant_id, contact_id,
            reason="tema sensible detectado por política del tenant",
        )
        await log_event(session, tenant_id, "agent.escalated",
                        {"contact_id": str(contact_id),
                         "reason": "sensitive_keyword"})
        await session.commit()
        return HANDOFF_NOTICE

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

    ctx = toolmod.AgentContext(session, tenant_id, contact_id, embedder,
                               timezone=tenant_tz)

    # Heurística de conocimiento: en la primera iteración, si la consulta del
    # usuario parece una pregunta de conocimiento (FAQ), forzamos la tool RAG.
    force_rag = (
        "search_knowledge_base" in tool_names
        and any(t in lowered for t in kb_triggers)
    )
    first_iter_tool_choice = (
        {"type": "function", "function": {"name": "search_knowledge_base"}}
        if force_rag else None
    )

    for i in range(MAX_ITER):
        resp = await llm.chat(
            messages,
            tools=tools,
            tool_choice=first_iter_tool_choice if i == 0 else None,
        )

        # Guard de infraestructura (no confiamos ciegamente en el LLM):
        # si forzamos RAG en la primera iteración y el modelo NO devolvió un
        # tool_call (algunos LLM locales ignoran tool_choice), ejecutamos
        # search_knowledge_base nosotros y re-inyectamos el resultado.
        if i == 0 and force_rag and not resp.is_tool_call:
            rag_result = await toolmod.run_tool(
                "search_knowledge_base", {"query": user_message}, ctx
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
            content = (resp.content or "").strip()
            if not content:
                # Baja confianza: el modelo no produjo respuesta final.
                await _create_handoff(
                    session, tenant_id, contact_id,
                    reason="el modelo no produjo respuesta final",
                )
                await session.commit()
                return HANDOFF_NOTICE
            return content

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
            if tc["name"] not in tool_names:
                messages.append({
                    "role": "tool", "tool_call_id": tc["id"],
                    "content": json.dumps({"error": f"tool no habilitada: {tc['name']}"}),
                })
                continue
            # Guard de orquestación (no confiamos en el LLM para la verdad del
            # calendario): antes de agendar, el engine valida contra la fuente
            # de verdad. Si no hay cupo, BLOQUEAMOS el book e inyectamos un
            # resultado que obliga al modelo a ofrecer alternativas reales.
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
                                 "NO se agendó. Ofrece estas alternativas reales: "
                                 f"{pre_check.get('alternatives', [])}",
                    }
                    messages.append({
                        "role": "tool", "tool_call_id": tc["id"],
                        "content": json.dumps(blocked, default=str),
                    })
                    continue
                # Clave de idempotencia determinista: reintentar el MISMO book
                # (mismo tenant/contacto/fecha/hora/tipo) no duplica la cita.
                args = dict(args)
                args.setdefault(
                    "idempotency_key",
                    _booking_key(tenant_id, contact_id, date, slot,
                                 args.get("type", "other")),
                )
                tc = dict(tc, arguments=args)

            result = await toolmod.run_tool(tc["name"], tc["arguments"], ctx)
            messages.append({
                "role": "tool", "tool_call_id": tc["id"],
                "content": json.dumps(result, default=str),
            })

            if tc["name"] == "escalate_to_human" and result.get("escalated"):
                # La tool ya creó el Handoff; el bot cierra con aviso.
                await session.commit()
                return HANDOFF_NOTICE

    # Iteraciones agotadas sin respuesta final: escalación automática real
    # (se CREA el Handoff, no solo se sugiere en texto).
    await _create_handoff(
        session, tenant_id, contact_id,
        reason=f"iteraciones agotadas ({MAX_ITER}) sin respuesta final",
    )
    await log_event(session, tenant_id, "agent.escalated",
                    {"contact_id": str(contact_id),
                     "reason": "max_iter_exhausted"})
    await session.commit()
    return HANDOFF_NOTICE


def _booking_key(tenant_id, contact_id, date, slot, atype) -> str:
    """Clave de idempotencia determinista para un intento de agenda."""
    return f"book:{tenant_id}:{contact_id}:{date}:{slot}:{atype}"


async def _create_handoff(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    contact_id: uuid.UUID,
    reason: str,
) -> Handoff:
    handoff = Handoff(
        tenant_id=tenant_id,
        contact_id=contact_id,
        reason=reason,
        status="open",
    )
    session.add(handoff)
    return handoff
