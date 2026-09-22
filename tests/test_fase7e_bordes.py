"""Tests de borde Fase 7e: verificación integral de la interacción entre fases.

Solo casos NO cubiertos por test_fase7c_disponibilidad / test_fase7d_panel /
test_fase7f_staff (matcheo de zonas con acentos, waitlist al primero que
califica con dos candidatos y capacidad 0 en el panel ya tienen cobertura).

Contra BD de test (postgres 127.0.0.1:5433; ver conftest). Cubre:
- venue None en negocio móvil: ambos None -> sin costo de traslado; solo uno
  None -> se aplica el default de la política per_zone (el traslado va contra
  un vecino real y contra "ningún lugar" declarado)
- doble consentimiento: re-aceptar la versión vigente es idempotente (no
  vuelve a pending, no cambia la versión, no rompe el gating)
- waitlist con dos candidatos: la primera entrada NO califica (traslape de
  su propia agenda) -> la oferta va a la segunda; la primera sigue waiting
- staff specialist sin resource_id: agenda, "quién a la hora" y huecos se
  degradan con mensaje claro, sin crash
- staff specialist con resource_id huérfano (recurso borrado): no se
  sobre-permitea (ve todo) ni crashea; se degrada igual que sin asignar
"""
import os
from datetime import datetime

import pytest_asyncio

from app.agent import availability as availmod
from app.agent.calendar import MemoryCalendarAdapter
from app.agent.consent import apply_privacy_gate, privacy_gate_error
from app.agent.staff import handle_staff_message
from app.agent.waitlist import join_waitlist
from app.core import db as db_mod
from app.core.base import Base
from app.models import (
    Contact,
    Resource,
    ServiceType,
    StaffMember,
    Tenant,
    TenantPrivacyTerms,
)

DATE = "2026-10-06"  # martes fijo para estos tests


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


def _mobile_service(s, tenant_id):
    r = Resource(
        tenant_id=tenant_id, slug="espejo-1", nombre="espejo-1",
        tipo="equipment", movilidad="mobile", capacidad=1,
    )
    s.add(r)
    st = ServiceType(
        tenant_id=tenant_id, slug="servicio-movil", nombre="servicio-movil",
        duracion_min=60,
        recursos_requeridos=[{"recurso": "espejo-1"}],
        buffers={"setup": 15, "teardown": 15},
        traslado={
            "modo": "per_zone",
            "default_min": 60,
            "zonas": {"xalapa": 0, "veracruz": 45},
        },
    )
    s.add(st)
    return st


def _contact(s, tenant_id, wa_id, consent_status="none",
             privacy_terms_version=None):
    c = Contact(
        tenant_id=tenant_id, wa_id=wa_id, consent_status=consent_status,
        privacy_terms_version=privacy_terms_version,
    )
    s.add(c)
    return c


def _terms(s, tenant_id, version="1.0"):
    pt = TenantPrivacyTerms(
        tenant_id=tenant_id, version=version, titulo="Aviso de privacidad",
        texto="Aviso de prueba: tus datos se usan solo para agendar.",
    )
    s.add(pt)
    return pt


class _FakeSender:
    def __init__(self):
        self.sent = []

    async def send_text(self, tenant_id, contact_id, channel_contact_id,
                        text, **kwargs):
        self.sent.append({"channel_contact_id": channel_contact_id,
                          "text": text})
        return {"status": "ok"}


# ── 1. venue None en negocio móvil ──────────────────────────────────────

@pytest_asyncio.fixture()
async def none_venue_setup():
    async with db_mod.async_session_maker() as s:
        t = await _tenant(s, "t7e-nonevenue")
        _mobile_service(s, t.id)
        c = _contact(s, t.id, "521550090", "granted", "1.0")
        await s.flush()
        cal = MemoryCalendarAdapter(s, t.id)
        # Evento 1: 10:00 sin venue declarada. Ocupado [09:45, 11:15].
        assert (await cal.book(str(c.id), DATE, "10:00", "other",
                               service_type_slug="servicio-movil",
                               venue=None))["ok"] is True
        await s.commit()
        yield t


async def test_mobile_venue_none_vs_none_no_travel(none_venue_setup):
    """Ambos venues None: no se cobra traslado (misma sede)."""
    async with db_mod.async_session_maker() as s:
        check = await availmod.check_resource_availability(
            s, none_venue_setup.id, "servicio-movil", DATE, "12:00",
            venue=None,
        )
        assert check["available"] is True, check["conflicts"]


async def test_mobile_venue_none_vs_declared_uses_default(none_venue_setup):
    """Vecino sin venue + candidato con venue: se aplica la política per_zone.

    Gap 11:15 -> 11:45 (setup de 15 min sobre las 12:00) = 30 min < 45
    (la zona "Veracruz" está listada con 45 min): conflicto de tipo
    'travel', no de capacidad.
    """
    async with db_mod.async_session_maker() as s:
        check = await availmod.check_resource_availability(
            s, none_venue_setup.id, "servicio-movil", DATE, "12:00",
            venue="Veracruz",
        )
        assert check["available"] is False
        travel = [c for c in check["conflicts"] if c["type"] == "travel"]
        assert len(travel) == 1, check["conflicts"]
        assert "45 min" in travel[0]["detail"]
        # Capacidad no está en conflicto: el problema es solo el traslado.
        assert not any(c["type"] == "resource_capacity"
                       for c in check["conflicts"])


async def test_mobile_venue_none_feasible_gap_accepted(none_venue_setup):
    """Con hueco suficiente el traslado contra venue None sí cabe."""
    async with db_mod.async_session_maker() as s:
        check = await availmod.check_resource_availability(
            s, none_venue_setup.id, "servicio-movil", DATE, "14:00",
            venue="Veracruz",
        )
        assert check["available"] is True, check["conflicts"]


# ── 2. doble consentimiento ─────────────────────────────────────────────

async def test_double_consent_idempotent():
    """Re-aceptar la versión vigente no regresa a pending ni rompe nada."""
    async with db_mod.async_session_maker() as s:
        t = await _tenant(s, "t7e-dobleconsent")
        _terms(s, t.id, version="1.0")
        c = _contact(s, t.id, "521550091", "granted", "1.0")
        await s.flush()

        res = await apply_privacy_gate(s, t.id, c, "sí, acepto")
        assert res["handled"] is False
        assert res["transition"] is None
        assert c.consent_status == "granted"
        assert c.privacy_terms_version == "1.0"

        assert await privacy_gate_error(s, t.id, c) is None


async def test_consent_reaccept_does_not_revoke():
    """Un contacto granted que dice 'no' a NADA en particular sigue granted.

    (La puerta solo actúa si los términos cambian o hay negativa clara.)
    """
    async with db_mod.async_session_maker() as s:
        t = await _tenant(s, "t7e-consent2")
        _terms(s, t.id, version="1.0")
        c = _contact(s, t.id, "521550092", "granted", "1.0")
        await s.flush()

        res = await apply_privacy_gate(s, t.id, c, "gracias, nos vemos")
        assert res["handled"] is False
        assert c.consent_status == "granted"


# ── 3. waitlist: salta a la que sí califica ───────────────────────────────

async def test_waitlist_skips_non_qualifying_first_entry():
    """Dos candidatos: la 1ra no califica (traslape propio) -> oferta a la 2da."""
    async with db_mod.async_session_maker() as s:
        t = await _tenant(s, "t7e-wait2")
        r = Resource(
            tenant_id=t.id, slug="room-1", nombre="room-1", tipo="room",
            movilidad="fixed", capacidad=1,
        )
        s.add(r)
        s.add(ServiceType(
            tenant_id=t.id, slug="servicio-1", nombre="servicio-1",
            duracion_min=60, recursos_requeridos=[{"recurso": "room-1"}],
            buffers={}, traslado={"modo": "none"},
        ))
        a = _contact(s, t.id, "521550093", "granted", "1.0")
        b = _contact(s, t.id, "521550094", "granted", "1.0")
        c = _contact(s, t.id, "521550095", "granted", "1.0")
        await s.flush()

        cal = MemoryCalendarAdapter(s, t.id)
        # a ocupa el hueco 10:00 con el servicio. b tiene una cita propia
        # (legacy, 10:30) que traslapa el hueco: no califica por traslape
        # de su propia agenda. Se usa 10:30 y no 10:00 porque el índice
        # único (tenant_id, start_at) impide dos citas al mismo instante.
        assert (await cal.book(str(a.id), DATE, "10:00", "other",
                               service_type_slug="servicio-1"))["ok"] is True
        assert (await cal.book(str(b.id), DATE, "10:30", "other"))["ok"] is True
        await s.flush()

        eb = await join_waitlist(s, t.id, b.id, "servicio-1")
        eb.created_at = datetime(2026, 9, 1, 10, 0)   # primera en la fila
        ec = await join_waitlist(s, t.id, c.id, "servicio-1")
        ec.created_at = datetime(2026, 9, 1, 11, 0)
        await s.flush()

        fake = _FakeSender()
        res = await cal.cancel(str(a.id), DATE, "10:00",
                               waitlist_sender=fake)
        assert res["ok"] is True
        wl = res["waitlist"]
        assert wl["offered"] is True
        assert wl["contact_id"] == str(c.id), wl  # salta a la que califica

        await s.refresh(eb)
        await s.refresh(ec)
        assert eb.status == "waiting"   # la primera no calificó: sigue ahí
        assert ec.status == "offered"
        assert len(fake.sent) == 1      # solo un aviso, al segundo
        assert fake.sent[0]["channel_contact_id"] == "521550095"


# ── 4. specialist sin recurso: degradación sin crash ──────────────────────

@pytest_asyncio.fixture()
async def unassigned_specialist():
    async with db_mod.async_session_maker() as s:
        t = await _tenant(s, "t7e-staffsin")
        r = Resource(
            tenant_id=t.id, slug="sala-1", nombre="sala-1", tipo="room",
            movilidad="fixed", capacidad=1,
        )
        s.add(r)
        s.add(ServiceType(
            tenant_id=t.id, slug="svc-1", nombre="svc-1", duracion_min=60,
            recursos_requeridos=[{"recurso": "sala-1"}],
            buffers={}, traslado={"modo": "none"},
        ))
        m = StaffMember(tenant_id=t.id, wa_id="521550096", nombre="Sin Recurso",
                        role="specialist", resource_id=None)
        s.add(m)
        await s.flush()
        yield t, m


async def test_specialist_without_resource_agenda_no_crash(unassigned_specialist):
    t, m = unassigned_specialist
    async with db_mod.async_session_maker() as s:
        reply = await handle_staff_message(s, t.id, m, "mi agenda de hoy")
        assert isinstance(reply, str)
        assert "No hay citas confirmadas" in reply


async def test_specialist_without_resource_who_at_no_crash(unassigned_specialist):
    t, m = unassigned_specialist
    async with db_mod.async_session_maker() as s:
        reply = await handle_staff_message(
            s, t.id, m, "¿quién es el de las 10:30?")
        assert reply == "No hay cita hoy a las 10:30."


async def test_specialist_without_resource_free_slots_clear_message(
    unassigned_specialist,
):
    t, m = unassigned_specialist
    async with db_mod.async_session_maker() as s:
        reply = await handle_staff_message(
            s, t.id, m, "¿qué huecos hay mañana?")
        assert isinstance(reply, str)
        assert "no tiene un recurso asignado" in reply


async def test_specialist_resource_deleted_degrades_gracefully():
    """Borrar el recurso del specialist -> resource_id queda NULL
    (el FK es ondelete=SET NULL) y el modo staff se degrada con mensaje
    claro: sin crash y sin sobre-permitir (ver todo).
    """
    from sqlalchemy import select

    async with db_mod.async_session_maker() as s:
        t = await _tenant(s, "t7e-staffdel")
        r = Resource(
            tenant_id=t.id, slug="sala-1", nombre="sala-1", tipo="room",
            movilidad="fixed", capacidad=1,
        )
        s.add(r)
        s.add(ServiceType(
            tenant_id=t.id, slug="svc-1", nombre="svc-1", duracion_min=60,
            recursos_requeridos=[{"recurso": "sala-1"}],
            buffers={}, traslado={"modo": "none"},
        ))
        await s.flush()
        s.add(StaffMember(tenant_id=t.id, wa_id="521550098",
                          nombre="Ex Sala", role="specialist",
                          resource_id=r.id))
        await s.flush()
        # El operador borra el recurso (sin citas futuras): SET NULL.
        await s.delete(r)
        await s.commit()
        tid = t.id
    async with db_mod.async_session_maker() as s:
        m = (
            await s.execute(
                select(StaffMember).where(
                    StaffMember.tenant_id == tid,
                    StaffMember.wa_id == "521550098",
                )
            )
        ).scalar_one()
        assert m.resource_id is None
        reply = await handle_staff_message(
            s, tid, m, "¿qué huecos hay mañana?")
        assert isinstance(reply, str)
        assert "no tiene un recurso asignado" in reply, reply
        reply2 = await handle_staff_message(s, tid, m, "mi agenda de hoy")
        assert isinstance(reply2, str)
