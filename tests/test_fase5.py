"""Tests de Fase 5: demo end-to-end del consultorio médico + worker de fondo.

Prueban las mismas 4 rutas que `scripts/demo_consultorio.py` (conocimiento,
acción, handoff, recordatorios) más el ciclo del worker (`app/worker.py`),
todo contra la BD de test con drop_all/create_all por test.

Los veredictos validan RESULTADOS reales (estado en BD / texto final), no
solo retrieval — igual que la demo.
"""
import json
import os
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.agent.calendar import MemoryCalendarAdapter
from app.agent.embedder import FakeEmbedder
from app.agent.engine import HANDOFF_NOTICE, run_agent
from app.agent.ports import LLMResponse
from app.agent.rag import ingest_knowledge
from app.api.onboarding import onboard_tenant
from app.channels.whatsapp.queue import drain_jobs, enqueue_job
from app.core import db as db_mod
from app.core.base import Base
from app.models import (
    Appointment,
    Contact,
    Conversation,
    Handoff,
    Message,
    ReminderLog,
    Tenant,
    TenantConfig,
    WebhookJob,
)
from app.models.conversations import MODE_HUMAN, get_or_create_conversation
from app.reminders import scheduler as scheduler_mod
from app.worker import run_cycle

PW = "Secret-12345678"


@pytest_asyncio.fixture(autouse=True)
async def _schema():
    url = os.getenv("TEST_DATABASE_URL", "").replace("+asyncpg", "")
    import asyncpg

    conn = await asyncpg.connect(url)
    try:
        await conn.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp";')
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    finally:
        await conn.close()
    async with db_mod.engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with db_mod.engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _onboard_clinic(session, slug: str = "clinica-test") -> uuid.UUID:
    result = await onboard_tenant(
        session,
        template_name="consultorio_medico",
        slug=slug,
        nombre=None,
        overrides=None,
        admin_email=f"{slug}@test.mx",
admin_password=PW,
        embedder=FakeEmbedder(),
    )
    return uuid.UUID(result["tenant_id"])


async def _make_contact(s, tenant_id, wa_id, consent="granted") -> Contact:
    contact = Contact(tenant_id=tenant_id, wa_id=wa_id,
                      consent_status=consent)
    s.add(contact)
    await s.flush()
    return contact


def _tc(id_, name, arguments):
    return {"id": id_, "name": name, "arguments": arguments}


class _ScriptedLLM:
    def __init__(self, responses):
        self._responses = list(responses)

    async def chat(self, messages, tools=None, tool_choice=None):
        if self._responses:
            return self._responses.pop(0)
        return LLMResponse(content="fin", finish_reason="stop")


class _KnowledgeEchoLLM:
    """Eco del tool result (no alucina: sin retrieval no hay precio)."""

    def __init__(self, question: str):
        self._question = question

    async def chat(self, messages, tools=None, tool_choice=None):
        for m in reversed(messages):
            if m.get("role") == "tool":
                data = json.loads(m["content"])
                results = data.get("results") or []
                if results:
                    return LLMResponse(content="Con gusto. " + results[0]["content"],
                                       finish_reason="stop")
                return LLMResponse(content="No encontré esa información.",
                                   finish_reason="stop")
        return LLMResponse(
            content=None, finish_reason="tool_calls",
            tool_calls=[_tc("k1", "search_knowledge_base",
                            {"query": self._question})])


# ── Ruta 1: conocimiento (precio en la RESPUESTA FINAL) ──────
@pytest.mark.asyncio
async def test_demo_knowledge_final_answer_contains_price():
    """El onboarding real + RAG real + respuesta final con el precio.

    FakeEmbedder es léxico: se ingiere un FAQ de apoyo (igual que la demo)
    para que la pregunta natural supere el umbral 0.75 del engine.
    """
    question = "¿cuánto cuesta la consulta general?"
    async with db_mod.async_session_maker() as s:
        tid = await _onboard_clinic(s, "clinica-kb")
        await ingest_knowledge(
            s, tid, "FAQ precio",
            "¿Cuánto cuesta la consulta general? Cuesta $600 MXN.",
            FakeEmbedder(),
        )
        contact = await _make_contact(s, tid, "521555009001")
        await s.commit()
        cid = contact.id
    async with db_mod.async_session_maker() as s:
        reply = await run_agent(
            s, _KnowledgeEchoLLM(question), tid, cid, question,
            embedder=FakeEmbedder(),
        )
    assert "$600" in reply, f"la respuesta final debe traer el precio: {reply!r}"


# ── Ruta 2: booking por run_agent + reintento idempotente ─────
@pytest.mark.asyncio
async def test_demo_booking_via_run_agent_idempotent_retry():
    target = (date.today() + timedelta(days=3)).isoformat()

    def _script():
        return [
            LLMResponse(content=None, finish_reason="tool_calls",
                        tool_calls=[_tc("c1", "check_availability",
                                        {"date": target, "time_slot": "10:00"})]),
            LLMResponse(content=None, finish_reason="tool_calls",
                        tool_calls=[_tc("c2", "book_appointment",
                                        {"date": target, "time_slot": "10:00",
                                         "type": "consultation"})]),
            LLMResponse(content="Listo, tu cita quedó agendada.",
                        finish_reason="stop"),
        ]

    async with db_mod.async_session_maker() as s:
        tid = await _onboard_clinic(s, "clinica-book")
        contact = await _make_contact(s, tid, "521555009002")
        await s.commit()
        cid = contact.id

    async with db_mod.async_session_maker() as s:
        reply = await run_agent(
            s, _ScriptedLLM(_script()), tid, cid,
            "Quiero agendar una cita", embedder=FakeEmbedder())
    assert "agendada" in reply
    async with db_mod.async_session_maker() as s:
        appt = (
            await s.execute(
                select(Appointment).where(
                    Appointment.tenant_id == tid,
                    Appointment.contact_id == cid,
                    Appointment.status == "confirmed",
                )
            )
        ).scalar_one_or_none()
    assert appt is not None, "la cita debe existir en BD tras run_agent"

    # Reintento del mismo intento: UNA sola cita.
    async with db_mod.async_session_maker() as s:
        await run_agent(
            s, _ScriptedLLM(_script()), tid, cid,
            "Quiero agendar una cita", embedder=FakeEmbedder())
        n = (
            await s.execute(
                select(func.count()).select_from(Appointment).where(
                    Appointment.tenant_id == tid,
                    Appointment.contact_id == cid,
                )
            )
        ).scalar()
    assert n == 1, "el reintento no debe duplicar la cita"


# ── Ruta 3: handoff por urgencia crea registro y silencia ─────
class _UrgencyLLM:
    async def chat(self, messages, tools=None, tool_choice=None):
        user_text = next(
            (m["content"] for m in reversed(messages)
             if m.get("role") == "user"), "")
        if "pecho" in user_text.lower():
            return LLMResponse(
                content=None, finish_reason="tool_calls",
                tool_calls=[_tc("e1", "escalate_to_human",
                                {"reason": "posible urgencia médica"})])
        return LLMResponse(content="Entendido.", finish_reason="stop")


def _change(wa, wamid, text):
    return {
        "contacts": [{"profile": {"name": "Paciente"}}],
        "messages": [{"from": wa, "id": wamid, "type": "text",
                      "text": {"body": text}}],
    }


@pytest.mark.asyncio
async def test_demo_handoff_urgency_creates_record_and_silences():
    async with db_mod.async_session_maker() as s:
        tid = await _onboard_clinic(s, "clinica-handoff")
        await s.commit()

    wa = "521555009003"
    llm_factory = lambda s_, t_, e_: _UrgencyLLM()  # noqa: E731

    # 1) Mensaje de urgencia por el drenador real.
    async with db_mod.async_session_maker() as s:
        await enqueue_job(
            s, tid, "PNID", _change(wa, "wamid-t5-1",
                                    "me duele mucho el pecho, ¿qué hago?"))
        await s.commit()
    stats = await drain_jobs(db_mod.async_session_maker,
                             llm_factory=llm_factory, dry_run=True)
    assert stats["done"] == 1

    async with db_mod.async_session_maker() as s:
        contact = (
            await s.execute(
                select(Contact).where(
                    Contact.tenant_id == tid, Contact.wa_id == wa))
        ).scalar_one()
        handoff = (
            await s.execute(
                select(Handoff).where(
                    Handoff.tenant_id == tid,
                    Handoff.contact_id == contact.id,
                    Handoff.status == "open",
                )
            )
        ).scalars().first()
        assert handoff is not None, "la urgencia debe CREAR el Handoff"
        conv = await get_or_create_conversation(s, tid, contact.id)
        assert conv.mode == MODE_HUMAN, "la conversación debe quedar en 'human'"
        out1 = (
            await s.execute(
                select(func.count()).select_from(Message).where(
                    Message.tenant_id == tid,
                    Message.contact_id == contact.id,
                    Message.direction == "outbound",
                )
            )
        ).scalar()

    # 2) Siguiente mensaje: el bot guarda silencio (gate del drenador).
    async with db_mod.async_session_maker() as s:
        await enqueue_job(s, tid, "PNID",
                          _change(wa, "wamid-t5-2", "¿siguen ahí?"))
        await s.commit()
    stats = await drain_jobs(db_mod.async_session_maker,
                             llm_factory=llm_factory, dry_run=True)
    assert stats["done"] == 1
    async with db_mod.async_session_maker() as s:
        out2 = (
            await s.execute(
                select(func.count()).select_from(Message).where(
                    Message.tenant_id == tid,
                    Message.contact_id == contact.id,
                    Message.direction == "outbound",
                )
            )
        ).scalar()
    assert out2 == out1, "el bot no debe responder tras el handoff"


@pytest.mark.asyncio
async def test_sensitive_keywords_fallback_to_temas_sensibles():
    """El engine acepta `temas_sensibles` (lo que escribe el onboarding).

    Sin este fallback, los tenants dados de alta por plantilla jamás
    disparaban la escalación automática por palabra clave.
    """
    async with db_mod.async_session_maker() as s:
        t = Tenant(slug="clinica-kw", name="Clínica KW",
                   business_type="consultorio_medico")
        s.add(t)
        await s.flush()
        s.add(TenantConfig(
            tenant_id=t.id,
            system_prompt="Eres Liah.",
            extra={"temas_sensibles": ["dolor de pecho"]},
        ))
        contact = await _make_contact(s, t.id, "521555009004")
        await s.commit()
        tid, cid = t.id, contact.id

    class _NormalLLM:
        async def chat(self, messages, tools=None, tool_choice=None):
            return LLMResponse(content="Respuesta normal.", finish_reason="stop")

    async with db_mod.async_session_maker() as s:
        reply = await run_agent(
            s, _NormalLLM(), tid, cid,
            "tengo un fuerte dolor de pecho, ¿qué hago?",
            embedder=FakeEmbedder(),
        )
        assert reply == HANDOFF_NOTICE
        handoff = (
            await s.execute(
                select(Handoff).where(
                    Handoff.tenant_id == tid, Handoff.contact_id == cid)
            )
        ).scalars().first()
    assert handoff is not None, "temas_sensibles debe disparar el handoff"


# ── Ruta 4: recordatorios sin duplicar en segunda corrida ─────
@pytest.mark.asyncio
async def test_demo_reminders_no_duplicate_on_second_run():
    async with db_mod.async_session_maker() as s:
        tid = await _onboard_clinic(s, "clinica-rem")
        contact = await _make_contact(s, tid, "521555009005")
        await s.commit()
        cid = contact.id

    async with db_mod.async_session_maker() as s:
        cal = MemoryCalendarAdapter(s, tid, tz="America/Mexico_City")
        start = datetime.now(ZoneInfo("America/Mexico_City")) + timedelta(hours=2)
        res = await cal.book(
            str(cid), start.date().isoformat(), start.strftime("%H:%M"),
            "consultation", idempotency_key=f"t5-{uuid.uuid4().hex[:8]}")
        assert res["ok"], f"no se pudo crear la cita: {res}"
        await s.commit()

    async def _count():
        async with db_mod.async_session_maker() as s:
            return (
                await s.execute(
                    select(func.count()).select_from(ReminderLog).where(
                        ReminderLog.tenant_id == tid,
                        ReminderLog.contact_id == cid,
                    )
                )
            ).scalar()

    before = await _count()
    await scheduler_mod.run_once(dry_run=True)
    after_first = await _count()
    await scheduler_mod.run_once(dry_run=True)
    after_second = await _count()

    assert after_first > before, "el scheduler debe generar reminder_log"
    assert after_second == after_first, "la 2da corrida no debe duplicar"


# ── Worker: un ciclo drena un webhook_jobs pendiente ───────────
@pytest.mark.asyncio
async def test_worker_cycle_drains_pending_job():
    async with db_mod.async_session_maker() as s:
        t = Tenant(slug="worker-test", name="Worker Test",
                   business_type="other")
        s.add(t)
        await s.flush()
        s.add(TenantConfig(tenant_id=t.id, system_prompt="Eres Liah."))
        await s.commit()
        tid = t.id
        job = await enqueue_job(
            s, tid, "PNID",
            _change("521555009006", "wamid-t5-w1", "Hola, ¿abren hoy?"))
        await s.commit()
        job_id = job.id

    class _TextLLM:
        async def chat(self, messages, tools=None, tool_choice=None):
            return LLMResponse(content="Sí, abrimos hoy.", finish_reason="stop")

    result = await run_cycle(
        db_mod.async_session_maker,
        llm_factory=lambda s_, t_, e_: _TextLLM(),  # noqa: E731
        dry_run=True,
        run_reminders=False,  # este test solo cubre el drenado
    )
    assert result["drain"]["done"] == 1, result
    assert result["reminder_error"] is None
    async with db_mod.async_session_maker() as s:
        job = await s.get(WebhookJob, job_id)
        assert job.status == "done"
        reply = (
            await s.execute(
                select(Message).where(
                    Message.tenant_id == tid,
                    Message.direction == "outbound",
                )
            )
        ).scalars().first()
    assert reply is not None and "abrimos" in reply.content
