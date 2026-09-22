"""Tests de Fase 7d (CRM/panel): recursos, configuración instalada y
consentimientos visibles.

Requieren Postgres + pgvector. El engine de test se configura en conftest.
"""
import os
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from app.agent.embedder import FakeEmbedder
from app.api.onboarding import onboard_tenant
from app.core import db as db_mod
from app.core.auth import create_access_token
from app.core.base import Base
from app.main import app
from app.models import (
    Appointment,
    AppointmentResource,
    Contact,
    PlatformUser,
    Resource,
    Tenant,
    TenantConfig,
)
from app.models.platform_users import (
    ROLE_PLATFORM_ADMIN,
    ROLE_TENANT_ADMIN,
    ROLE_TENANT_AGENT,
    hash_password,
)

API = "http://t"
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


# ── Helpers ───────────────────────────────────────────────────────────────


async def _mk_tenant(session, slug: str) -> Tenant:
    t = Tenant(slug=slug, name=f"Tenant {slug}", business_type="consultorio")
    session.add(t)
    await session.flush()
    session.add(TenantConfig(tenant_id=t.id, system_prompt="Eres Liah."))
    await session.flush()
    return t


async def _mk_user(session, email: str, role: str, password: str = PW,
                   tenant_id=None) -> PlatformUser:
    u = PlatformUser(
        email=email,
        password_hash=hash_password(password),
        role=role,
        tenant_id=tenant_id,
    )
    session.add(u)
    await session.flush()
    return u


async def _login(c, email: str, password: str = PW) -> dict:
    r = await c.post(
        "/api/v1/admin/auth/login", json={"email": email, "password": password}
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=API
    )


async def _seed_panel():
    """Un tenant con tenant_admin, tenant_agent y platform_admin."""
    async with db_mod.async_session_maker() as s:
        t = await _mk_tenant(s, "res-t")
        tid = t.id
        await _mk_user(s, "res-admin@liah.local", ROLE_TENANT_ADMIN,
                       tenant_id=tid)
        await _mk_user(s, "res-agent@liah.local", ROLE_TENANT_AGENT,
                       tenant_id=tid)
        await _mk_user(s, "res-super@liah.local", ROLE_PLATFORM_ADMIN)
        await s.commit()
        return tid


def _res_url(tid):
    return f"/api/v1/admin/tenants/{tid}/resources"


# ── CRUD de recursos ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resources_crud_full_cycle():
    tid = await _seed_panel()
    async with _client() as c:
        h = await _login(c, "res-admin@liah.local")

        # crear
        r = await c.post(
            _res_url(tid), headers=h,
            json={"slug": "consultorio-1", "nombre": "Consultorio 1",
                  "tipo": "room", "movilidad": "fixed", "capacidad": 1},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["slug"] == "consultorio-1"
        assert body["tipo"] == "room"
        assert body["tenant_id"] == str(tid)
        rid = body["id"]

        # listar
        r = await c.get(_res_url(tid), headers=h)
        assert r.status_code == 200
        assert [x["slug"] for x in r.json()] == ["consultorio-1"]

        # editar (parcial)
        r = await c.put(
            f"{_res_url(tid)}/{rid}", headers=h,
            json={"nombre": "Consultorio Principal",
                  "movilidad": "mobile", "capacidad": 2,
                  "especialidad": "medicina-general"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["nombre"] == "Consultorio Principal"
        assert r.json()["movilidad"] == "mobile"
        assert r.json()["especialidad"] == "medicina-general"

        # eliminar sin citas -> ok
        r = await c.delete(f"{_res_url(tid)}/{rid}", headers=h)
        assert r.status_code == 200
        assert r.json()["status"] == "deleted"
        r = await c.get(_res_url(tid), headers=h)
        assert r.json() == []


@pytest.mark.asyncio
async def test_resources_validation():
    tid = await _seed_panel()
    async with _client() as c:
        h = await _login(c, "res-admin@liah.local")

        # tipo inválido -> 422
        r = await c.post(
            _res_url(tid), headers=h,
            json={"slug": "x-1", "nombre": "X", "tipo": "nave-espacial"},
        )
        assert r.status_code == 422

        # capacidad < 1 -> 422
        r = await c.post(
            _res_url(tid), headers=h,
            json={"slug": "x-2", "nombre": "X", "tipo": "room",
                  "capacidad": 0},
        )
        assert r.status_code == 422

        # slug malformado -> 422
        r = await c.post(
            _res_url(tid), headers=h,
            json={"slug": "MAL FORMADO!", "nombre": "X", "tipo": "room"},
        )
        assert r.status_code == 422

        # movilidad inválida -> 422
        r = await c.post(
            _res_url(tid), headers=h,
            json={"slug": "x-3", "nombre": "X", "tipo": "room",
                  "movilidad": "teleport"},
        )
        assert r.status_code == 422

        # slug duplicado en el mismo tenant -> 409
        r = await c.post(
            _res_url(tid), headers=h,
            json={"slug": "dup-1", "nombre": "Uno", "tipo": "room"},
        )
        assert r.status_code == 201
        r = await c.post(
            _res_url(tid), headers=h,
            json={"slug": "dup-1", "nombre": "Dos", "tipo": "staff"},
        )
        assert r.status_code == 409
        assert "dup-1" in r.json()["detail"]

        # PUT a un slug que ya existe -> 409
        r2 = await c.post(
            _res_url(tid), headers=h,
            json={"slug": "otro-1", "nombre": "Otro", "tipo": "room"},
        )
        rid_otro = r2.json()["id"]
        r = await c.put(
            f"{_res_url(tid)}/{rid_otro}", headers=h,
            json={"slug": "dup-1"},
        )
        assert r.status_code == 409

        # recurso inexistente -> 404
        r = await c.delete(
            f"{_res_url(tid)}/{uuid.uuid4()}", headers=h
        )
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_resources_roles_and_scope():
    async with db_mod.async_session_maker() as s:
        ta = await _mk_tenant(s, "res-a")
        tb = await _mk_tenant(s, "res-b")
        await _mk_user(s, "a-admin@liah.local", ROLE_TENANT_ADMIN,
                       tenant_id=ta.id)
        await _mk_user(s, "a-agent@liah.local", ROLE_TENANT_AGENT,
                       tenant_id=ta.id)
        await _mk_user(s, "super@liah.local", ROLE_PLATFORM_ADMIN)
        await s.commit()
        ta_id, tb_id = ta.id, tb.id
    async with _client() as c:
        h_admin_a = await _login(c, "a-admin@liah.local")
        h_agent_a = await _login(c, "a-agent@liah.local")
        h_super = await _login(c, "super@liah.local")

        # tenant_agent NO gestiona recursos -> 403
        r = await c.post(
            _res_url(ta_id), headers=h_agent_a,
            json={"slug": "s-1", "nombre": "S", "tipo": "room"},
        )
        assert r.status_code == 403

        # tenant_admin no toca otro tenant -> 403
        r = await c.post(
            _res_url(tb_id), headers=h_admin_a,
            json={"slug": "s-1", "nombre": "S", "tipo": "room"},
        )
        assert r.status_code == 403
        r = await c.get(_res_url(tb_id), headers=h_admin_a)
        assert r.status_code == 403

        # platform_admin sí, en ambos tenants (multi-tenant con scope)
        for tid in (ta_id, tb_id):
            r = await c.post(
                _res_url(tid), headers=h_super,
                json={"slug": "s-1", "nombre": "S", "tipo": "room"},
            )
            assert r.status_code == 201, r.text
        # el slug se repite entre tenants: la unicidad es POR tenant
        r = await c.get(_res_url(tb_id), headers=h_super)
        assert [x["slug"] for x in r.json()] == ["s-1"]


# ── Borrado bloqueado con citas futuras ───────────────────────────────────


async def _seed_resource_with_appointments(session, tid):
    """Recurso + 3 citas enlazadas: futura confirmada, futura cancelada,
    pasada confirmada."""
    res = Resource(tenant_id=tid, slug="barra-1", nombre="Barra 1",
                   tipo="equipment", movilidad="mobile", capacidad=1)
    session.add(res)
    await session.flush()
    contact = Contact(tenant_id=tid, wa_id="521900000001", name="Fiesta")
    session.add(contact)
    await session.flush()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    specs = [
        (now + timedelta(days=2), "confirmed"),
        (now + timedelta(days=5), "cancelled"),
        (now - timedelta(days=2), "confirmed"),
    ]
    appts = []
    for start, status in specs:
        a = Appointment(
            tenant_id=tid, contact_id=contact.id, type="other",
            start_at=start, end_at=start + timedelta(hours=2),
            status=status,
        )
        session.add(a)
        await session.flush()
        session.add(AppointmentResource(
            tenant_id=tid, appointment_id=a.id, resource_id=res.id
        ))
        appts.append(a)
    await session.flush()
    return res.id, [a.id for a in appts]


@pytest.mark.asyncio
async def test_resource_delete_blocked_with_future_appointments():
    async with db_mod.async_session_maker() as s:
        t = await _mk_tenant(s, "res-blk")
        tid = t.id
        rid, _ = await _seed_resource_with_appointments(s, tid)
        await _mk_user(s, "blk-admin@liah.local", ROLE_TENANT_ADMIN,
                       tenant_id=tid)
        await s.commit()
    async with _client() as c:
        h = await _login(c, "blk-admin@liah.local")

        # hay 1 cita futura no cancelada (la cancelada y la pasada no cuentan)
        r = await c.delete(f"{_res_url(tid)}/{rid}", headers=h)
        assert r.status_code == 409, r.text
        assert "futura" in r.json()["detail"]
        # el recurso sigue existiendo: no hubo borrado en cascada silencioso
        r = await c.get(_res_url(tid), headers=h)
        assert [x["id"] for x in r.json()] == [str(rid)]

        # tras cancelar la cita futura, el borrado procede
        async with db_mod.async_session_maker() as s:
            rows = (
                await s.execute(
                    select(Appointment).where(
                        Appointment.tenant_id == tid,
                        Appointment.status == "confirmed",
                        Appointment.start_at
                        >= datetime.now(timezone.utc).replace(tzinfo=None),
                    )
                )
            ).scalars().all()
            assert len(rows) == 1
            rows[0].status = "cancelled"
            await s.commit()
        r = await c.delete(f"{_res_url(tid)}/{rid}", headers=h)
        assert r.status_code == 200
        r = await c.get(_res_url(tid), headers=h)
        assert r.json() == []


# ── Configuración instalada ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_installed_config_reflects_onboarding():
    async with db_mod.async_session_maker() as s:
        await onboard_tenant(
            s,
            template_name="consultorio_medico",
            slug="clinica-7d",
            nombre=None,
            overrides=None,
            admin_email="clinica7d@test.mx",
            admin_password="Larga-Secret-12345",
            embedder=FakeEmbedder(),
        )
    async with db_mod.async_session_maker() as s:
        t = (
            await s.execute(select(Tenant).where(Tenant.slug == "clinica-7d"))
        ).scalar_one()
        tid = t.id
        await _mk_user(s, "cfg7d-super@liah.local", ROLE_PLATFORM_ADMIN)
        await s.commit()
    async with _client() as c:
        h = await _login(c, "cfg7d-super@liah.local")
        r = await c.get(
            f"/api/v1/admin/tenants/{tid}/installed-config", headers=h
        )
        assert r.status_code == 200, r.text
        cfg = r.json()

        # origen del levantamiento: plantilla + versión del esquema
        assert cfg["installed_from"]["template"] == "consultorio_medico"
        assert cfg["installed_from"]["template_schema_version"] == "1.1"
        assert cfg["installed_from"]["giro"] == "consultorio_medico"
        assert cfg["tenant"]["slug"] == "clinica-7d"

        # recursos instalados del perfil
        slugs = {x["slug"] for x in cfg["resources"]}
        assert "consultorio-1" in slugs
        assert "consultorio-2" in slugs
        c1 = next(x for x in cfg["resources"] if x["slug"] == "consultorio-1")
        assert c1["tipo"] == "room"
        assert c1["movilidad"] == "fixed"

        # tipos de servicio con duración, buffers y traslado
        sts = {x["slug"]: x for x in cfg["service_types"]}
        assert "consulta-medicina-general" in sts
        st = sts["consulta-medicina-general"]
        assert st["duracion_min"] == 30
        assert st["buffers"]["setup"] == 0
        assert st["buffers"]["teardown"] == 0
        assert "modo" in st["traslado"]
        assert any(
            req.get("recurso") == "medico-general"
            for req in st["recursos_requeridos"]
        )

        # aviso de privacidad vigente: versión + título
        assert cfg["privacy_terms"]["version"] == "1.0"
        assert "privacidad" in cfg["privacy_terms"]["titulo"].lower()

        # horarios del perfil
        assert cfg["business_hours"]


@pytest.mark.asyncio
async def test_installed_config_roles():
    tid = await _seed_panel()
    async with _client() as c:
        h_agent = await _login(c, "res-agent@liah.local")
        h_super = await _login(c, "res-super@liah.local")
        # tenant_agent no entra (solo platform/tenant_admin)
        r = await c.get(
            f"/api/v1/admin/tenants/{tid}/installed-config", headers=h_agent
        )
        assert r.status_code == 403
        r = await c.get(
            f"/api/v1/admin/tenants/{tid}/installed-config", headers=h_super
        )
        assert r.status_code == 200
        assert r.json()["tenant"]["slug"] == "res-t"


# ── Consentimientos visibles + segmentación ───────────────────────────────


@pytest.mark.asyncio
async def test_contacts_consent_visible_and_segment_filter():
    async with db_mod.async_session_maker() as s:
        t = await _mk_tenant(s, "cons-t")
        tid = t.id
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        client = Contact(
            tenant_id=tid, wa_id="521300000001", name="Claudia",
            contact_type="client", consent_status="granted",
            consent_at=now - timedelta(days=3),
            privacy_terms_version="1.0",
            marketing_opt_in=True,
            marketing_opt_in_source="keyword",
        )
        prospect = Contact(
            tenant_id=tid, wa_id="521300000002", name="Paco",
            contact_type="prospect", consent_status="none",
            privacy_terms_version=None, marketing_opt_in=False,
        )
        s.add_all([client, prospect])
        await _mk_user(s, "cons-admin@liah.local", ROLE_TENANT_ADMIN,
                       tenant_id=tid)
        await s.commit()
    async with _client() as c:
        h = await _login(c, "cons-admin@liah.local")
        url = f"/api/v1/admin/tenants/{tid}/contacts"

        r = await c.get(url, headers=h)
        assert r.status_code == 200, r.text
        rows = {x["wa_id"]: x for x in r.json()}
        cl = rows["521300000001"]
        assert cl["consent_status"] == "granted"
        assert cl["consent_at"] is not None
        assert cl["privacy_terms_version"] == "1.0"
        assert cl["contact_type"] == "client"
        pr = rows["521300000002"]
        assert pr["consent_status"] == "none"
        assert pr["consent_at"] is None
        assert pr["privacy_terms_version"] is None

        # filtro de segmentación curioso/cliente
        r = await c.get(url, headers=h, params={"contact_type": "client"})
        assert [x["wa_id"] for x in r.json()] == ["521300000001"]
        r = await c.get(url, headers=h, params={"contact_type": "prospect"})
        assert [x["wa_id"] for x in r.json()] == ["521300000002"]

        # la página del panel acepta el filtro y muestra los consentimientos
        r = await c.get(url, headers=h, params={"contact_type": "invalido"})
        assert r.status_code == 422


# ── UI server-rendered ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_ui_recursos_and_configuracion_pages():
    tid = await _seed_panel()
    async with _client() as c:
        # sin sesión -> redirect al login
        for page in ("/admin/recursos", "/admin/configuracion"):
            r = await c.get(page, follow_redirects=False)
            assert r.status_code == 303, page

        # login por formulario fija la cookie httpOnly
        r = await c.post(
            "/admin/login",
            data={"email": "res-super@liah.local", "password": PW},
            follow_redirects=False,
        )
        assert r.status_code == 303

        # las páginas cargan con la cookie
        r = await c.get(f"/admin/recursos?tenant_id={tid}")
        assert r.status_code == 200
        assert "Recursos reservables" in r.text
        assert "Sin recursos" in r.text  # aún sin recursos

        r = await c.get(f"/admin/configuracion?tenant_id={tid}")
        assert r.status_code == 200
        assert "Configuración instalada" in r.text
        assert "una sola fuente" in r.text
        assert "res-t" in r.text

        # crear por API y verlo reflejado en la página
        h = await _login(c, "res-super@liah.local")
        r = await c.post(
            _res_url(tid), headers=h,
            json={"slug": "espejo-1", "nombre": "Espejo mágico",
                  "tipo": "equipment", "movilidad": "mobile",
                  "capacidad": 1},
        )
        assert r.status_code == 201
        r = await c.get(f"/admin/recursos?tenant_id={tid}")
        assert "espejo-1" in r.text
        assert "móvil" in r.text


@pytest.mark.asyncio
async def test_admin_ui_contacts_shows_consent_and_type_filter():
    async with db_mod.async_session_maker() as s:
        t = await _mk_tenant(s, "cons-ui")
        tid = t.id
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        s.add(Contact(
            tenant_id=tid, wa_id="521400000001", name="Curiosa Carla",
            contact_type="prospect", consent_status="pending",
            consent_at=now - timedelta(hours=2),
            privacy_terms_version="1.0",
        ))
        await _mk_user(s, "consui-super@liah.local", ROLE_PLATFORM_ADMIN)
        await s.commit()
    async with _client() as c:
        r = await c.post(
            "/admin/login",
            data={"email": "consui-super@liah.local", "password": PW},
            follow_redirects=False,
        )
        assert r.status_code == 303

        r = await c.get(
            f"/admin/contacts?tenant_id={tid}&contact_type=prospect"
        )
        assert r.status_code == 200
        assert "Curiosa Carla" in r.text
        assert "pendiente" in r.text
        assert "v1.0" in r.text
        # el filtro de tipo prospect está seleccionado en la página
        assert 'value="prospect" selected' in r.text
