"""Tests de Fase 3: onboarding white-label (API Key auth + Embedded Signup).

Requieren Postgres + pgvector. El engine de test se configura en conftest.
"""
import os
import respx
import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from app.core.auth import generate_api_key, hash_api_key
from app.core.base import Base
from app.core import db as db_mod
from app.main import app
from app.models import (
    AutomationRule,
    PlatformUser,
    Template,
    Tenant,
    TenantConfig,
    WhatsappChannel,
)
from app.models.platform_users import ROLE_PLATFORM_ADMIN, hash_password

API = "http://t"


async def _admin_headers(c) -> dict:
    """Crea un platform_admin en BD y devuelve headers con su JWT.

    Idempotente: si el admin ya existe (misma BD de test), lo reutiliza.
    (Fase 3 del panel: POST /tenants y el callback de signup exigen auth.)
    """
    async with db_mod.async_session_maker() as s:
        existing = (
            await s.execute(
                select(PlatformUser).where(
                    PlatformUser.email == "op-panel@liah.local"
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            s.add(
                PlatformUser(
                    email="op-panel@liah.local",
                    password_hash=hash_password("OpPanel-Secret-123"),
                    role=ROLE_PLATFORM_ADMIN,
                )
            )
            await s.commit()
    r = await c.post(
        "/api/v1/admin/auth/login",
        json={"email": "op-panel@liah.local", "password": "OpPanel-Secret-123"},
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


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


@pytest.mark.asyncio
async def test_create_tenant_returns_api_key_once():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=API) as c:
        headers = await _admin_headers(c)
        r = await c.post(
            "/tenants",
            json={"slug": "barberia-x", "name": "Barberia X", "business_type": "barberia",
                  "system_prompt": "Eres Liah."},
            headers=headers,
        )
    assert r.status_code == 201
    body = r.json()
    assert body["api_key"].startswith("liah_live_sk_")
    # el hash con salt+pepper se guardó, no la key en claro
    async with db_mod.async_session_maker() as s:
        t = (await s.execute(select(Tenant).where(Tenant.slug == "barberia-x"))).scalar_one()
        assert t.api_key_salt is not None
        assert t.api_key_hash == hash_api_key(body["api_key"], t.api_key_salt)
        assert t.api_key_hash != body["api_key"]


@pytest.mark.asyncio
async def test_rotate_api_key():
    """Rotación: la nueva key funciona, la anterior muere de inmediato."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=API) as c:
        headers = await _admin_headers(c)
        created = await c.post(
            "/tenants",
            json={"slug": "rotacion-x", "name": "Rot X", "business_type": "otro"},
            headers=headers,
        )
        assert created.status_code == 201
        old_key = created.json()["api_key"]

        r = await c.post("/tenants/me/api-key/rotate",
                         headers={"X-Tenant-API-Key": old_key})
        assert r.status_code == 200
        new_key = r.json()["api_key"]
        assert new_key != old_key
        assert new_key.startswith("liah_live_sk_")

        # la vieja ya no autentica
        r_old = await c.get("/tenants/me/config",
                            headers={"X-Tenant-API-Key": old_key})
        assert r_old.status_code == 401
        # la nueva sí
        r_new = await c.get("/tenants/me/config",
                            headers={"X-Tenant-API-Key": new_key})
        assert r_new.status_code == 200


@pytest.mark.asyncio
async def test_duplicate_slug_rejected():
    """Dos tenants no pueden compartir slug."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=API) as c:
        headers = await _admin_headers(c)
        r1 = await c.post(
            "/tenants",
            json={"slug": "slug-dup", "name": "Uno", "business_type": "otro"},
            headers=headers,
        )
        assert r1.status_code == 201
        r2 = await c.post(
            "/tenants",
            json={"slug": "slug-dup", "name": "Dos", "business_type": "otro"},
            headers=headers,
        )
        assert r2.status_code == 409


@pytest.mark.asyncio
async def test_protected_endpoints_require_api_key():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=API) as c:
        # sin header -> 401
        r = await c.get("/tenants/me/config")
        assert r.status_code == 401
        # con key falsa -> 401
        r2 = await c.get("/tenants/me/config", headers={"X-Tenant-API-Key": "liah_live_sk_fake"})
        assert r2.status_code == 401


@pytest.mark.asyncio
async def test_protected_config_and_crud_with_valid_key():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=API) as c:
        headers_admin = await _admin_headers(c)
        created = await c.post(
            "/tenants",
            json={"slug": "consultorio-y", "name": "Consultorio Y", "business_type": "consultorio"},
            headers=headers_admin,
        )
        api_key = created.json()["api_key"]
        headers = {"X-Tenant-API-Key": api_key}

        # GET config
        r = await c.get("/tenants/me/config", headers=headers)
        assert r.status_code == 200
        assert "system_prompt" in r.json()

        # PUT config
        r2 = await c.put("/tenants/me/config", headers=headers,
                         json={"tone": "amable", "lfpdp_consent_required": True})
        assert r2.status_code == 200

        # POST rule
        r3 = await c.post("/tenants/me/rules", headers=headers,
                          json={"type": "followup_30d", "enabled": True})
        assert r3.status_code == 201

        # POST template
        r4 = await c.post("/tenants/me/templates", headers=headers,
                          json={"name": "seguimiento_consulta",
                                "body": "Hola {{1}} de {{2}}", "variables": ["name", "tenant"]})
        assert r4.status_code == 201

    # verificar persistencia en BD
    async with db_mod.async_session_maker() as s:
        t = (await s.execute(select(Tenant).where(Tenant.slug == "consultorio-y"))).scalar_one()
        rules = (await s.execute(select(AutomationRule).where(AutomationRule.tenant_id == t.id))).scalars().all()
        tpls = (await s.execute(select(Template).where(Template.tenant_id == t.id))).scalars().all()
        cfg = (await s.execute(select(TenantConfig).where(TenantConfig.tenant_id == t.id))).scalar_one()
        assert len(rules) == 1 and rules[0].type == "followup_30d"
        assert len(tpls) == 1 and tpls[0].name == "seguimiento_consulta"
        assert cfg.lfpdp_consent_required is True


@pytest.mark.asyncio
async def test_embedded_signup_callback_persists_channel():
    # crear tenant primero
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=API) as c:
        headers = await _admin_headers(c)
        created = await c.post(
            "/tenants",
            json={"slug": "academia-z", "name": "Academia Z", "business_type": "academy"},
            headers=headers,
        )
        tid = created.json()["tenant_id"]

    # mock de Graph API (Pasos B/C/D)
    with respx.mock:
        import respx as _r

        _r.get(url__startswith="https://graph.facebook.com/v20.0/oauth/access_token").mock(
            return_value=httpx.Response(200, json={"access_token": "TOKEN_123"})
        )
        _r.get(url__startswith="https://graph.facebook.com/v20.0/100/phone_numbers").mock(
            return_value=httpx.Response(200, json={"data": [
                {"id": "PN_999", "display_phone_number": "5215551234567"}
            ]})
        )
        _r.post(url__startswith="https://graph.facebook.com/v20.0/100/subscribed_apps").mock(
            return_value=httpx.Response(200, json={"success": True})
        )
        async with httpx.AsyncClient(transport=transport, base_url=API) as c:
            headers = await _admin_headers(c)
            r = await c.post(
                "/tenants/channels/whatsapp/embedded-signup/callback",
                json={"tenant_id": tid, "code": "CODE_ABC", "waba_id": "100",
                      "business_id": "200"},
                headers=headers,
            )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "connected"
    assert body["phone_number_id"] == "PN_999"

    async with db_mod.async_session_maker() as s:
        ch = (await s.execute(select(WhatsappChannel).where(WhatsappChannel.waba_id == "100"))).scalar_one()
        assert ch.tenant_id == _uuid(tid)
        assert ch.phone_number == "5215551234567"


def _uuid(s):
    import uuid as _u

    return _u.UUID(s)


@pytest.mark.asyncio
async def test_create_tenant_requires_platform_admin():
    """POST /tenants sin JWT de plataforma -> 401 (Fase 3: ya no es abierto)."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=API) as c:
        r = await c.post(
            "/tenants",
            json={"slug": "sin-auth", "name": "Sin Auth", "business_type": "otro"},
        )
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_signup_callback_rejects_unauthenticated():
    """Callback de Embedded Signup sin auth -> 401 (vector de hijack cerrado)."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=API) as c:
        headers = await _admin_headers(c)
        created = await c.post(
            "/tenants",
            json={"slug": "hijack-x", "name": "Hijack X", "business_type": "otro"},
            headers=headers,
        )
        tid = created.json()["tenant_id"]
    # Cliente fresco: el anterior guarda la cookie httpOnly del login y eso
    # SÍ autentica (correcto); aquí queremos probar la ausencia total de auth.
    async with httpx.AsyncClient(transport=transport, base_url=API) as c2:
        r = await c2.post(
            "/tenants/channels/whatsapp/embedded-signup/callback",
            json={"tenant_id": tid, "code": "CODE_X", "waba_id": "999"},
        )
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_signup_callback_accepts_own_tenant_api_key():
    """El tenant puede enlazar su propio WhatsApp con su API key (self-service)."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=API) as c:
        headers = await _admin_headers(c)
        created = await c.post(
            "/tenants",
            json={"slug": "selfsvc-x", "name": "Self Svc", "business_type": "otro"},
            headers=headers,
        )
        body = created.json()
        # El callback con auth válida llega a la Graph API: la mockeamos
        # (respx no intercepta el ASGITransport de la app bajo test).
        with respx.mock:
            respx.get(url__startswith="https://graph.facebook.com/v20.0/oauth/access_token").mock(
                return_value=httpx.Response(200, json={"access_token": "SELF_SVC_TOKEN"})
            )
            respx.get(url__startswith="https://graph.facebook.com/v20.0/555/phone_numbers").mock(
                return_value=httpx.Response(
                    200,
                    json={"data": [{"id": "PN_SELF", "display_phone_number": "+525500000001"}]},
                )
            )
            respx.post(url__startswith="https://graph.facebook.com/v20.0/PN_SELF/register").mock(
                return_value=httpx.Response(200, json={"success": True})
            )
            respx.post(url__startswith="https://graph.facebook.com/v20.0/555/subscribed_apps").mock(
                return_value=httpx.Response(200, json={"success": True})
            )
            r = await c.post(
                "/tenants/channels/whatsapp/embedded-signup/callback",
                json={"tenant_id": body["tenant_id"], "code": "CODE_Y", "waba_id": "555"},
                headers={"X-Tenant-API-Key": body["api_key"]},
            )
        assert r.status_code == 200
        # La API key de OTRO tenant no sirve para este tenant_id
        # (cliente fresco para no arrastrar la cookie de admin).
        created2 = await c.post(
            "/tenants",
            json={"slug": "otro-x", "name": "Otro X", "business_type": "otro"},
            headers=headers,
        )
    async with httpx.AsyncClient(transport=transport, base_url=API) as c2:
        r2 = await c2.post(
            "/tenants/channels/whatsapp/embedded-signup/callback",
            json={"tenant_id": body["tenant_id"], "code": "CODE_Z", "waba_id": "556"},
            headers={"X-Tenant-API-Key": created2.json()["api_key"]},
        )
        assert r2.status_code == 401
