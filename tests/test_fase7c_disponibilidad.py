"""Tests de Fase 7c: motor de disponibilidad por recursos, consentimiento
de privacidad y lista de espera.

Contra BD de test (postgres 127.0.0.1:5433; ver conftest). Cubre:
- traslape de consultorio (room) y de especialista rechazados
- buffers setup/teardown respetados
- traslados: imposible rechazado / factible aceptado (+ matcheo de zonas)
- capacidad > 1 permite N traslapes pero no N+1
- booking sin consentimiento -> error (gating en tools)
- afirmativo otorga consentimiento (versión + timestamp)
- versión nueva de términos exige re-aceptar
- al cancelar, el primero de la lista de espera recibe oferta
- traslape de la propia persona, tipo desconocido, idempotencia de
  join_waitlist, no persistencia de nombre sin consentimiento
"""
import os
from datetime import datetime

import pytest
import pytest_asyncio

from app.agent import tools as toolmod
from app.agent.availability import (
    _travel_minutes,
    check_resource_availability,
)
from app.agent.calendar import MemoryCalendarAdapter
from app.agent.consent import apply_privacy_gate
from app.agent.embedder import FakeEmbedder
from app.agent.waitlist import join_waitlist
from app.channels.whatsapp.queue import _get_or_create_contact
from app.core import db as db_mod
from app.core.base import Base
from app.models import (
    Appointment,
    AppointmentResource,
    Contact,
    Resource,
    ServiceType,
    Tenant,
    TenantPrivacyTerms,
    WaitlistEntry,
)

DATE = "2026-10-05"  # lunes fijo para los tests


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

async def _tenant(s, slug):
    t = Tenant(slug=slug, name=f"Tenant {slug}", business_type="test")
    s.add(t)
    await s.flush()
    return t


def _resource(s, tenant_id, slug, tipo="room", movilidad="fixed",
              capacidad=1, especialidad=None):
    r = Resource(
        tenant_id=tenant_id, slug=slug, nombre=slug, tipo=tipo,
        movilidad=movilidad, capacidad=capacidad, especialidad=especialidad,
    )
    s.add(r)
    return r


def _service(s, tenant_id, slug, duracion_min=60, recursos=None,
             buffers=None, traslado=None):
    st = ServiceType(
        tenant_id=tenant_id, slug=slug, nombre=slug,
        duracion_min=duracion_min,
        recursos_requeridos=recursos or [],
        buffers=buffers or {},
        traslado=traslado or {"modo": "none"},
    )
    s.add(st)
    return st


def _terms(s, tenant_id, version="1.0"):
    pt = TenantPrivacyTerms(
        tenant_id=tenant_id, version=version, titulo="Aviso de privacidad",
        texto="Aviso de prueba: tus datos se usan solo para agendar.",
    )
    s.add(pt)
    return pt


def _contact(s, tenant_id, wa_id, consent_status="none",
             privacy_terms_version=None):
    c = Contact(
        tenant_id=tenant_id, wa_id=wa_id, consent_status=consent_status,
        privacy_terms_version=privacy_terms_version,
    )
    s.add(c)
    return c


async def _setup_basico(s, slug):
    """Tenant + room cap 1 + servicio de 60 min que requiere la room."""
    t = await _tenant(s, slug)
    _resource(s, t.id, "room-1", tipo="room")
    _service(
        s, t.id, "servicio-1", duracion_min=60,
        recursos=[{"recurso": "room-1"}],
    )
    await s.flush()
    return t


class _FakeSender:
    """SenderPort de prueba: captura envíos sin tocar Meta."""

    def __init__(self):
        self.sent = []

    async def send_text(self, tenant_id, contact_id, channel_contact_id,
                        text, *, idempotency_key=None, dry_run=False):
        self.sent.append({
            "tenant_id": tenant_id, "contact_id": contact_id,
            "channel_contact_id": channel_contact_id, "text": text,
            "idempotency_key": idempotency_key, "dry_run": dry_run,
        })
        return {"message_id": "fake", "meta_message_id": None,
                "status": "dry_run", "error": None}


# ── motor: capacidad / traslapes ────────────────────────────────────────

@pytest.mark.asyncio
async def test_room_overlap_rejected():
    async with db_mod.async_session_maker() as s:
        t = await _setup_basico(s, "t7c-room")
        c = _contact(s, t.id, "521550001", "granted", "1.0")
        await s.flush()
        cal = MemoryCalendarAdapter(s, t.id)
        r1 = await cal.book(str(c.id), DATE, "10:00", "other",
                            service_type_slug="servicio-1")
        assert r1["ok"] is True
        # Misma room, intervalo traslapado -> no disponible.
        check = await check_resource_availability(
            s, t.id, "servicio-1", DATE, "10:30")
        assert check["available"] is False
        assert check["conflicts"][0]["type"] == "resource_capacity"
        assert "room-1" in check["reason"]
        r2 = await cal.book(str(c.id), DATE, "10:30", "other",
                            service_type_slug="servicio-1")
        assert r2["ok"] is False
        # Sin traslape -> disponible.
        check2 = await check_resource_availability(
            s, t.id, "servicio-1", DATE, "11:00")
        assert check2["available"] is True


@pytest.mark.asyncio
async def test_specialist_overlap_rejected():
    async with db_mod.async_session_maker() as s:
        t = await _tenant(s, "t7c-esp")
        _resource(s, t.id, "esp-1", tipo="specialist",
                  especialidad="pediatría")
        _resource(s, t.id, "esp-2", tipo="specialist",
                  especialidad="general")
        _service(
            s, t.id, "servicio-ped", duracion_min=60,
            recursos=[{"tipo": "specialist", "cantidad": 1,
                       "especialidad": "pediatría"}],
        )
        c = _contact(s, t.id, "521550002", "granted", "1.0")
        await s.flush()
        cal = MemoryCalendarAdapter(s, t.id)
        assert (await cal.book(str(c.id), DATE, "10:00", "other",
                               service_type_slug="servicio-ped"))["ok"] is True
        check = await check_resource_availability(
            s, t.id, "servicio-ped", DATE, "10:30")
        assert check["available"] is False
        assert check["conflicts"][0]["resource"] == "esp-1"


@pytest.mark.asyncio
async def test_setup_teardown_buffers_respected():
    async with db_mod.async_session_maker() as s:
        t = await _tenant(s, "t7c-buf")
        _resource(s, t.id, "room-1", tipo="room")
        _service(
            s, t.id, "servicio-buf", duracion_min=60,
            recursos=[{"recurso": "room-1"}],
            buffers={"setup": 10, "teardown": 15},
        )
        c = _contact(s, t.id, "521550003", "granted", "1.0")
        await s.flush()
        cal = MemoryCalendarAdapter(s, t.id)
        # 09:00 ocupa [08:50, 10:15] con buffers.
        assert (await cal.book(str(c.id), DATE, "09:00", "other",
                               service_type_slug="servicio-buf"))["ok"] is True
        # 10:15 ocupa [10:05, ...] -> traslapa el teardown -> rechazado.
        check = await check_resource_availability(
            s, t.id, "servicio-buf", DATE, "10:15")
        assert check["available"] is False
        # 10:30 ocupa [10:20, ...] -> toca el borde, no traslapa -> ok.
        check2 = await check_resource_availability(
            s, t.id, "servicio-buf", DATE, "10:30")
        assert check2["available"] is True


@pytest.mark.asyncio
async def test_capacity_allows_n_not_n_plus_1():
    async with db_mod.async_session_maker() as s:
        t = await _tenant(s, "t7c-cap")
        _resource(s, t.id, "sala-1", tipo="room", capacidad=2)
        _service(
            s, t.id, "servicio-cap", duracion_min=60,
            recursos=[{"recurso": "sala-1"}],
        )
        a = _contact(s, t.id, "521550010", "granted", "1.0")
        b = _contact(s, t.id, "521550011", "granted", "1.0")
        c = _contact(s, t.id, "521550012", "granted", "1.0")
        await s.flush()
        cal = MemoryCalendarAdapter(s, t.id)
        assert (await cal.book(str(a.id), DATE, "10:00", "other",
                               service_type_slug="servicio-cap"))["ok"] is True
        assert (await cal.book(str(b.id), DATE, "10:30", "other",
                               service_type_slug="servicio-cap"))["ok"] is True
        # Tercer traslape (10:15 pisa a las dos anteriores) -> rechazado.
        r3 = await cal.book(str(c.id), DATE, "10:15", "other",
                            service_type_slug="servicio-cap")
        assert r3["ok"] is False
        assert "capacidad 2" in r3["error"]
        # El book con recursos creó las filas AppointmentResource.
        from sqlalchemy import func, select
        n = await s.scalar(
            select(func.count()).select_from(AppointmentResource).where(
                AppointmentResource.tenant_id == t.id)
        )
        assert n == 2


# ── motor: traslados ───────────────────────────────────────────────────

async def _setup_mobile(s, slug):
    t = await _tenant(s, slug)
    _resource(s, t.id, "equipo-1", tipo="equipment", movilidad="mobile")
    _service(
        s, t.id, "servicio-movil", duracion_min=60,
        recursos=[{"recurso": "equipo-1"}],
        buffers={"setup": 60, "teardown": 45},
        traslado={"modo": "per_zone", "default_min": 60,
                  "zonas": {"misma_sede": 0}},
    )
    await s.flush()
    return t


@pytest.mark.asyncio
async def test_travel_impossible_rejected():
    async with db_mod.async_session_maker() as s:
        t = await _setup_mobile(s, "t7c-trav-no")
        c = _contact(s, t.id, "521550020", "granted", "1.0")
        await s.flush()
        cal = MemoryCalendarAdapter(s, t.id)
        # 16:00 en Xalapa ocupa [15:00, 17:45] con buffers.
        assert (await cal.book(str(c.id), DATE, "16:00", "other",
                               service_type_slug="servicio-movil",
                               venue="Xalapa"))["ok"] is True
        # 19:00 en Veracruz: hueco de 15 min < 60 min de viaje -> rechazado.
        # ("Veracruz" no está en zonas -> default_min.)
        check = await check_resource_availability(
            s, t.id, "servicio-movil", DATE, "19:00", venue="Veracruz")
        assert check["available"] is False
        assert check["conflicts"][0]["type"] == "travel"
        assert "60 min" in check["reason"]


@pytest.mark.asyncio
async def test_travel_feasible_accepted():
    async with db_mod.async_session_maker() as s:
        t = await _setup_mobile(s, "t7c-trav-si")
        c = _contact(s, t.id, "521550021", "granted", "1.0")
        await s.flush()
        cal = MemoryCalendarAdapter(s, t.id)
        assert (await cal.book(str(c.id), DATE, "16:00", "other",
                               service_type_slug="servicio-movil",
                               venue="Xalapa"))["ok"] is True
        # 20:00 en Veracruz: hueco de 75 min >= 60 min de viaje -> aceptado.
        check = await check_resource_availability(
            s, t.id, "servicio-movil", DATE, "20:00", venue="Veracruz")
        assert check["available"] is True
        r = await cal.book(str(c.id), DATE, "20:00", "other",
                           service_type_slug="servicio-movil",
                           venue="Veracruz")
        assert r["ok"] is True


def test_travel_zone_matching():
    cfg = {"modo": "per_zone", "default_min": 60,
           "zonas": {"xalapa": 0, "veracruz": 45}}
    # Igualdad exacta normalizada (mayúsculas/acentos no importan).
    assert _travel_minutes(cfg, "Xalapa", "Veracruz") == 45
    assert _travel_minutes(cfg, "Xalapa", "Xalapa") == 0
    assert _travel_minutes(cfg, None, None) == 0
    assert _travel_minutes(cfg, "Verácruz", "XALAPA") == 0
    # Zona no listada -> default_min.
    assert _travel_minutes(cfg, "Xalapa", "Puebla") == 60
    assert _travel_minutes({"modo": "none"}, "Xalapa", "Puebla") == 0
    assert _travel_minutes({"modo": "fixed", "fixed_min": 30},
                           "Xalapa", "Puebla") == 30


# ── motor: persona y config ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_person_overlap_rejected():
    async with db_mod.async_session_maker() as s:
        t = await _tenant(s, "t7c-person")
        _resource(s, t.id, "room-1", tipo="room")
        _resource(s, t.id, "room-2", tipo="room")
        _service(s, t.id, "s1", duracion_min=60,
                 recursos=[{"recurso": "room-1"}])
        _service(s, t.id, "s2", duracion_min=60,
                 recursos=[{"recurso": "room-2"}])
        c = _contact(s, t.id, "521550030", "granted", "1.0")
        await s.flush()
        cal = MemoryCalendarAdapter(s, t.id)
        assert (await cal.book(str(c.id), DATE, "10:00", "other",
                               service_type_slug="s1"))["ok"] is True
        # room-2 está libre, pero la PERSONA ya tiene cita 10:00-11:00.
        check = await check_resource_availability(
            s, t.id, "s2", DATE, "10:30", contact_id=c.id)
        assert check["available"] is False
        assert check["conflicts"][0]["type"] == "person_overlap"


@pytest.mark.asyncio
async def test_unknown_service_type_clear_error():
    async with db_mod.async_session_maker() as s:
        t = await _tenant(s, "t7c-unknown")
        await s.flush()
        check = await check_resource_availability(
            s, t.id, "servicio-fantasma", DATE, "10:00")
        assert check["available"] is False
        assert "servicio-fantasma" in check["reason"]
        assert check["alternatives"] == []


# ── gating de privacidad en tools ──────────────────────────────────────

@pytest.mark.asyncio
async def test_book_without_consent_fails():
    async with db_mod.async_session_maker() as s:
        t = await _tenant(s, "t7c-noconsent")
        _terms(s, t.id, "1.0")
        c = _contact(s, t.id, "521550040", "none")
        await s.flush()
        ctx = toolmod.AgentContext(s, t.id, c.id, FakeEmbedder())
        res = await toolmod.run_tool(
            "book_appointment",
            {"date": DATE, "time_slot": "10:00", "type": "other"}, ctx)
        assert res["ok"] is False
        assert "privacidad" in res["error"]
        res2 = await toolmod.run_tool(
            "reschedule_appointment",
            {"old_date": DATE, "old_time_slot": "10:00",
             "new_date": DATE, "new_time_slot": "11:00",
             "type": "other"}, ctx)
        assert res2["ok"] is False
        assert "privacidad" in res2["error"]


@pytest.mark.asyncio
async def test_book_with_consent_succeeds_legacy():
    async with db_mod.async_session_maker() as s:
        t = await _tenant(s, "t7c-siconsent")
        _terms(s, t.id, "1.0")
        c = _contact(s, t.id, "521550041", "granted", "1.0")
        await s.flush()
        ctx = toolmod.AgentContext(s, t.id, c.id, FakeEmbedder())
        res = await toolmod.run_tool(
            "book_appointment",
            {"date": DATE, "time_slot": "10:00", "type": "other"}, ctx)
        assert res["ok"] is True


@pytest.mark.asyncio
async def test_book_without_terms_configured_is_open():
    """Sin fila de términos, la puerta está abierta (tenants legacy)."""
    async with db_mod.async_session_maker() as s:
        t = await _tenant(s, "t7c-sinterms")
        c = _contact(s, t.id, "521550042", "none")
        await s.flush()
        ctx = toolmod.AgentContext(s, t.id, c.id, FakeEmbedder())
        res = await toolmod.run_tool(
            "book_appointment",
            {"date": DATE, "time_slot": "10:00", "type": "other"}, ctx)
        assert res["ok"] is True


# ── flujo de consentimiento ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_first_message_sends_terms_and_sets_pending():
    async with db_mod.async_session_maker() as s:
        t = await _tenant(s, "t7c-terms1")
        _terms(s, t.id, "1.0")
        c = _contact(s, t.id, "521550050", "none")
        await s.flush()
        gate = await apply_privacy_gate(s, t.id, c, "hola, buenos días")
        assert gate["handled"] is True
        assert c.consent_status == "pending"
        assert "Aviso de prueba" in gate["reply"]
        assert "sí" in gate["reply"]


@pytest.mark.asyncio
async def test_affirmative_grants_consent_with_version_and_timestamp():
    async with db_mod.async_session_maker() as s:
        t = await _tenant(s, "t7c-terms2")
        _terms(s, t.id, "1.0")
        c = _contact(s, t.id, "521550051", "pending")
        await s.flush()
        gate = await apply_privacy_gate(s, t.id, c, "Sí, acepto")
        assert gate["handled"] is False  # el flujo normal continúa
        assert gate["transition"] == "granted"
        assert c.consent_status == "granted"
        assert c.privacy_terms_version == "1.0"
        assert c.consent_at is not None


@pytest.mark.asyncio
async def test_negative_revokes_with_minimal_reply():
    async with db_mod.async_session_maker() as s:
        t = await _tenant(s, "t7c-terms3")
        _terms(s, t.id, "1.0")
        c = _contact(s, t.id, "521550052", "pending")
        await s.flush()
        gate = await apply_privacy_gate(s, t.id, c, "no acepto")
        assert gate["handled"] is True
        assert gate["transition"] == "revoked"
        assert c.consent_status == "revoked"
        assert "Sin tu aceptación" in gate["reply"]
        # Y un "no" pelado también es negativa clara.
        c2 = _contact(s, t.id, "521550053", "pending")
        await s.flush()
        gate2 = await apply_privacy_gate(s, t.id, c2, "No.")
        assert gate2["transition"] == "revoked"


@pytest.mark.asyncio
async def test_new_terms_version_requires_reaccept():
    async with db_mod.async_session_maker() as s:
        t = await _tenant(s, "t7c-terms4")
        pt = _terms(s, t.id, "1.0")
        c = _contact(s, t.id, "521550054", "granted", "1.0")
        await s.flush()
        # El tenant publica una versión nueva.
        pt.version = "2.0"
        pt.texto = "Aviso de prueba v2: texto actualizado."
        await s.flush()
        gate = await apply_privacy_gate(s, t.id, c, "hola")
        assert gate["handled"] is True
        assert gate["transition"] == "reaccept_required"
        assert c.consent_status == "pending"
        assert "v2" in gate["reply"]
        # Y agendar queda bloqueado hasta re-aceptar.
        ctx = toolmod.AgentContext(s, t.id, c.id, FakeEmbedder())
        res = await toolmod.run_tool(
            "book_appointment",
            {"date": DATE, "time_slot": "10:00", "type": "other"}, ctx)
        assert res["ok"] is False
        # Re-acepta -> granted con la versión nueva.
        gate2 = await apply_privacy_gate(s, t.id, c, "de acuerdo")
        assert gate2["transition"] == "granted"
        assert c.consent_status == "granted"
        assert c.privacy_terms_version == "2.0"


@pytest.mark.asyncio
async def test_name_not_persisted_without_consent():
    async with db_mod.async_session_maker() as s:
        t = await _tenant(s, "t7c-noname")
        _terms(s, t.id, "1.0")
        await s.flush()
        c = await _get_or_create_contact(s, t.id, "521550060", "Ana")
        assert c.name is None  # sin consentimiento: no se persiste
        # Tras otorgar, el siguiente mensaje sí lo persiste.
        c.consent_status = "granted"
        c.privacy_terms_version = "1.0"
        await s.flush()
        c2 = await _get_or_create_contact(s, t.id, "521550060", "Ana")
        assert c2.name == "Ana"


# ── lista de espera ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_offers_waitlist_to_first_qualifying():
    async with db_mod.async_session_maker() as s:
        t = await _setup_basico(s, "t7c-wait")
        a = _contact(s, t.id, "521550070", "granted", "1.0")
        b = _contact(s, t.id, "521550071", "granted", "1.0")
        c = _contact(s, t.id, "521550072", "granted", "1.0")
        await s.flush()
        cal = MemoryCalendarAdapter(s, t.id)
        assert (await cal.book(str(a.id), DATE, "10:00", "other",
                               service_type_slug="servicio-1"))["ok"] is True
        eb = await join_waitlist(s, t.id, b.id, "servicio-1")
        eb.created_at = datetime(2026, 9, 1, 10, 0)
        ec = await join_waitlist(s, t.id, c.id, "servicio-1")
        ec.created_at = datetime(2026, 9, 1, 11, 0)
        # Idempotencia: doble alta no duplica.
        assert (await join_waitlist(s, t.id, b.id, "servicio-1")).id == eb.id
        await s.flush()

        fake = _FakeSender()
        res = await cal.cancel(str(a.id), DATE, "10:00",
                               waitlist_sender=fake)
        assert res["ok"] is True
        assert res["freed_slot"]["service_type_slug"] == "servicio-1"
        assert res["freed_slot"]["venue"] is None
        wl = res["waitlist"]
        assert wl["offered"] is True
        assert wl["contact_id"] == str(b.id)

        await s.refresh(eb)
        await s.refresh(ec)
        assert eb.status == "offered"   # el primero califica
        assert ec.status == "waiting"   # el segundo sigue esperando
        assert len(fake.sent) == 1
        assert fake.sent[0]["channel_contact_id"] == "521550071"
        assert "Se liberó" in fake.sent[0]["text"]


@pytest.mark.asyncio
async def test_cancel_without_service_type_skips_waitlist():
    async with db_mod.async_session_maker() as s:
        t = await _tenant(s, "t7c-wait2")
        a = _contact(s, t.id, "521550080", "granted", "1.0")
        await s.flush()
        cal = MemoryCalendarAdapter(s, t.id)
        assert (await cal.book(str(a.id), DATE, "10:00", "other"))["ok"] is True
        fake = _FakeSender()
        res = await cal.cancel(str(a.id), DATE, "10:00",
                               waitlist_sender=fake)
        assert res["ok"] is True
        assert res["freed_slot"]["service_type_slug"] is None
        assert "waitlist" not in res
        assert fake.sent == []


# ── compatibilidad legacy ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_legacy_check_and_book_without_service_type():
    """Sin service_type: el comportamiento legacy no cambia."""
    async with db_mod.async_session_maker() as s:
        t = await _tenant(s, "t7c-legacy")
        await s.flush()
        cal = MemoryCalendarAdapter(s, t.id)
        assert (await cal.check_availability(DATE, "10:00"))["available"] is True
        c = _contact(s, t.id, "521550090", "granted", "1.0")
        await s.flush()
        assert (await cal.book(str(c.id), DATE, "10:00", "other"))["ok"] is True
        assert (await cal.check_availability(DATE, "10:00"))["available"] is False
