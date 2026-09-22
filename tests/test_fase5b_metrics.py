"""Tests de Fase 5b: métricas con lente "ganar clientes" del panel.

Números exactos con datos sembrados para cada métrica agregada en fase 5b:
- `appointments_scheduled`: citas creadas en la ventana, sin canceladas.
- `leads_captured`: contactos nuevos creados en la ventana.
- `after_hours`: inbound fuera de horario que recibió respuesta
  (None si el tenant no tiene horarios configurados).

Requieren Postgres + pgvector. El engine de test se configura en conftest.
"""
import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio

from app.core import db as db_mod
from app.core.base import Base
from app.main import app
from app.models import (
    Appointment,
    Contact,
    Message,
    PlatformUser,
    Tenant,
    TenantConfig,
)
from app.models.platform_users import ROLE_TENANT_ADMIN, hash_password

API = "http://t"


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


async def _mk_tenant(session, slug: str, business_hours: dict | None = None,
                     tz: str = "America/Mexico_City") -> Tenant:
    t = Tenant(slug=slug, name=f"Tenant {slug}", business_type="consultorio",
               timezone=tz)
    session.add(t)
    await session.flush()
    cfg = TenantConfig(tenant_id=t.id, system_prompt="Eres Liah.")
    if business_hours is not None:
        cfg.business_hours = business_hours
    session.add(cfg)
    await session.flush()
    return t


async def _mk_user(session, email: str, tenant_id) -> PlatformUser:
    u = PlatformUser(
        email=email,
        password_hash=hash_password("Secret-12345678"),
        role=ROLE_TENANT_ADMIN,
        tenant_id=tenant_id,
    )
    session.add(u)
    await session.flush()
    return u


def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


async def _login(c, email: str) -> dict:
    r = await c.post("/api/v1/admin/auth/login",
                     json={"email": email, "password": "Secret-12345678"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _recent_sunday_16utc(base: datetime) -> datetime:
    """Domingo más reciente (pasado) a las 16:00 UTC."""
    days_back = (base.weekday() + 1) % 7  # weekday(): Mon=0..Sun=6
    cand = (base - timedelta(days=days_back)).replace(
        hour=16, minute=0, second=0, microsecond=0)
    if cand >= base:
        cand -= timedelta(days=7)
    return cand


@pytest.mark.asyncio
async def test_metrics_appointments_and_leads():
    async with db_mod.async_session_maker() as s:
        t = await _mk_tenant(s, "m5b-t")
        tid = t.id
        base = datetime.now(timezone.utc).replace(tzinfo=None)
        in_win = base - timedelta(days=3)
        old = base - timedelta(days=60)

        ca = Contact(tenant_id=tid, wa_id="521300000001", name="Ana",
                     created_at=in_win)
        cb = Contact(tenant_id=tid, wa_id="521300000002", name="Beto",
                     created_at=in_win)
        cc = Contact(tenant_id=tid, wa_id="521300000003", name="Viejo",
                     created_at=old)
        s.add_all([ca, cb, cc])
        await s.flush()

        # 2 citas agendadas en ventana (1 cancelada no cuenta), 1 fuera de ventana.
        s.add_all([
            Appointment(tenant_id=tid, contact_id=ca.id, type="consultation",
                        start_at=in_win + timedelta(days=1), status="confirmed",
                        created_at=in_win),
            Appointment(tenant_id=tid, contact_id=cb.id, type="other",
                        start_at=in_win + timedelta(days=2), status="confirmed",
                        created_at=in_win),
            Appointment(tenant_id=tid, contact_id=ca.id, type="consultation",
                        start_at=in_win + timedelta(days=3), status="cancelled",
                        created_at=in_win),
            Appointment(tenant_id=tid, contact_id=ca.id, type="consultation",
                        start_at=old + timedelta(days=1), status="confirmed",
                        created_at=old),
        ])
        await _mk_user(s, "m5b-admin@liah.local", tid)
        await s.commit()

    async with _client() as c:
        h = await _login(c, "m5b-admin@liah.local")
        r = await c.get(f"/api/v1/admin/tenants/{tid}/metrics",
                        headers=h, params={"days": 30})
        assert r.status_code == 200, r.text
        m = r.json()
        assert m["appointments_scheduled"] == 2
        assert m["leads_captured"] == 2
        # sin horarios configurados -> None (no se inventa un número)
        assert m["after_hours"] is None


@pytest.mark.asyncio
async def test_metrics_after_hours_attended():
    hours = {
        "monday": {"open": "09:00", "close": "19:00"},
        "tuesday": {"open": "09:00", "close": "19:00"},
        "wednesday": {"open": "09:00", "close": "19:00"},
        "thursday": {"open": "09:00", "close": "19:00"},
        "friday": {"open": "09:00", "close": "19:00"},
        "saturday": {"open": "09:00", "close": "14:00"},
        "sunday": "closed",
    }
    async with db_mod.async_session_maker() as s:
        t = await _mk_tenant(s, "m5b-ah", business_hours=hours)
        tid = t.id
        base = datetime.now(timezone.utc).replace(tzinfo=None)
        sunday_16utc = _recent_sunday_16utc(base)  # domingo local 10:00 (cerrado)
        monday_16utc = sunday_16utc + timedelta(days=1)   # lunes local 10:00 (abierto)
        monday_20local = sunday_16utc + timedelta(days=1, hours=10)  # lunes local 20:00 (cerrado)

        ca = Contact(tenant_id=tid, wa_id="521300000101")
        cb = Contact(tenant_id=tid, wa_id="521300000102")
        cc = Contact(tenant_id=tid, wa_id="521300000103")
        s.add_all([ca, cb, cc])
        await s.flush()

        s.add_all([
            # domingo (cerrado) con respuesta -> fuera de horario ATENDIDO
            Message(tenant_id=tid, contact_id=ca.id, direction="inbound",
                    content="hola", created_at=sunday_16utc),
            Message(tenant_id=tid, contact_id=ca.id, direction="outbound",
                    content="hola", created_at=sunday_16utc + timedelta(minutes=1)),
            # lunes 20:00 local (después del cierre 19:00) sin respuesta
            Message(tenant_id=tid, contact_id=cb.id, direction="inbound",
                    content="precios?", created_at=monday_20local),
            # lunes 10:00 local (dentro de horario): no entra al cómputo
            Message(tenant_id=tid, contact_id=cc.id, direction="inbound",
                    content="hola", created_at=monday_16utc),
            Message(tenant_id=tid, contact_id=cc.id, direction="outbound",
                    content="hola", created_at=monday_16utc + timedelta(minutes=1)),
        ])
        await _mk_user(s, "m5b-ah-admin@liah.local", tid)
        await s.commit()

    async with _client() as c:
        h = await _login(c, "m5b-ah-admin@liah.local")
        r = await c.get(f"/api/v1/admin/tenants/{tid}/metrics",
                        headers=h, params={"days": 30})
        assert r.status_code == 200, r.text
        m = r.json()
        ah = m["after_hours"]
        assert ah["outside_hours"] == 2
        assert ah["attended"] == 1
        assert ah["attended_pct"] == 50.0


@pytest.mark.asyncio
async def test_metrics_ui_renders_new_kpis():
    """La página /admin/metrics muestra la sección de valor de negocio."""
    async with db_mod.async_session_maker() as s:
        t = await _mk_tenant(s, "m5b-ui")
        tid = t.id
        base = datetime.now(timezone.utc).replace(tzinfo=None)
        c1 = Contact(tenant_id=tid, wa_id="521300000201",
                     created_at=base - timedelta(days=1))
        s.add(c1)
        await s.flush()
        s.add(Appointment(tenant_id=tid, contact_id=c1.id, type="other",
                          start_at=base + timedelta(days=1), status="confirmed",
                          created_at=base - timedelta(days=1)))
        await _mk_user(s, "m5b-ui-admin@liah.local", tid)
        await s.commit()
    async with _client() as c:
        # login por formulario (fija la cookie httpOnly de la UI)
        r = await c.post("/admin/login", data={
            "email": "m5b-ui-admin@liah.local", "password": "Secret-12345678"},
            follow_redirects=False)
        assert r.status_code == 303, r.text
        r = await c.get("/admin/metrics", params={"tenant_id": str(tid)})
        assert r.status_code == 200, r.text
        html = r.text
        assert "lo que el asistente generó por ti" in html.lower()
        for label in ("Citas agendadas", "Leads capturados",
                      "Mensajes fuera de horario atendidos",
                      "1ª respuesta media"):
            assert label in html, label
