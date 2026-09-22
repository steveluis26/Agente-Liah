"""Tests de Fase 3 (panel mínimo): auth JWT, bandeja, config, métricas, seed.

Requieren Postgres + pgvector. El engine de test se configura en conftest.
"""
import os
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.core import db as db_mod
from app.core.auth import create_access_token
from app.core.base import Base
from app.main import app
from app.models import (
    ActionLog,
    Contact,
    Conversation,
    Handoff,
    Message,
    PlatformUser,
    Tenant,
    TenantConfig,
    UsageMonthly,
    UsageRecord,
)
from app.models.conversations import MODE_AI, MODE_HUMAN, MODE_RESOLVED
from app.models.platform_users import (
    ROLE_PLATFORM_ADMIN,
    ROLE_TENANT_ADMIN,
    ROLE_TENANT_AGENT,
    hash_password,
    verify_password,
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


# ── Helpers ───────────────────────────────────────────────────────────────


async def _mk_tenant(session, slug: str) -> Tenant:
    t = Tenant(slug=slug, name=f"Tenant {slug}", business_type="consultorio")
    session.add(t)
    await session.flush()
    session.add(TenantConfig(tenant_id=t.id, system_prompt="Eres Liah."))
    await session.flush()
    return t


async def _mk_user(session, email: str, role: str, password: str = "Secret-12345678",
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


async def _login(c, email: str, password: str = "Secret-12345678") -> dict:
    r = await c.post(
        "/api/v1/admin/auth/login", json={"email": email, "password": password}
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=API
    )


# ── Auth: login / tokens ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_login_ok_bad_credentials_and_token_abuse():
    async with db_mod.async_session_maker() as s:
        await _mk_user(s, "admin@liah.local", ROLE_PLATFORM_ADMIN)
        await s.commit()
    async with _client() as c:
        # OK
        r = await c.post(
            "/api/v1/admin/auth/login",
            json={"email": "admin@liah.local", "password": "Secret-12345678"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["token_type"] == "bearer"
        assert body["role"] == ROLE_PLATFORM_ADMIN
        assert body["access_token"].count(".") == 2
        # la cookie httpOnly también se fija (para la UI /admin)
        assert "liah_admin_token" in r.cookies

        # credenciales malas -> 401
        r2 = await c.post(
            "/api/v1/admin/auth/login",
            json={"email": "admin@liah.local", "password": "otra-cosa"},
        )
        assert r2.status_code == 401
        r3 = await c.post(
            "/api/v1/admin/auth/login",
            json={"email": "nadie@liah.local", "password": "Secret-12345678"},
        )
        assert r3.status_code == 401

        # token manipulado -> 401
        token = body["access_token"]
        head, payload_b64, sig = token.split(".")
        tampered = f"{head}.e30.{sig}"  # payload '{}' con firma original
        r4 = await c.get(
            "/api/v1/admin/handoffs", headers={"Authorization": f"Bearer {tampered}"}
        )
        assert r4.status_code == 401

        # sin token -> 401 (cliente fresco: el anterior guarda la cookie del login)
        async with _client() as c2:
            r5 = await c2.get("/api/v1/admin/handoffs")
            assert r5.status_code == 401


@pytest.mark.asyncio
async def test_expired_token_rejected():
    async with db_mod.async_session_maker() as s:
        u = await _mk_user(s, "exp@liah.local", ROLE_PLATFORM_ADMIN)
        await s.commit()
        uid = u.id
    expired = create_access_token(uid, ROLE_PLATFORM_ADMIN, None, expires_minutes=-1)
    async with _client() as c:
        r = await c.get(
            "/api/v1/admin/handoffs",
            headers={"Authorization": f"Bearer {expired}"},
        )
        assert r.status_code == 401
        assert "expirado" in r.json()["detail"]


@pytest.mark.asyncio
async def test_cookie_auth_works_like_bearer():
    """La UI (/admin) se autentica con la cookie httpOnly, misma verificación."""
    async with db_mod.async_session_maker() as s:
        u = await _mk_user(s, "cookie@liah.local", ROLE_PLATFORM_ADMIN)
        await s.commit()
        uid = u.id
    token = create_access_token(uid, ROLE_PLATFORM_ADMIN, None)
    async with _client() as c:
        r = await c.get(
            "/api/v1/admin/handoffs", cookies={"liah_admin_token": token}
        )
        assert r.status_code == 200


# ── Bandeja: scopes por rol ───────────────────────────────────────────────


async def _seed_two_tenants_with_handoffs():
    async with db_mod.async_session_maker() as s:
        ta = await _mk_tenant(s, "tenant-a")
        tb = await _mk_tenant(s, "tenant-b")
        ca = Contact(tenant_id=ta.id, wa_id="521100000001", name="Ana")
        cb = Contact(tenant_id=tb.id, wa_id="521100000002", name="Beto")
        s.add_all([ca, cb])
        await s.flush()
        ha = Handoff(tenant_id=ta.id, contact_id=ca.id, reason="duda", status="open")
        hb = Handoff(tenant_id=tb.id, contact_id=cb.id, reason="queja", status="open")
        s.add_all([ha, hb])
        await _mk_user(s, "agent-a@liah.local", ROLE_TENANT_AGENT,
                       tenant_id=ta.id)
        await _mk_user(s, "super@liah.local", ROLE_PLATFORM_ADMIN)
        await s.commit()
        return ta.id, tb.id, ha.id, hb.id


@pytest.mark.asyncio
async def test_handoff_scopes_by_role():
    ta_id, tb_id, ha_id, hb_id = await _seed_two_tenants_with_handoffs()
    async with _client() as c:
        h_admin = await _login(c, "super@liah.local")
        h_agent = await _login(c, "agent-a@liah.local")

        # platform_admin ve los handoffs de todos los tenants
        r = await c.get("/api/v1/admin/handoffs", headers=h_admin)
        assert r.status_code == 200
        assert {x["id"] for x in r.json()} == {str(ha_id), str(hb_id)}
        # y puede filtrar por tenant
        r = await c.get(
            "/api/v1/admin/handoffs", headers=h_admin, params={"tenant_id": str(ta_id)}
        )
        assert [x["id"] for x in r.json()] == [str(ha_id)]

        # tenant_agent solo ve los de su tenant
        r2 = await c.get("/api/v1/admin/handoffs", headers=h_agent)
        assert r2.status_code == 200
        assert [x["id"] for x in r2.json()] == [str(ha_id)]
        # filtrar por otro tenant -> 403
        r3 = await c.get(
            "/api/v1/admin/handoffs", headers=h_agent, params={"tenant_id": str(tb_id)}
        )
        assert r3.status_code == 403


@pytest.mark.asyncio
async def test_tray_flow_take_resolve_return_to_bot():
    ta_id, _, ha_id, _ = await _seed_two_tenants_with_handoffs()
    async with _client() as c:
        h_agent = await _login(c, "agent-a@liah.local")

        # take
        r = await c.post(
            f"/api/v1/admin/handoffs/{ha_id}/take", headers=h_agent
        )
        assert r.status_code == 200
        assert r.json()["status"] == "taken"
        assert r.json()["taken_by"] == "agent-a@liah.local"
        # tomar dos veces -> 409
        r = await c.post(
            f"/api/v1/admin/handoffs/{ha_id}/take", headers=h_agent
        )
        assert r.status_code == 409

        # resolve con nota -> cierra la conversación (mode resolved)
        r = await c.post(
            f"/api/v1/admin/handoffs/{ha_id}/resolve",
            headers=h_agent,
            json={"note": "Se reagendó la cita"},
        )
        assert r.status_code == 200
        async with db_mod.async_session_maker() as s:
            h = await s.get(Handoff, ha_id)
            assert h.status == "resolved"
            assert h.resolution_note == "Se reagendó la cita"
            conv = (
                await s.execute(
                    select(Conversation).where(
                        Conversation.tenant_id == ta_id
                    )
                )
            ).scalars().all()
            assert len(conv) == 1
            assert conv[0].mode == MODE_RESOLVED
            assert conv[0].closed_at is not None


@pytest.mark.asyncio
async def test_return_to_bot_reactivates_conversation_mode():
    ta_id, _, ha_id, _ = await _seed_two_tenants_with_handoffs()
    async with db_mod.async_session_maker() as s:
        # el handoff había puesto la conversación en modo humano
        from app.models.conversations import set_conversation_mode

        h = await s.get(Handoff, ha_id)
        await set_conversation_mode(s, ta_id, h.contact_id, MODE_HUMAN)
        await s.commit()
    async with _client() as c:
        h_agent = await _login(c, "agent-a@liah.local")
        r = await c.post(
            f"/api/v1/admin/handoffs/{ha_id}/return-to-bot", headers=h_agent
        )
        assert r.status_code == 200
        assert r.json()["conversation_mode"] == MODE_AI
    async with db_mod.async_session_maker() as s:
        conv = (
            await s.execute(
                select(Conversation).where(Conversation.tenant_id == ta_id)
            )
        ).scalar_one()
        assert conv.mode == MODE_AI
        assert conv.closed_at is None
        h = await s.get(Handoff, ha_id)
        assert h.status == "resolved"  # el handoff quedó cerrado


@pytest.mark.asyncio
async def test_drainer_silences_on_human_and_resumes_after_return_to_bot():
    """Nivel de modo (sin WhatsApp real): con la conversación en `human` el
    drenador no llama al LLM; tras return-to-bot vuelve a responder."""
    import app.channels.whatsapp.queue  # noqa: F401 (registro del adapter)
    from app.agent.ports import LLMResponse
    from app.channels.whatsapp.queue import drain_jobs, enqueue_job
    from app.models.conversations import set_conversation_mode

    calls = []

    class StubLLM:
        async def chat(self, messages, tools=None, tool_choice=None):
            calls.append(messages[-1]["content"])
            return LLMResponse(content="respuesta stub", finish_reason="stop")

    async def _enqueue(session, wamid, body):
        return await enqueue_job(
            session, ta_id, "PN_1",
            {"messages": [
                {"from": "521100000003", "id": wamid, "type": "text",
                 "text": {"body": body}}
            ], "contacts": [{"profile": {"name": "Cara"}}]},
        )

    async with db_mod.async_session_maker() as s:
        t = await _mk_tenant(s, "drain-t")
        ta_id = t.id
        contact = Contact(tenant_id=ta_id, wa_id="521100000003", name="Cara")
        s.add(contact)
        await s.flush()
        contact_id = contact.id
        h = Handoff(tenant_id=ta_id, contact_id=contact_id,
                    reason="tema sensible", status="open")
        s.add(h)
        await s.flush()
        handoff_id = h.id
        await set_conversation_mode(s, ta_id, contact_id, MODE_HUMAN)
        await _enqueue(s, "wamid-silencio-1", "hola")
        await s.commit()
        await _mk_user(s, "drain-agent@liah.local", ROLE_TENANT_AGENT,
                       tenant_id=ta_id)
        await s.commit()

    # 1) conversación en human -> el bot no responde
    stats = await drain_jobs(
        db_mod.async_session_maker,
        llm_factory=lambda session, tid, emb: StubLLM(),
        dry_run=True,
    )
    assert stats["done"] == 1
    assert calls == []
    async with db_mod.async_session_maker() as s:
        out = (
            await s.execute(
                select(func.count(Message.id)).where(
                    Message.tenant_id == ta_id,
                    Message.direction == "outbound",
                )
            )
        ).scalar_one()
        assert out == 0

    # 2) return-to-bot desde el panel -> el drenador vuelve a responder
    async with _client() as c:
        h_agent = await _login(c, "drain-agent@liah.local")
        r = await c.post(
            f"/api/v1/admin/handoffs/{handoff_id}/return-to-bot",
            headers=h_agent,
        )
        assert r.status_code == 200
    async with db_mod.async_session_maker() as s:
        await _enqueue(s, "wamid-vuelve-2", "hola de nuevo")
        await s.commit()
    stats = await drain_jobs(
        db_mod.async_session_maker,
        llm_factory=lambda session, tid, emb: StubLLM(),
        dry_run=True,
    )
    assert stats["done"] == 1
    assert calls == ["hola de nuevo"]
    async with db_mod.async_session_maker() as s:
        out = (
            await s.execute(
                select(func.count(Message.id)).where(
                    Message.tenant_id == ta_id,
                    Message.direction == "outbound",
                )
            )
        ).scalar_one()
        assert out == 1


# ── Config por tenant ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_config_get_put_validation_and_no_secrets():
    async with db_mod.async_session_maker() as s:
        t = await _mk_tenant(s, "cfg-t")
        tid = t.id
        await _mk_user(s, "cfg-admin@liah.local", ROLE_TENANT_ADMIN,
                       tenant_id=tid)
        await _mk_user(s, "cfg-super@liah.local", ROLE_PLATFORM_ADMIN)
        await s.commit()
    async with _client() as c:
        h_admin = await _login(c, "cfg-admin@liah.local")

        # GET: sin secretos en ninguna parte de la respuesta
        r = await c.get(f"/api/v1/admin/tenants/{tid}/config", headers=h_admin)
        assert r.status_code == 200
        blob = r.text.lower()
        for banned in ("api_key", "secret", "token", "password"):
            assert banned not in blob, f"posible secreto expuesto: {banned}"

        # PUT válido: persiste y el factory de Fase 2 lo aceptaría
        r = await c.put(
            f"/api/v1/admin/tenants/{tid}/config",
            headers=h_admin,
            json={
                "tone": "cálido y breve",
                "business_hours": {"monday": {"open": "09:00", "close": "18:00"},
                                   "sunday": "closed"},
                "model_routing": {
                    "llm_provider": "ollama",
                    "llm_model": "llama3",
                    "llm_max_tokens": 500,
                    "llm_temperature": 0.3,
                    "embedder": "fake",
                },
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["model_routing"]["llm_provider"] == "ollama"

        # el factory lo leería: construye el LLM sin error
        from app.agent.engine import build_llm_for_tenant
        from app.agent.ollama_llm import OllamaLLM

        async with db_mod.async_session_maker() as s:
            llm = await build_llm_for_tenant(s, tid)
            assert isinstance(llm, OllamaLLM)

        # PUT con provider inválido -> 422
        r = await c.put(
            f"/api/v1/admin/tenants/{tid}/config",
            headers=h_admin,
            json={"model_routing": {"llm_provider": "gpt-inventado"}},
        )
        assert r.status_code == 422

        # PUT con clave desconocida en model_routing -> 422 (extra=forbid)
        r = await c.put(
            f"/api/v1/admin/tenants/{tid}/config",
            headers=h_admin,
            json={"model_routing": {"llm_provider": "openai", "api_key": "sk-x"}},
        )
        assert r.status_code == 422

        # PUT con horario mal formado -> 422
        r = await c.put(
            f"/api/v1/admin/tenants/{tid}/config",
            headers=h_admin,
            json={"business_hours": {"lunes": {"open": "9", "close": "18:00"}}},
        )
        assert r.status_code == 422

        # tenant_admin no puede ver config de otro tenant
        async with db_mod.async_session_maker() as s:
            t2 = await _mk_tenant(s, "cfg-otro")
            await s.commit()
            tid2 = t2.id
        r = await c.get(f"/api/v1/admin/tenants/{tid2}/config", headers=h_admin)
        assert r.status_code == 403

        # platform_admin sí
        h_super = await _login(c, "cfg-super@liah.local")
        r = await c.get(f"/api/v1/admin/tenants/{tid2}/config", headers=h_super)
        assert r.status_code == 200


# ── Métricas ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_metrics_numbers_add_up():
    async with db_mod.async_session_maker() as s:
        t = await _mk_tenant(s, "met-t")
        tid = t.id
        ca = Contact(tenant_id=tid, wa_id="521200000001", name="Ana")
        cb = Contact(tenant_id=tid, wa_id="521200000002", name="Beto")
        s.add_all([ca, cb])
        await s.flush()

        base = datetime.now(timezone.utc).replace(tzinfo=None)
        # conv1: cerrada sin handoff (auto-resuelta)
        conv1 = Conversation(tenant_id=tid, contact_id=ca.id, channel="whatsapp",
                             mode=MODE_RESOLVED, opened_at=base - timedelta(hours=5),
                             closed_at=base - timedelta(hours=4))
        # conv2: cerrada CON handoff (transferencia)
        conv2 = Conversation(tenant_id=tid, contact_id=cb.id, channel="whatsapp",
                             mode=MODE_RESOLVED, opened_at=base - timedelta(hours=3),
                             closed_at=base - timedelta(hours=2))
        s.add_all([conv1, conv2])
        await s.flush()
        s.add(Handoff(tenant_id=tid, contact_id=cb.id, reason="urgencia",
                      status="resolved",
                      created_at=base - timedelta(hours=2, minutes=30)))
        # mensajes: primera respuesta 60s en conv1, 120s en conv2
        t0 = base - timedelta(hours=5)
        s.add_all([
            Message(tenant_id=tid, contact_id=ca.id, direction="inbound",
                    content="hola", created_at=t0),
            Message(tenant_id=tid, contact_id=ca.id, direction="outbound",
                    content="hola, ¿en qué ayudo?", created_at=t0 + timedelta(seconds=60)),
        ])
        t1 = base - timedelta(hours=3)
        s.add_all([
            Message(tenant_id=tid, contact_id=cb.id, direction="inbound",
                    content="me duele", created_at=t1),
            Message(tenant_id=tid, contact_id=cb.id, direction="outbound",
                    content="te paso con un humano",
                    created_at=t1 + timedelta(seconds=120)),
        ])
        # acciones exitosas
        s.add_all([
            ActionLog(tenant_id=tid, contact_id=ca.id, action="book_appointment",
                      idempotency_key="k1", status="ok"),
            ActionLog(tenant_id=tid, contact_id=ca.id, action="book_appointment",
                      idempotency_key="k2", status="ok"),
            ActionLog(tenant_id=tid, contact_id=cb.id, action="send_template",
                      idempotency_key="k3", status="failed"),
        ])
        # costo por conversación
        s.add_all([
            UsageRecord(tenant_id=tid, contact_id=ca.id, conversation_id=conv1.id,
                        model="gpt-4o-mini", tokens_in=100, tokens_out=50,
                        cost_usd=0.01),
            UsageRecord(tenant_id=tid, contact_id=cb.id, conversation_id=conv2.id,
                        model="gpt-4o-mini", tokens_in=200, tokens_out=100,
                        cost_usd=0.03),
        ])
        s.add(UsageMonthly(tenant_id=tid, year=base.year, month=base.month,
                           tokens_in=300, tokens_out=150, cost_usd=0.04))
        await _mk_user(s, "met-admin@liah.local", ROLE_TENANT_ADMIN, tenant_id=tid)
        await s.commit()

    async with _client() as c:
        h = await _login(c, "met-admin@liah.local")
        r = await c.get(f"/api/v1/admin/tenants/{tid}/metrics",
                        headers=h, params={"days": 30})
        assert r.status_code == 200, r.text
        m = r.json()
        assert m["conversations"]["total"] == 2
        assert m["conversations"]["closed"] == 2
        assert m["conversations"]["auto_resolved"] == 1
        assert m["conversations"]["auto_resolution_pct"] == 50.0
        assert m["transfers"] == 1
        assert m["avg_first_response_seconds"] == 90.0
        assert m["first_responses_measured"] == 2
        assert m["successful_actions"] == {"book_appointment": 2}
        assert m["cost_usd"]["total_window"] == 0.04
        assert m["cost_usd"]["tokens_in"] == 300
        assert m["cost_usd"]["tokens_out"] == 150
        assert m["cost_usd"]["per_conversation"] == 0.02
        assert m["cost_usd"]["monthly_total"] == 0.04


# ── Seed del operador inicial ─────────────────────────────────────────────


def _load_seed_module():
    import importlib.util

    path = os.path.join(
        os.path.dirname(__file__), "..", "scripts", "seed_platform_admin.py"
    )
    spec = importlib.util.spec_from_file_location("seed_platform_admin", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.asyncio
async def test_seed_creates_admin_once_and_never_resets(monkeypatch):
    mod = _load_seed_module()
    monkeypatch.setenv("LIAH_ADMIN_PASSWORD", "Seed-Password-12345")

    # 1) sin usuarios: crea el admin
    await mod.main()
    async with db_mod.async_session_maker() as s:
        users = (await s.execute(select(PlatformUser))).scalars().all()
        assert len(users) == 1
        assert users[0].role == ROLE_PLATFORM_ADMIN
        assert verify_password("Seed-Password-12345", users[0].password_hash)

    # 2) segunda corrida: no duplica ni resetea (aunque cambie el env)
    monkeypatch.setenv("LIAH_ADMIN_PASSWORD", "Otro-Password-99999")
    await mod.main()
    async with db_mod.async_session_maker() as s:
        users = (await s.execute(select(PlatformUser))).scalars().all()
        assert len(users) == 1
        assert verify_password("Seed-Password-12345", users[0].password_hash)
        assert not verify_password("Otro-Password-99999", users[0].password_hash)


# ── UI server-rendered ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_ui_login_and_pages():
    async with db_mod.async_session_maker() as s:
        t = await _mk_tenant(s, "ui-t")
        await _mk_user(s, "ui-admin@liah.local", ROLE_PLATFORM_ADMIN)
        await s.commit()
    async with _client() as c:
        # sin sesión -> redirect al login
        r = await c.get("/admin/handoffs", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/admin/login"

        # login por formulario fija la cookie httpOnly
        r = await c.post(
            "/admin/login",
            data={"email": "ui-admin@liah.local", "password": "Secret-12345678"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert "liah_admin_token" in r.cookies

        # con la cookie, las páginas cargan
        for page in ("/admin/handoffs", "/admin/config", "/admin/metrics"):
            r = await c.get(page)
            assert r.status_code == 200, page
            assert "Panel" in r.text
