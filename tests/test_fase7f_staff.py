"""Tests de Fase 7f: modo staff por WhatsApp + briefings + notas de voz.

Contra BD de test aislada (ver conftest; en iteración se usa
pyme_agent_test_7f). Cubre:
- detección staff por número: jamás entra al flujo de cliente (sin Contact
  de cliente, sin aviso de privacidad)
- scoping por rol: specialist solo ve su agenda; owner ve todo
- los 3 comandos (agenda hoy, quién a la hora, huecos mañana) con datos reales
- comando desconocido -> ayuda
- briefing matutino: contenido y hora; enabled=false no envía; idempotente
- alertas: al cancelar, al ofrecer hueco de waitlist y al crear handoff
- notas de voz: el stub transcribe y el texto alimenta al agente
"""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from app.agent import tools as toolmod
from app.agent.calendar import MemoryCalendarAdapter
from app.agent.embedder import FakeEmbedder
from app.agent.staff_notify import notify_staff
from app.agent.transcriber import (
    STUB_MARKER,
    StubTranscriber,
    WhisperTranscriber,
    transcriber_for_tenant,
)
from app.agent.waitlist import join_waitlist
from app.channels.whatsapp.queue import _DevStubLLM, drain_jobs, enqueue_job
from app.core import db as db_mod
from app.core.base import Base
from app.models import (
    Appointment,
    Contact,
    EventLog,
    Message,
    Resource,
    ServiceType,
    StaffMember,
    Tenant,
    TenantConfig,
    TenantPrivacyTerms,
)

TZ = "America/Mexico_City"
STAFF_WA = "521550000001"
OWNER_WA = "521550000002"
CLIENT_WA = "521550000099"


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


# ── helpers ────────────────────────────────────────────────────────────

async def _tenant(s, slug, timezone=TZ):
    t = Tenant(slug=slug, name=f"Tenant {slug}", business_type="test",
               timezone=timezone)
    s.add(t)
    await s.flush()
    return t


def _terms(s, tenant_id, version="1.0"):
    s.add(TenantPrivacyTerms(
        tenant_id=tenant_id, version=version, titulo="Aviso",
        texto="Aviso de prueba.",
    ))


def _staff(s, tenant_id, wa_id, nombre, role, resource_id=None):
    m = StaffMember(tenant_id=tenant_id, wa_id=wa_id, nombre=nombre,
                    role=role, resource_id=resource_id)
    s.add(m)
    return m


def _resource(s, tenant_id, slug, tipo="room", movilidad="fixed",
              capacidad=1, especialidad=None):
    r = Resource(tenant_id=tenant_id, slug=slug, nombre=slug, tipo=tipo,
                 movilidad=movilidad, capacidad=capacidad,
                 especialidad=especialidad)
    s.add(r)
    return r


def _service(s, tenant_id, slug, duracion_min=60, recursos=None):
    st = ServiceType(
        tenant_id=tenant_id, slug=slug, nombre=slug,
        duracion_min=duracion_min,
        recursos_requeridos=recursos or [],
        buffers={}, traslado={"modo": "none"},
    )
    s.add(st)
    return st


def _contact(s, tenant_id, wa_id, name=None, consent_status="granted",
             privacy_terms_version="1.0"):
    c = Contact(tenant_id=tenant_id, wa_id=wa_id, name=name,
                consent_status=consent_status,
                privacy_terms_version=privacy_terms_version)
    s.add(c)
    return c


def _local_now():
    return datetime.now(ZoneInfo(TZ))


def _change_text(wa_id, wamid, body):
    return {
        "messages": [{
            "from": wa_id, "id": wamid, "type": "text",
            "text": {"body": body}, "timestamp": "1700000000",
        }],
        "contacts": [{"profile": {"name": "Alguien"}}],
    }


def _change_audio(wa_id, wamid):
    return {
        "messages": [{
            "from": wa_id, "id": wamid, "type": "audio",
            "audio": {"id": "media-123", "mime_type": "audio/ogg"},
            "timestamp": "1700000000",
        }],
        "contacts": [{"profile": {"name": "Alguien"}}],
    }


async def _drain():
    return await drain_jobs(
        db_mod.async_session_maker,
        limit=50,
        llm_factory=lambda s, t, e: _DevStubLLM(s, t, e),
        dry_run=True,
    )


async def _outbound_texts(session, tenant_id):
    rows = (
        await session.execute(
            Message.__table__.select().where(
                Message.tenant_id == tenant_id,
                Message.direction == "outbound",
            ).order_by(Message.created_at.asc())
        )
    ).all()
    return [r.content for r in rows]


async def _event_kinds(session, tenant_id):
    from sqlalchemy import select
    rows = (
        await session.execute(
            select(EventLog).where(EventLog.tenant_id == tenant_id)
        )
    ).scalars().all()
    return [r.type for r in rows]


# ── 1. detección staff vs cliente ─────────────────────────────────────

@pytest.mark.asyncio
async def test_staff_no_entra_al_flujo_de_cliente():
    async with db_mod.async_session_maker() as s:
        t = await _tenant(s, "t7f-staff")
        _terms(s, t.id)  # con términos, un cliente SÍ recibiría el aviso
        _staff(s, t.id, STAFF_WA, "Jefa", "owner")
        await s.commit()
        await enqueue_job(s, t.id, "pnid-1",
                          _change_text(STAFF_WA, "wamid-staff-1",
                                       "mi agenda de hoy"))
        await s.commit()
    stats = await _drain()
    assert stats["done"] == 1
    async with db_mod.async_session_maker() as s:
        # No se creó Contact de cliente para el número staff (solo el de
        # canal contact_type="staff", que no cuenta como cliente).
        rows = (
            await s.execute(
                Contact.__table__.select().where(
                    Contact.tenant_id == t.id, Contact.wa_id == STAFF_WA,
                )
            )
        ).all()
        assert all(r.contact_type == "staff" for r in rows)
        texts = await _outbound_texts(s, t.id)
        assert len(texts) == 1
        # Respuesta de staff (agenda), jamás el aviso de privacidad.
        assert "agenda" in texts[0].lower()
        assert "aviso" not in texts[0].lower()


@pytest.mark.asyncio
async def test_cliente_si_recibe_aviso_de_privacidad():
    async with db_mod.async_session_maker() as s:
        t = await _tenant(s, "t7f-cli")
        _terms(s, t.id)
        await s.commit()
        await enqueue_job(s, t.id, "pnid-1",
                          _change_text(CLIENT_WA, "wamid-cli-1", "hola"))
        await s.commit()
    stats = await _drain()
    assert stats["done"] == 1
    async with db_mod.async_session_maker() as s:
        texts = await _outbound_texts(s, t.id)
        assert len(texts) == 1
        # El cliente sí recibe el aviso de privacidad (contraste con staff).
        assert "privacidad" in texts[0].lower() or "aviso" in texts[0].lower()


# ── 2. scoping por rol + comandos ─────────────────────────────────────

async def _agenda_setup(s, slug):
    """Dos especialistas con una cita cada uno hoy a las 09:00 y 11:00."""
    t = await _tenant(s, slug)
    r1 = _resource(s, t.id, "esp-1", tipo="specialist")
    r2 = _resource(s, t.id, "esp-2", tipo="specialist")
    sala = _resource(s, t.id, "sala-1", tipo="room")
    await s.flush()  # los resources necesitan id antes de enlazar el staff
    _service(s, t.id, "svc-1", recursos=[{"recurso": "esp-1"},
                                         {"recurso": "sala-1"}])
    _service(s, t.id, "svc-2", recursos=[{"recurso": "esp-2"},
                                         {"recurso": "sala-1"}])
    esp1 = _staff(s, t.id, STAFF_WA, "Dra Ana", "specialist",
                  resource_id=r1.id)
    _staff(s, t.id, OWNER_WA, "Dueño", "owner")
    ana = _contact(s, t.id, "521550000101", name="Ana Paciente")
    beto = _contact(s, t.id, "521550000102", name="Beto Paciente")
    await s.flush()
    cal = MemoryCalendarAdapter(s, t.id)
    hoy = _local_now().strftime("%Y-%m-%d")
    assert (await cal.book(str(ana.id), hoy, "09:00", "other",
                           service_type_slug="svc-1"))["ok"] is True
    assert (await cal.book(str(beto.id), hoy, "11:00", "other",
                           service_type_slug="svc-2"))["ok"] is True
    await s.commit()
    return t, hoy


@pytest.mark.asyncio
async def test_specialist_solo_ve_su_agenda():
    async with db_mod.async_session_maker() as s:
        t, _ = await _agenda_setup(s, "t7f-scope")
        await enqueue_job(s, t.id, "pnid-1",
                          _change_text(STAFF_WA, "wamid-s1", "mi agenda de hoy"))
        await s.commit()
    assert (await _drain())["done"] == 1
    async with db_mod.async_session_maker() as s:
        texts = await _outbound_texts(s, t.id)
        assert len(texts) == 1
        assert "Ana Paciente" in texts[0]
        assert "Beto Paciente" not in texts[0]
        assert "09:00" in texts[0]


@pytest.mark.asyncio
async def test_owner_ve_toda_la_agenda():
    async with db_mod.async_session_maker() as s:
        t, _ = await _agenda_setup(s, "t7f-owner")
        await enqueue_job(s, t.id, "pnid-1",
                          _change_text(OWNER_WA, "wamid-o1", "¿cuántas citas tengo hoy?"))
        await s.commit()
    assert (await _drain())["done"] == 1
    async with db_mod.async_session_maker() as s:
        texts = await _outbound_texts(s, t.id)
        assert "Ana Paciente" in texts[0]
        assert "Beto Paciente" in texts[0]


@pytest.mark.asyncio
async def test_who_at_hora():
    async with db_mod.async_session_maker() as s:
        t, _ = await _agenda_setup(s, "t7f-who")
        await enqueue_job(s, t.id, "pnid-1",
                          _change_text(OWNER_WA, "wamid-o2",
                                       "¿quién es el de las 11:00?"))
        await enqueue_job(s, t.id, "pnid-1",
                          _change_text(OWNER_WA, "wamid-o3",
                                       "¿quién es el de las 15:00?"))
        await s.commit()
    assert (await _drain())["done"] == 2
    async with db_mod.async_session_maker() as s:
        texts = await _outbound_texts(s, t.id)
        assert len(texts) == 2
        assert "Beto Paciente" in texts[0]
        assert "No hay cita hoy a las 15:00" in texts[1]


@pytest.mark.asyncio
async def test_free_slots_manana():
    async with db_mod.async_session_maker() as s:
        t = await _tenant(s, "t7f-free")
        _resource(s, t.id, "sala-1", tipo="room")
        _service(s, t.id, "svc-1", duracion_min=60,
                 recursos=[{"recurso": "sala-1"}])
        _staff(s, t.id, OWNER_WA, "Dueño", "receptionist")
        c = _contact(s, t.id, "521550000103", name="Cli")
        await s.flush()
        cal = MemoryCalendarAdapter(s, t.id)
        from datetime import timedelta
        manana = (_local_now() + timedelta(days=1)).strftime("%Y-%m-%d")
        assert (await cal.book(str(c.id), manana, "09:00", "other",
                               service_type_slug="svc-1"))["ok"] is True
        await s.commit()
        await enqueue_job(s, t.id, "pnid-1",
                          _change_text(OWNER_WA, "wamid-o4",
                                       "¿qué huecos hay mañana?"))
        await s.commit()
    assert (await _drain())["done"] == 1
    async with db_mod.async_session_maker() as s:
        texts = await _outbound_texts(s, t.id)
        assert len(texts) == 1
        assert "10:00" in texts[0]   # libre tras la cita de 09:00–10:00
        assert "08:00" in texts[0]   # libre antes
        assert "09:00" not in texts[0]
        assert "09:30" not in texts[0]


@pytest.mark.asyncio
async def test_free_slots_scoping_specialist():
    async with db_mod.async_session_maker() as s:
        t = await _tenant(s, "t7f-freescope")
        esp = _resource(s, t.id, "esp-1", tipo="specialist")
        _resource(s, t.id, "sala-1", tipo="room")
        await s.flush()  # el resource necesita id antes de enlazar el staff
        _service(s, t.id, "svc-con-esp", duracion_min=60,
                 recursos=[{"recurso": "esp-1"}, {"recurso": "sala-1"}])
        _service(s, t.id, "svc-sin-esp", duracion_min=60,
                 recursos=[{"recurso": "sala-1"}])
        _staff(s, t.id, STAFF_WA, "Especialista", "specialist",
               resource_id=esp.id)
        await s.flush()
        await enqueue_job(s, t.id, "pnid-1",
                          _change_text(STAFF_WA, "wamid-s2",
                                       "¿qué huecos hay mañana?"))
        await s.commit()
    assert (await _drain())["done"] == 1
    async with db_mod.async_session_maker() as s:
        texts = await _outbound_texts(s, t.id)
        assert "svc-con-esp" in texts[0]
        assert "svc-sin-esp" not in texts[0]


@pytest.mark.asyncio
async def test_comando_desconocido_da_ayuda():
    async with db_mod.async_session_maker() as s:
        t = await _tenant(s, "t7f-help")
        _staff(s, t.id, STAFF_WA, "Jefa", "owner")
        await s.commit()
        await enqueue_job(s, t.id, "pnid-1",
                          _change_text(STAFF_WA, "wamid-s3", "blablabla"))
        await s.commit()
    assert (await _drain())["done"] == 1
    async with db_mod.async_session_maker() as s:
        texts = await _outbound_texts(s, t.id)
        assert "asistente del negocio" in texts[0]
        assert "mi agenda de hoy" in texts[0]


# ── 3. briefing matutino ──────────────────────────────────────────────

async def _briefing_setup(s, slug, enabled=True, hour="07:30"):
    t = await _tenant(s, slug)
    _resource(s, t.id, "sala-1", tipo="room")
    _service(s, t.id, "svc-1", recursos=[{"recurso": "sala-1"}])
    _staff(s, t.id, STAFF_WA, "Jefa", "owner")
    c = _contact(s, t.id, "521550000104", name="Ana Paciente")
    s.add(TenantConfig(
        tenant_id=t.id, system_prompt="x",
        extra={"staff_briefing": {"enabled": enabled, "hour": hour,
                                 "roles": ["owner"]}},
    ))
    await s.flush()
    cal = MemoryCalendarAdapter(s, t.id)
    hoy = _local_now().strftime("%Y-%m-%d")
    assert (await cal.book(str(c.id), hoy, "09:00", "other",
                           service_type_slug="svc-1"))["ok"] is True
    await s.commit()
    return t


def _at_today(hh, mm):
    return _local_now().replace(hour=hh, minute=mm, second=0, microsecond=0)


@pytest.mark.asyncio
async def test_briefing_envia_a_la_hora_configurada():
    from app.reminders.staff_briefing import send_due_staff_briefings
    async with db_mod.async_session_maker() as s:
        t = await _briefing_setup(s, "t7f-brief")
    out = await send_due_staff_briefings(
        db_mod.async_session_maker, dry_run=True, now=_at_today(7, 30))
    assert out[str(t.id)]["sent"] == 1
    async with db_mod.async_session_maker() as s:
        texts = await _outbound_texts(s, t.id)
        assert len(texts) == 1
        assert "Buenos días, Jefa" in texts[0]
        assert "09:00" in texts[0]
        assert "Ana Paciente" in texts[0]
    # Idempotente: segunda pasada el mismo día no duplica.
    out2 = await send_due_staff_briefings(
        db_mod.async_session_maker, dry_run=True, now=_at_today(7, 35))
    assert out2[str(t.id)]["sent"] == 0
    async with db_mod.async_session_maker() as s:
        assert len(await _outbound_texts(s, t.id)) == 1


@pytest.mark.asyncio
async def test_briefing_fuera_de_hora_no_envia():
    from app.reminders.staff_briefing import send_due_staff_briefings
    async with db_mod.async_session_maker() as s:
        t = await _briefing_setup(s, "t7f-brief2")
    out = await send_due_staff_briefings(
        db_mod.async_session_maker, dry_run=True, now=_at_today(10, 0))
    assert out[str(t.id)]["sent"] == 0
    async with db_mod.async_session_maker() as s:
        assert await _outbound_texts(s, t.id) == []


@pytest.mark.asyncio
async def test_briefing_disabled_no_envia():
    from app.reminders.staff_briefing import send_due_staff_briefings
    async with db_mod.async_session_maker() as s:
        t = await _briefing_setup(s, "t7f-brief3", enabled=False)
    out = await send_due_staff_briefings(
        db_mod.async_session_maker, dry_run=True, now=_at_today(7, 30))
    assert out[str(t.id)]["sent"] == 0
    async with db_mod.async_session_maker() as s:
        assert await _outbound_texts(s, t.id) == []


# ── 4. alertas ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_alerta_al_staff():
    async with db_mod.async_session_maker() as s:
        t = await _tenant(s, "t7f-cancel")
        _resource(s, t.id, "sala-1", tipo="room")
        _service(s, t.id, "svc-1", recursos=[{"recurso": "sala-1"}])
        _staff(s, t.id, STAFF_WA, "Jefa", "owner")
        c = _contact(s, t.id, "521550000105", name="Ana Paciente")
        await s.flush()
        cal = MemoryCalendarAdapter(s, t.id)
        hoy = _local_now().strftime("%Y-%m-%d")
        assert (await cal.book(str(c.id), hoy, "10:00", "other",
                               service_type_slug="svc-1"))["ok"] is True
        res = await cal.cancel(str(c.id), hoy, "10:00")
        assert res["ok"] is True
        await s.commit()
    async with db_mod.async_session_maker() as s:
        texts = await _outbound_texts(s, t.id)
        assert any("Cita cancelada" in m and "Ana Paciente" in m
                   for m in texts)


@pytest.mark.asyncio
async def test_waitlist_offer_alerta_al_staff():
    async with db_mod.async_session_maker() as s:
        t = await _tenant(s, "t7f-wait")
        _resource(s, t.id, "sala-1", tipo="room")
        _service(s, t.id, "svc-1", recursos=[{"recurso": "sala-1"}])
        _staff(s, t.id, OWNER_WA, "Recep", "receptionist")
        cancela = _contact(s, t.id, "521550000106", name="El que cancela")
        espera = _contact(s, t.id, "521550000107", name="El que espera")
        await s.flush()
        await join_waitlist(s, t.id, espera.id, "svc-1")
        cal = MemoryCalendarAdapter(s, t.id)
        hoy = _local_now().strftime("%Y-%m-%d")
        assert (await cal.book(str(cancela.id), hoy, "10:00", "other",
                               service_type_slug="svc-1"))["ok"] is True
        res = await cal.cancel(str(cancela.id), hoy, "10:00")
        assert res["ok"] is True
        await s.commit()
    async with db_mod.async_session_maker() as s:
        texts = await _outbound_texts(s, t.id)
        # oferta al de la lista + alerta al staff del hueco liberado
        assert any("Se liberó un lugar" in m for m in texts)
        assert any("Hueco liberado" in m and "El que espera" in m
                   for m in texts)


@pytest.mark.asyncio
async def test_handoff_alerta_al_staff():
    async with db_mod.async_session_maker() as s:
        t = await _tenant(s, "t7f-hand")
        _staff(s, t.id, STAFF_WA, "Jefa", "owner")
        c = _contact(s, t.id, CLIENT_WA, name="Cli")
        await s.flush()
        ctx = toolmod.AgentContext(s, t.id, c.id, FakeEmbedder())
        res = await toolmod.run_tool(
            "escalate_to_human", {"reason": "pide hablar con un humano"}, ctx)
        assert res["escalated"] is True
        await s.commit()
    async with db_mod.async_session_maker() as s:
        texts = await _outbound_texts(s, t.id)
        assert any("Handoff" in m and "pide hablar con un humano" in m
                   for m in texts)


@pytest.mark.asyncio
async def test_notify_staff_fallo_no_revierte():
    # notify_staff nunca levanta aunque un miembro falle.
    async with db_mod.async_session_maker() as s:
        t = await _tenant(s, "t7f-notfail")
        _staff(s, t.id, STAFF_WA, "Jefa", "owner")
        await s.commit()
        res = await notify_staff(s, t.id, "hola", dry_run=True)
        assert res != {}
        assert all(v == "sent" for v in res.values())


# ── 5. notas de voz ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_transcriber_stub_y_config():
    async with db_mod.async_session_maker() as s:
        t = await _tenant(s, "t7f-trx")
        await s.commit()
        tr = await transcriber_for_tenant(s, t.id)
        assert isinstance(tr, StubTranscriber)
        tx = await tr.transcribe("media-x")
        assert STUB_MARKER in (tx["text"] or "")
        # provider desconocido -> cae al stub sin levantar
        s.add(TenantConfig(tenant_id=t.id, system_prompt="x",
                           extra={"transcriber": {"provider": "raro"}}))
        await s.flush()
        tr2 = await transcriber_for_tenant(s, t.id)
        assert isinstance(tr2, StubTranscriber)


@pytest.mark.asyncio
async def test_whisper_no_implementado_devuelve_error():
    w = WhisperTranscriber(None, None)
    tx = await w.transcribe("media-x")
    assert tx["text"] is None
    assert "no implementado" in (tx["error"] or "")


@pytest.mark.asyncio
async def test_audio_cliente_alimenta_al_agente():
    async with db_mod.async_session_maker() as s:
        t = await _tenant(s, "t7f-audio")
        _terms(s, t.id)
        _contact(s, t.id, CLIENT_WA, name="Cli", consent_status="granted",
                 privacy_terms_version="1.0")
        await s.commit()
        await enqueue_job(s, t.id, "pnid-1",
                          _change_audio(CLIENT_WA, "wamid-au-1"))
        await s.commit()
    assert (await _drain())["done"] == 1
    async with db_mod.async_session_maker() as s:
        inbound = (
            await s.execute(
                Message.__table__.select().where(
                    Message.tenant_id == t.id,
                    Message.direction == "inbound",
                )
            )
        ).all()
        assert len(inbound) == 1
        # El inbound guardado es la transcripción, no vacío.
        assert STUB_MARKER in inbound[0].content
        kinds = await _event_kinds(s, t.id)
        assert "message.transcribed" in kinds
        texts = await _outbound_texts(s, t.id)
        assert len(texts) == 1
        # El agente respondió al texto transcrito (stub dev: sin RAG hits).
        assert "No encontré esa información" in texts[0]


@pytest.mark.asyncio
async def test_audio_staff_va_al_manejador_de_staff():
    async with db_mod.async_session_maker() as s:
        t = await _tenant(s, "t7f-audiostaff")
        _terms(s, t.id)
        _staff(s, t.id, STAFF_WA, "Jefa", "owner")
        await s.commit()
        await enqueue_job(s, t.id, "pnid-1",
                          _change_audio(STAFF_WA, "wamid-au-2"))
        await s.commit()
    assert (await _drain())["done"] == 1
    async with db_mod.async_session_maker() as s:
        texts = await _outbound_texts(s, t.id)
        assert len(texts) == 1
        # La transcripción ("[nota de voz...]") no matchea comandos -> ayuda.
        assert "mi agenda de hoy" in texts[0]
        kinds = await _event_kinds(s, t.id)
        assert "staff.message_transcribed" in kinds
