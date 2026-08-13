"""Demo End-to-End: Academia de Danza "Baila Ya" (Fase Demo).

Ejecuta la historia completa de un cliente real contra la BD (Postgres real):
  1. Onboarding self-service (ORM) + config de prompt/tono.
  2. Ingesta de KB (Salsa/Bachata/Ballet, precios, horarios) con OpenAIEmbedder.
  3. Conversación por WhatsApp simulada con OpenAILLM (gpt-4o-mini + tool-calling):
     - FAQ -> RAG
     - Agendamiento -> check_availability + book_appointment
  4. Recordatorio proactivo (Fase 2): simula el cron y genera reminder_log.

Requiere OPENAI_API_KEY en el entorno para la prueba real con OpenAI.
"""
import asyncio
import os
from datetime import datetime, timedelta

from sqlalchemy import select, text

from app.agent.calendar import MemoryCalendarAdapter
from app.agent.embedder import FakeEmbedder, OpenAIEmbedder
from app.agent.engine import run_agent, set_embedder
from app.agent.llm import OpenAILLM
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

SLUG = "academia-baila-ya"

KB = """Estilos de danza en Baila Ya:
- Salsa: clases los martes y jueves de 19:00 a 20:30. Mensualidad 450 pesos.
- Bachata: clases los lunes y miércoles de 20:00 a 21:30. Mensualidad 450 pesos.
- Ballet: clases los sabados de 10:00 a 11:30 (infantil 5-12 anos) y 12:00 a 13:30 (juvenil/adulto). Mensualidad 500 pesos.
Edades: aceptamos desde los 5 anos en adelante.
Direccion: Calle Danza 123, Centro.
Clase muestra: ofrecemos una primera clase muestra gratuita de prueba, se agendar bajo disponibilidad.
Reglamento: usar ropa comoda, llegar 10 minutos antes, respetar horarios."""


def _ts():
    return datetime.now().strftime("%H:%M:%S")


async def _reset():
    async with db_mod.engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)


async def _setup_tenant_and_kb(embedder):
    # tenant
    t = Tenant(slug=SLUG, name="Academia Baila Ya", business_type="academy",
               timezone="America/Mexico_City")
    async with db_mod.async_session_maker() as s:
        s.add(t)
        await s.flush()
        cfg = TenantConfig(
            tenant_id=t.id,
            system_prompt=(
                "Eres Liah, la asistente virtual de la Academia de Danza Baila Ya. "
                "Hablas con tono jovial, cercano y profesional. Ayudas con precios, "
                "horarios y agendamiento de clases muestra. Usa las herramientas "
                "disponibles para consultar la base de conocimiento y agendar citas. "
                "Si no sabes algo, pregunta o escala a un humano."
            ),
            tone="jovial",
            privacy_notice="Tus datos se usan solo para atender tu consulta (LFPDPPP).",
            lfpdp_consent_required=True,
        )
        s.add(cfg)
        s.add(WhatsappChannel(tenant_id=t.id, phone_number_id="DEMO_PN",
                              verify_token="demo", token_secret_ref="DEMO"))
        await s.commit()
        tid = t.id

    # KB con embedder real
    async with db_mod.async_session_maker() as s:
        await ingest_knowledge(s, tid, "Info Baila Ya", KB, embedder)
        # plantilla HSM para recordatorio
        s.add(Template(tenant_id=tid, name="recordatorio_clase_muestra",
                       category="utility", language="es",
                       body="Hola {{1}} 👋 Soy Liah de {{2}}. Te esperamos mañana a las {{3}} "
                            "para tu clase muestra de {{4}}. ¿Confirmas tu asistencia?",
                       variables=["name", "tenant", "time", "style"], status="approved"))
        # regla de recordatorio (día anterior)
        s.add(AutomationRule(tenant_id=tid, type="trial_class", enabled=True, params={}))
        await s.commit()
    return tid


async def _chat(tid, contact_id, user_msg, llm):
    print(f"\n🟦 CLIENTE: {user_msg}")
    async with db_mod.async_session_maker() as s:
        reply = await run_agent(s, llm, tid, contact_id, user_msg)
    print(f"🟩 LIAH:    {reply}")
    return reply


async def _make_contact(tid):
    async with db_mod.async_session_maker() as s:
        c = Contact(tenant_id=tid, wa_id="5215550007777", name="Valeria",
                    consent_status="granted")
        s.add(c)
        await s.commit()
        await s.refresh(c)
        return c.id


async def _book_via_tools(tid, contact_id, llm, date, time_slot):
    """Simula el agendamiento usando las mismas tools que el agente."""
    from app.agent import tools as toolmod
    from app.agent.tools import AgentContext

    async with db_mod.async_session_maker() as s:
        ctx = AgentContext(s, tid, contact_id, FakeEmbedder())
        avail = await toolmod.run_tool(
            "check_availability", {"date": date, "time_slot": time_slot}, ctx
        )
        print(f"   [tool] check_availability -> {avail}")
        if avail.get("available"):
            res = await toolmod.run_tool(
                "book_appointment",
                {"contact_id": str(contact_id), "date": date,
                 "time_slot": time_slot, "type": "trial_class"},
                ctx,
            )
            print(f"   [tool] book_appointment -> {res}")
            return res
        return avail


async def _simulate_reminder_cron(tid):
    """Simula el scheduler: cita mañana -> genera reminder_log (dry-run)."""
    from app.reminders import dispatch

    tomorrow = datetime.now() + timedelta(days=1)
    async with db_mod.async_session_maker() as s:
        # insertamos una clase muestra para mañana (como si el agente la agendó)
        c = (await s.execute(select(Contact).where(Contact.tenant_id == tid))).scalar_one()
        appt = Appointment(tenant_id=tid, contact_id=c.id, type="trial_class",
                            start_at=tomorrow.replace(hour=17, minute=0, second=0, microsecond=0))
        s.add(appt)
        await s.commit()

        targets = await dispatch.load_rule_targets(
            s, tid, "Academia Baila Ya", "trial_class", datetime.now()
        )
        print(f"\n⏰ CRON recordatorios: {len(targets)} objetivo(s) para mañana")
        for (rule, contact, scheduled_for, variables) in targets:
            tpl = (await s.execute(
                select(Template).where(Template.tenant_id == tid,
                                        Template.name == "recordatorio_clase_muestra")
            )).scalar_one_or_none()
            ok = await dispatch.dispatch_reminder(
                s, tid, "Academia Baila Ya", rule, contact, tpl,
                scheduled_for, variables, dry_run=True,
            )
            print(f"   -> recordatorio {'ENVIADO(dry-run)' if ok else 'OMITIDO'} "
                  f"a {contact.name}: {variables}")
        # verificar reminder_log
        n = (await s.execute(select(ReminderLog))).scalar()
        print(f"   reminder_log registros: {n}")


async def main():
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise SystemExit(
            "❌ OPENAI_API_KEY no está definida. Exponla antes de correr el demo:\n"
            "   export OPENAI_API_KEY=sk-...\n"
            "   python scripts/demo_academia.py"
        )
    embedder = OpenAIEmbedder()
    set_embedder(embedder)
    llm = OpenAILLM()

    print("=" * 70)
    print("DEMO END-TO-END — Academia de Danza 'Baila Ya' (Agente Liah)")
    print("=" * 70)

    await _reset()
    tid = await _setup_tenant_and_kb(embedder)
    print(f"✅ Tenant creado: {SLUG} ({tid})")
    contact_id = await _make_contact(tid)
    print(f"✅ Contacto demo: Valeria ({contact_id})")

    print("\n--- CONVERSACIÓN WHATSAPP (simulada) ---")
    # 1) FAQ
    await _chat(tid, contact_id, "Hola, ¿cuánto cuestan las clases de Salsa y qué horarios tienen?", llm)
    # 2) Agendamiento (el usuario pide jueves 17:00)
    target_date = (datetime.now() + timedelta(days=3))
    date_str = target_date.strftime("%Y-%m-%d")
    await _chat(tid, contact_id, f"Me gustaría ir a una clase muestra de prueba el {date_str} a las 17:00.", llm)
    # ejecutamos el agendamiento vía tools (como lo haría el agente tras tool-calling)
    await _book_via_tools(tid, contact_id, llm, date_str, "17:00")

    # 3) Recordatorio proactivo (Fase 2)
    await _simulate_reminder_cron(tid)

    print("\n" + "=" * 70)
    print("DEMO COMPLETO ✅")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
