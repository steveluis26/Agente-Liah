"""Motor del agente: loop de tool-calling con límite anti-bucle.

run_agent(): carga system prompt + historial, itera LLM <-> tools hasta
respuesta final (MAX_ITER). No envía nada; devuelve el texto final para que
quien llama lo despache (webhook/sender).

El motor es GENÉRICO: prohibido hardcodear literales de cualquier vertical
(ni estética, ni médico, ni academia) y prohibido hardcodear supuestos del
canal (WhatsApp u otro). Todo lo variable por negocio vive en la config del
tenant (`tenant_configs.extra`):
  - kb_triggers: lista de palabras que fuerzan la consulta RAG.
  - sensitive_keywords (o `temas_sensibles`, como lo guarda el onboarding):
    lista de palabras/frases que disparan escalación automática.
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
import logging
import os
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import tools as toolmod
from app.agent.costing import record_turn_usage
from app.agent.ollama_llm import OllamaLLM
from app.agent.ports import EmbedderPort, LLMPort
from app.agent.secrets import (
    EnvSecretProvider,
    SecretProvider,
    require_tenant_openai_key,
)
from app.core.audit import log_event
from app.models import Handoff, Message, Tenant, TenantConfig
from app.models.conversations import MODE_HUMAN, set_conversation_mode

logger = logging.getLogger("liah.engine")

MAX_ITER = 5

# Defaults comerciales del esqueleto (Fase 2): el cliente comercial usa
# OpenAI; Ollama queda como opción local/dev por tenant.
DEFAULT_LLM_PROVIDER = os.getenv("LIAH_DEFAULT_LLM_PROVIDER", "openai")
DEFAULT_LLM_MODEL = os.getenv("LIAH_DEFAULT_LLM_MODEL", "gpt-4o-mini")
DEFAULT_LLM_TEMPERATURE = float(os.getenv("LIAH_DEFAULT_LLM_TEMPERATURE", "0.2"))
DEFAULT_EMBEDDER = os.getenv("LIAH_DEFAULT_EMBEDDER", "fake")

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


# ── Factory de LLM / embedder por tenant (Fase 2) ──────────────
#
# La config vive en `tenant_configs.model_routing` (JSONB), p.ej.:
#   {"llm_provider": "openai", "llm_model": "gpt-4o-mini",
#    "llm_max_tokens": 800, "llm_temperature": 0.2,
#    "embedder": "openai", "tier": "comercial"}
# Defaults: proveedor OpenAI (comercial), modelo gpt-4o-mini, embedder fake
# (cero costo; el tenant comercial lo pone en "openai" en el onboarding).
# La API key se resuelve POR TENANT vía SecretProvider, nunca del env global
# directo (salvo fallback documentado en secrets.py).


async def _tenant_routing(
    session: AsyncSession, tenant_id: uuid.UUID
) -> tuple[dict, str]:
    cfg = (
        await session.execute(
            select(TenantConfig).where(TenantConfig.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    routing = dict(cfg.model_routing or {}) if cfg else {}
    tenant = await session.get(Tenant, tenant_id)
    slug = tenant.slug if tenant else str(tenant_id)
    return routing, slug


async def build_llm_for_tenant(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    secrets: SecretProvider | None = None,
) -> LLMPort:
    """Construye el LLM del tenant según su `model_routing`.

    - `llm_provider: "openai"` (default comercial) -> OpenAILLM con la key del
      tenant resuelta por `secrets`. Sin key: RuntimeError claro (fail fast).
    - `llm_provider: "ollama"` -> OllamaLLM local (dev; costo 0).
    """
    from app.agent.llm import OpenAILLM  # import tardío: evita ciclo

    secrets = secrets or EnvSecretProvider()
    routing, slug = await _tenant_routing(session, tenant_id)
    provider = str(routing.get("llm_provider") or DEFAULT_LLM_PROVIDER).lower()

    if provider == "ollama":
        return OllamaLLM(
            model=routing.get("ollama_model") or None,
            base_url=routing.get("ollama_base_url") or None,
        )

    if provider != "openai":
        raise RuntimeError(
            f"llm_provider desconocido para el tenant '{slug}': '{provider}' "
            "(válidos: 'openai', 'ollama')"
        )
    api_key = require_tenant_openai_key(secrets, slug)
    max_tokens = routing.get("llm_max_tokens")
    temperature = routing.get("llm_temperature", DEFAULT_LLM_TEMPERATURE)
    return OpenAILLM(
        api_key=api_key,
        model=routing.get("llm_model") or DEFAULT_LLM_MODEL,
        max_tokens=int(max_tokens) if max_tokens is not None else None,
        temperature=float(temperature),
    )


def build_embedder_for_tenant(
    routing: dict | None, secrets: SecretProvider | None = None
) -> EmbedderPort:
    """Embedder según `model_routing["embedder"]`: openai | ollama | fake.

    Default: `fake` (cero costo en dev/tests). El tenant comercial usa
    "openai" (misma key del tenant que el chat).
    """
    from app.agent.embedder import FakeEmbedder, OpenAIEmbedder
    from app.agent.ollama_embedder import OllamaEmbedder

    routing = routing or {}
    kind = str(routing.get("embedder") or DEFAULT_EMBEDDER).lower()
    if kind == "openai":
        secrets = secrets or EnvSecretProvider()
        slug = routing.get("_tenant_slug") or ""
        api_key = (
            require_tenant_openai_key(secrets, slug) if slug else None
        )
        return OpenAIEmbedder(api_key=api_key)
    if kind == "ollama":
        return OllamaEmbedder(
            model=routing.get("ollama_embed_model") or None,
            base_url=routing.get("ollama_base_url") or None,
        )
    if kind == "fake":
        return FakeEmbedder()
    raise RuntimeError(
        f"embedder desconocido en model_routing: '{kind}' "
        "(válidos: 'openai', 'ollama', 'fake')"
    )


async def _record_llm_usage(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    contact_id: uuid.UUID,
    llm: LLMPort,
    resp,
    conversation_id: uuid.UUID | None = None,
) -> None:
    """Persiste el usage del turno. Si falla, loguea y el flujo SIGUE.

    El costeo nunca debe romper una conversación con el cliente.
    """
    try:
        usage = dict(getattr(resp, "usage", None) or {})
        if not usage.get("prompt_tokens") and not usage.get("completion_tokens"):
            return  # LLM local/stub sin usage: nada que costear
        model = getattr(llm, "model", None) or "unknown"
        await record_turn_usage(
            session, tenant_id, contact_id, conversation_id, model, usage
        )
    except Exception:
        logger.exception(
            "No se pudo registrar usage del turno (tenant=%s); el flujo continúa",
            tenant_id,
        )


async def run_agent(
    session: AsyncSession,
    llm: LLMPort,
    tenant_id: uuid.UUID,
    contact_id: uuid.UUID,
    user_message: str,
    *,
    embedder: EmbedderPort,
    conversation_id: uuid.UUID | None = None,
) -> str:
    """Ejecuta el loop del agente. El embedder se inyecta explícitamente.

    `conversation_id` (Fase 3) enlaza el costeo del turno con la conversación
    para el panel de métricas; None = comportamiento legacy.
    """
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
    # Fase 5: el onboarding (Fase 4) guarda la lista del perfil como
    # `temas_sensibles`; también se acepta `sensitive_keywords` (nombre que
    # usa la documentación del engine). Sin este fallback, los tenants dados
    # de alta por plantilla NUNCA disparaban la escalación automática.
    sensitive_keywords = tuple(
        kw.lower()
        for kw in (
            policy.get("sensitive_keywords") or policy.get("temas_sensibles") or []
        )
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
        # Costeo por turno (Fase 2): persiste el usage; si falla, loguea y
        # el loop sigue (el costeo jamás rompe la conversación).
        await _record_llm_usage(
            session, tenant_id, contact_id, llm, resp,
            conversation_id=conversation_id,
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
    # Fase 3: el handoff pone la conversación en modo humano (el drenador
    # silencia al bot mientras mode == "human").
    await set_conversation_mode(session, tenant_id, contact_id, MODE_HUMAN)
    return handoff
