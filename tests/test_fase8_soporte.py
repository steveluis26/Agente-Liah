"""Tests de Fase 8 (empaque/soporte): plan de renta, suspensión, rate limit, logs.

Cubre:
- is_tenant_active: active=True; suspended/trial/inexistente=False
- webhook: tenant suspendido -> 200 sin encolar jobs (no gasta proceso)
- PATCH /tenants/{id}/plan: platform_admin cambia plan/status/billing_ref;
  tenant_admin -> 403; plan inválido -> 422
- list_tenants incluye plan y status
- RateLimitMiddleware: 429 tras superar el límite (aislado, reglas tuneadas)
- logging estructurado: TenantIdFilter inyecta tenant_id sin romper
"""
import hashlib
import hmac
import json
import logging
import os

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

import asyncpg

from app.core import db as db_mod
from app.core.base import Base
from app.core.billing import is_tenant_active
from app.core.logging import TenantIdFilter, setup_logging
from app.core.ratelimit import RateLimitMiddleware
from app.core import ratelimit as ratelimit_mod
from app.core.tenant_ctx import clear_tenant_id, set_tenant_id
from app.main import app
from app.models import PlatformUser, Tenant, TenantConfig, WhatsappChannel
from app.models.webhook_jobs import WebhookJob as Job
from app.models.platform_users import hash_password

API = "http://t"


@pytest_asyncio.fixture(autouse=True)
async def _schema():
    url = os.getenv("TEST_DATABASE_URL", "").replace("+asyncpg", "")
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


async def _tenant(s, slug, status="active", plan="renta"):
    t = Tenant(slug=slug, name=f"Tenant {slug}", business_type="consultorio",
               status=status, plan=plan)
    s.add(t)
    await s.flush()
    s.add(TenantConfig(tenant_id=t.id, system_prompt="x"))
    await s.flush()
    return t


async def _user(s, email, role, tenant_id=None):
    u = PlatformUser(email=email, password_hash=hash_password("Secret-12345678"),
                     role=role, tenant_id=tenant_id)
    s.add(u)
    await s.flush()
    return u


def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


async def _login(c, email):
    r = await c.post("/api/v1/admin/auth/login",
                     json={"email": email, "password": "Secret-12345678"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


# ── is_tenant_active ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tenant_active_matrix():
    async with db_mod.async_session_maker() as s:
        ta = await _tenant(s, "t-active")
        ts = await _tenant(s, "t-susp", status="suspended")
        tt = await _tenant(s, "t-trial", status="trial")
        await s.commit()
        assert await is_tenant_active(s, ta.id) is True
        assert await is_tenant_active(s, ts.id) is False
        assert await is_tenant_active(s, tt.id) is False
        import uuid
        assert await is_tenant_active(s, uuid.uuid4()) is False


# ── webhook: suspendido no encola ──────────────────────────────────────

def _sign(body: bytes) -> str:
    from app.core.config import get_settings as _gs
    return "sha256=" + hmac.new(_gs().whatsapp_app_secret.encode(), body, hashlib.sha256).hexdigest()


def _payload(phone_number_id, wa_from="5215550001111"):
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA1", "changes": [{
            "field": "messages",
            "value": {
                "messaging_product": "whatsapp",
                "metadata": {"display_phone_number": "+5215555550000",
                             "phone_number_id": phone_number_id},
                "messages": [{"from": wa_from, "id": "wamid.SUSP1",
                              "type": "text", "timestamp": "1690000000",
                              "text": {"body": "Hola"}}],
            }}]}],
    }


@pytest.mark.asyncio
async def test_webhook_suspended_tenant_discards():
    async with db_mod.async_session_maker() as s:
        t = await _tenant(s, "t-susp-web", status="suspended")
        s.add(WhatsappChannel(tenant_id=t.id, phone_number_id="999888777",
                              verify_token="tok"))
        await s.commit()
    body = json.dumps(_payload("999888777")).encode()
    async with _client() as c:
        r = await c.post("/webhook/whatsapp", content=body,
                         headers={"X-Hub-Signature-256": _sign(body)})
    assert r.status_code == 200  # Meta no reintenta
    async with db_mod.async_session_maker() as s:
        n = (await s.execute(select(func.count()).select_from(Job))).scalar()
    assert n == 0  # nada encolado


# ── PATCH plan (platform_admin) ────────────────────────────────────────

@pytest.mark.asyncio
async def test_update_tenant_plan_platform_admin():
    async with db_mod.async_session_maker() as s:
        t = await _tenant(s, "t-plan")
        await _user(s, "root@liah.test", "platform_admin")
        await _user(s, "op@t-plan.test", "tenant_admin", tenant_id=t.id)
        await s.commit()
        tid = t.id
    async with _client() as c:
        h_admin = await _login(c, "root@liah.test")
        h_tenant = await _login(c, "op@t-plan.test")

        r = await c.patch(f"/api/v1/admin/tenants/{tid}/plan",
                          json={"plan": "compra_unica", "status": "suspended",
                                "billing_ref": "MP-123"},
                          headers=h_admin)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["plan"] == "compra_unica"
        assert data["status"] == "suspended"
        assert data["billing_ref"] == "MP-123"

        # tenant_admin no puede tocar facturación
        r2 = await c.patch(f"/api/v1/admin/tenants/{tid}/plan",
                           json={"status": "active"}, headers=h_tenant)
        assert r2.status_code == 403

        # plan inválido -> 422
        r3 = await c.patch(f"/api/v1/admin/tenants/{tid}/plan",
                           json={"plan": "gratis"}, headers=h_admin)
        assert r3.status_code == 422

        # list_tenants expone plan y status
        r4 = await c.get("/api/v1/admin/tenants", headers=h_admin)
        assert r4.status_code == 200
        row = [x for x in r4.json() if x["slug"] == "t-plan"][0]
        assert row["plan"] == "compra_unica" and row["status"] == "suspended"


# ── rate limit (aislado) ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ratelimit_middleware_429(monkeypatch):
    monkeypatch.setattr(ratelimit_mod, "_RULES",
                        [("/api/v1/admin/auth/login", 2, 60), ("", 600, 60)])

    async def ok(request):
        return PlainTextResponse("ok")

    inner = Starlette(routes=[Route("/api/v1/admin/auth/login", ok,
                                   methods=["POST"])])
    app_rl = RateLimitMiddleware(inner)
    transport = httpx.ASGITransport(app=app_rl)
    async with httpx.AsyncClient(transport=transport,
                                base_url="http://t") as c:
        assert (await c.post("/api/v1/admin/auth/login")).status_code == 200
        assert (await c.post("/api/v1/admin/auth/login")).status_code == 200
        r = await c.post("/api/v1/admin/auth/login")
        assert r.status_code == 429
        assert "Retry-After" in r.headers


# ── logging estructurado ───────────────────────────────────────────────

def test_tenant_id_filter_without_context():
    clear_tenant_id()
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "hola", (), None)
    assert TenantIdFilter().filter(rec) is True
    assert rec.tenant_id == "-"


def test_tenant_id_filter_with_context():
    import uuid
    tid = uuid.uuid4()
    set_tenant_id(tid)
    try:
        rec = logging.LogRecord("x", logging.INFO, __file__, 1, "hola", (), None)
        TenantIdFilter().filter(rec)
        assert rec.tenant_id == str(tid)
    finally:
        clear_tenant_id()


def test_setup_logging_no_crash():
    setup_logging(json_format=True)
    setup_logging(json_format=False)
    logging.getLogger("liah.test").info("smoke")
