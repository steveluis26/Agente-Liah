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
    Template,
    Tenant,
    TenantConfig,
    WhatsappChannel,
)

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


@pytest.mark.asyncio
async def test_create_tenant_returns_api_key_once():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=API) as c:
        r = await c.post(
            "/tenants",
            json={"slug": "barberia-x", "name": "Barberia X", "business_type": "barberia",
                  "system_prompt": "Eres Liah."},
        )
    assert r.status_code == 201
    body = r.json()
    assert body["api_key"].startswith("liah_live_sk_")
    # el hash se guardó, no la key en claro
    async with db_mod.async_session_maker() as s:
        t = (await s.execute(select(Tenant).where(Tenant.slug == "barberia-x"))).scalar_one()
        assert t.api_key_hash == hash_api_key(body["api_key"])
        assert t.api_key_hash != body["api_key"]


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
        created = await c.post(
            "/tenants",
            json={"slug": "consultorio-y", "name": "Consultorio Y", "business_type": "consultorio"},
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
        created = await c.post(
            "/tenants",
            json={"slug": "academia-z", "name": "Academia Z", "business_type": "academy"},
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
            r = await c.post(
                "/tenants/channels/whatsapp/embedded-signup/callback",
                json={"tenant_id": tid, "code": "CODE_ABC", "waba_id": "100",
                      "business_id": "200"},
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
