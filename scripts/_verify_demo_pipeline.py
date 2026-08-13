"""Verificación local del pipeline del demo SIN OpenAI (stub tool-calling).

Esto NO es el demo del usuario (que usa OPENAI_API_KEY real). Es una
verificación interna para confirmar que el pipeline end-to-end funciona contra
Postgres real: RAG -> tools (check/book) -> appointments -> reminder_log.

El stub imita el contrato de OpenAI tool-calling: decide la tool y devuelve
argumentos parseables, igual que lo haría gpt-4o-mini.
"""
import asyncio
import os
from datetime import datetime, timedelta

from sqlalchemy import select, func

from app.agent.calendar import MemoryCalendarAdapter
from app.agent.embedder import FakeEmbedder, OpenAIEmbedder
from app.agent.engine import run_agent, set_embedder
from app.agent.rag import ingest_knowledge
from app.core import db as db_mod
from app.core.base import Base
from app.models import (
    Appointment,
    AutomationRule,
    Contact,
    ReminderLog,
    Template,
    Tenant,
    TenantConfig,
    WhatsappChannel,
)
from app.reminders import dispatch

SLUG = "academia-baila-ya-verify"
KB = (
    "Salsa: martes y jueves 19:00-20:30, mensualidad 450 pesos. "
    "Bachata: lunes y miercoles 20:00-21:30, mensualidad 450 pesos. "
    "Ballet: sabados 10:00-11:30 infantil, 12:00-13:30 juvenil, mensualidad 500 pesos."
)


class StubLLM:
    """Imita tool-calling de OpenAI de forma determinista para el demo."""
    session = None
    tenant_id = None

    async def chat(self, messages, tools=None, tool_choice=None):
        from app.agent.ports import LLMResponse

        user = messages[-1]["content"].lower()
        if "cuanto" in user or "precio" in user or "horario" in user or "salsa" in user:
            return LLMResponse(
                content=None,
                finish_reason="tool_calls",
                tool_calls=[{"id": "c1", "name": "search_knowledge_base",
                             "arguments": {"query": "precio horario salsa"}}],
            )
        # tras tool result, responde con la info recuperada
        last_tool = messages[-1].get("content", "")
        if "results" in str(last_tool):
            return LLMResponse(
                content="Las clases de Salsa son martes y jueves de 19:00 a 20:30, "
                        "con una mensualidad de $450 pesos. ¿Quieres agendar una clase muestra?",
                finish_reason="stop",
            )
        return LLMResponse(content="Listo, con gusto te ayudo.", finish_reason="stop")


async def main():
    # usa OpenAIEmbedder si hay key, si no FakeEmbedder (la KB es small)
    try:
        embedder = OpenAIEmbedder()
        print("Usando OpenAIEmbedder")
    except RuntimeError:
        embedder = FakeEmbedder()
        print("Usando FakeEmbedder (sin OPENAI_API_KEY)")
    set_embedder(embedder)
    llm = StubLLM()

    async with db_mod.engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)
        await c.run_sync(Base.metadata.create_all)

    async with db_mod.async_session_maker() as s:
        t = Tenant(slug=SLUG, name="Baila Ya", business_type="academy")
        s.add(t); await s.flush()
        s.add(TenantConfig(tenant_id=t.id, system_prompt="Eres Liah."))
        s.add(WhatsappChannel(tenant_id=t.id, phone_number_id="DEMO", token_secret_ref="DEMO"))
        s.add(Template(tenant_id=t.id, name="recordatorio_clase_muestra",
                       category="utility", language="es",
                       body="Hola {{1}} de {{2}}", variables=["name", "tenant"],
                       status="approved"))
        s.add(AutomationRule(tenant_id=t.id, type="trial_class", enabled=True))
        await s.commit()
        tid = t.id

    async with db_mod.async_session_maker() as s:
        await ingest_knowledge(s, tid, "KB", KB, embedder)
        contact = Contact(tenant_id=tid, wa_id="5215550007777", name="Valeria",
                          consent_status="granted")
        s.add(contact); await s.commit(); await s.refresh(contact)
        cid = contact.id

    # 1) FAQ via agente (RAG)
    async with db_mod.async_session_maker() as s:
        r1 = await run_agent(s, llm, tid, cid, "¿Cuánto cuestan las clases de salsa y horarios?")
    print("LIAH FAQ:", r1)

    # 2) Agendamiento via tools
    from app.agent import tools as toolmod
    from app.agent.tools import AgentContext
    target = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")
    async with db_mod.async_session_maker() as s:
        ctx = AgentContext(s, tid, cid, FakeEmbedder())
        avail = await toolmod.run_tool("check_availability",
                                       {"date": target, "time_slot": "17:00"}, ctx)
        print("availability:", avail)
        book = await toolmod.run_tool("book_appointment",
                                      {"contact_id": str(cid), "date": target,
                                       "time_slot": "17:00", "type": "trial_class"}, ctx)
        print("book:", book)

    # 3) Recordatorio (cron simulado)
    tomorrow = datetime.now() + timedelta(days=1)
    async with db_mod.async_session_maker() as s:
        appt = Appointment(tenant_id=tid, contact_id=cid, type="trial_class",
                            start_at=tomorrow.replace(hour=17, minute=0, microsecond=0))
        s.add(appt); await s.commit()
        targets = await dispatch.load_rule_targets(s, tid, "Baila Ya", "trial_class", datetime.now())
        print("cron targets:", len(targets))
        for (rule, contact, sched, vars_) in targets:
            tpl = (await s.execute(select(Template).where(
                Template.tenant_id == tid, Template.name == "recordatorio_clase_muestra")
            )).scalar_one_or_none()
            ok = await dispatch.dispatch_reminder(
                s, tid, "Baila Ya", rule, contact, tpl, sched, vars_, dry_run=True)
            print("reminder enviado(dry):", ok, vars_)
        n_log = (await s.execute(select(func.count()).select_from(ReminderLog))).scalar()
        n_appt = (await s.execute(select(func.count()).select_from(Appointment))).scalar()
    print(f"VERIFY: appointments={n_appt}, reminder_log={n_log}")
    assert n_appt >= 1 and n_log >= 1
    print("PIPELINE OK ✅")


if __name__ == "__main__":
    asyncio.run(main())
