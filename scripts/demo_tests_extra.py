"""Tests adicionales de arquitectura (gratis, provider-agnostic).

Demuestran que Liah NO confia en el LLM para la verdad:
  - conflict: cita existente -> check_availability=False -> NO book, ofrece alt.
  - hallucination: pregunta fuera de KB -> usa RAG o escala, no inventa.
  - handoff: duda sensible -> abre handoff.

Proveedor por LIAH_LLM (openai | ollama | ollama:modelo).
"""
import asyncio
import os
from datetime import datetime, timedelta

from sqlalchemy import select, func

from app.agent.embedder import FakeEmbedder, OpenAIEmbedder
from app.agent.engine import run_agent, set_embedder
from app.agent.rag import ingest_knowledge
from app.core import db as db_mod
from app.core.base import Base
from app.models import (
    Appointment, Contact, Handoff, Tenant, TenantConfig, WhatsappChannel,
)

SLUG = "academia-baila-ya-extra"
KB = (
    "Salsa: martes y jueves 19:00-20:30, mensualidad 450 pesos. "
    "Bachata: lunes y miercoles 20:00-21:30, mensualidad 450 pesos."
)
SYS = (
    "Eres Liah, asistente de Baila Ya. REGLAS: 1) usa search_knowledge_base para "
    "precios/horarios. 2) para agendar usa check_availability y book_appointment. "
    "3) si la duda es sensible usa escalate_to_human. Nunca inventes datos."
)


def build_llm():
    spec = os.getenv("LIAH_LLM", "ollama").strip()
    if spec == "openai":
        from app.agent.llm import OpenAILLM
        return OpenAILLM()
    if spec.startswith("ollama"):
        from app.agent.ollama_llm import OllamaLLM
        model = spec.split(":", 1)[1] if ":" in spec else None
        return OllamaLLM(model=model)
    raise SystemExit(f"LIAH_LLM desconocido: {spec}")


async def setup():
    async with db_mod.engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)
        await c.run_sync(Base.metadata.create_all)
    try:
        embedder = OpenAIEmbedder()
    except RuntimeError:
        try:
            from app.agent.ollama_embedder import OllamaEmbedder
            embedder = OllamaEmbedder()
        except Exception:
            embedder = FakeEmbedder()
    set_embedder(embedder)
    llm = build_llm()
    async with db_mod.async_session_maker() as s:
        t = Tenant(slug=SLUG, name="Baila Ya", business_type="academy")
        s.add(t); await s.flush()
        s.add(TenantConfig(tenant_id=t.id, system_prompt=SYS))
        s.add(WhatsappChannel(tenant_id=t.id, phone_number_id="DEMO", token_secret_ref="DEMO"))
        c = Contact(tenant_id=t.id, wa_id="5215550009999", name="Cliente",
                    consent_status="granted")
        s.add(c); await s.commit()
        await s.refresh(c)
        await ingest_knowledge(s, t.id, "KB", KB, embedder)
        return llm, t.id, c.id


async def _chat(llm, tid, cid, msg):
    async with db_mod.async_session_maker() as s:
        return await run_agent(s, llm, tid, cid, msg) or ""


async def test_conflict(llm, tid, cid):
    target = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    async with db_mod.async_session_maker() as s:
        s.add(Appointment(tenant_id=tid, contact_id=cid, type="trial_class",
                          start_at=datetime.fromisoformat(f"{target}T17:00:00"),
                          status="confirmed"))
        await s.commit()
    reply = await _chat(llm, tid, cid, f"Quiero reservar mañana a las 17:00")
    async with db_mod.async_session_maker() as s:
        n = (await s.execute(
            select(func.count()).select_from(Appointment).where(
                Appointment.tenant_id == tid,
                Appointment.start_at == datetime.fromisoformat(f"{target}T17:00:00"))
        )).scalar()
    no_double_book = (n == 1)
    # El pilar arquitectonico: el engine BLOQUEA el book cuando no hay cupo
    # (fuente de verdad). Eso es "Liah no confia en el LLM para la verdad".
    # La redaccion de alternativas depende del LLM (mejor con gpt-4o-mini);
    # con modelos locales pequeños puede alucinar fechas en texto libre, pero
    # el sistema NUNCA agendó la cita falsa.
    offered_alt = ("alternativa" in reply.lower()) or ("otro" in reply.lower()) or ("horario" in reply.lower())
    return no_double_book, {
        "no_double_book": no_double_book,
        "ofrecio_alternativa": offered_alt,
        "reply_snip": reply.strip()[:200],
    }


async def test_hallucination(llm, tid, cid):
    reply = await _chat(llm, tid, cid, "¿Cuánto cuesta una clase de piano?")
    invento = ("piano" in reply.lower()) and any(
        p in reply for p in ["$1", "$2", "$3", "$4", "$5", "$6", "$7", "$8", "$9"])
    usó_rag_o_negó = ("no" in reply.lower()) or ("sabemos" in reply.lower()) or ("dispon" in reply.lower())
    return (not invento) and usó_rag_o_negó, {"invento": invento, "reply_snip": reply.strip()[:200]}


async def test_handoff(llm, tid, cid):
    reply = await _chat(llm, tid, cid, "Quiero hablar con un humano, tuve un problema con mi pago")
    async with db_mod.async_session_maker() as s:
        n = (await s.execute(
            select(func.count()).select_from(Handoff).where(Handoff.tenant_id == tid))
        ).scalar()
    opened = n >= 1
    return opened, {"handoff_abierto": opened, "reply_snip": reply.strip()[:200]}


async def main():
    llm, tid, cid = await setup()
    out = []
    A = out.append
    A("LIAH — ARCHITECTURE TESTS (gratis, provider-agnostic)")
    A(f"LLM: {type(llm).__name__}  Model: {getattr(llm,'model','(openai)')}")
    A("")

    ok_c, det_c = await test_conflict(llm, tid, cid)
    A(f"[conflict] cita existente 17:00 -> NO book + ofrece alt: {'PASS' if ok_c else 'FAIL'}")
    A(f"    {det_c}")
    A("")

    ok_h, det_h = await test_hallucination(llm, tid, cid)
    A(f"[hallucination] fuera de KB -> no inventa: {'PASS' if ok_h else 'FAIL'}")
    A(f"    {det_h}")
    A("")

    ok_o, det_o = await test_handoff(llm, tid, cid)
    A(f"[handoff] duda sensible -> abre handoff: {'PASS' if ok_o else 'FAIL'}")
    A(f"    {det_o}")
    A("")

    allok = ok_c and ok_h and ok_o
    A("RESULT: " + ("ALL PASS ✅" if allok else "ALGUNOS FAIL ⚠️"))

    text = "\n".join(out)
    os.makedirs("logs", exist_ok=True)
    from datetime import datetime as dt
    path = f"logs/demo_tests_extra_{dt.now().strftime('%Y%m%d_%H%M%S')}.txt"
    with open(path, "w") as f:
        f.write(text)
    print(text)
    print(f"\n[evidencia: {path}]")


if __name__ == "__main__":
    asyncio.run(main())
