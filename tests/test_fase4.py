"""Tests de Fase 4 (alta por perfil declarativo + plantillas por giro).

Requieren Postgres + pgvector. El engine de test se configura en conftest.
"""
import os
from copy import deepcopy
from datetime import datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.agent.embedder import FakeEmbedder
from app.agent.rag import search_knowledge
from app.agent.tools import AgentContext, run_tool
from app.api.onboarding import OnboardingError, onboard_tenant
from app.core import db as db_mod
from app.core.auth import create_access_token
from app.core.base import Base
from app.core.profile_schema import (
    PerfilGiro,
    apply_overrides,
    list_templates,
    load_template,
)
from app.main import app
from app.models import (
    Appointment,
    AutomationRule,
    Contact,
    KnowledgeChunk,
    KnowledgeSource,
    PlatformUser,
    Template,
    Tenant,
    TenantConfig,
)
from app.models.platform_users import (
    ROLE_PLATFORM_ADMIN,
    ROLE_TENANT_ADMIN,
    hash_password,
    verify_password,
)
from app.reminders import dispatch

API = "http://t"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLINIC_YAML = os.path.join(REPO_ROOT, "templates", "consultorio_medico.yaml")
ESTETICA_YAML = os.path.join(REPO_ROOT, "templates", "estetica.yaml")
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


def _client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=API
    )


async def _mk_platform_admin(session, email="op@liah.mx") -> PlatformUser:
    u = PlatformUser(
        email=email,
        password_hash=hash_password(PW),
        role=ROLE_PLATFORM_ADMIN,
        tenant_id=None,
    )
    session.add(u)
    await session.flush()
    return u


def _bearer(user: PlatformUser) -> dict:
    token = create_access_token(user.id, user.role, user.tenant_id)
    return {"Authorization": f"Bearer {token}"}


async def _onboard(
    session,
    slug: str,
    template: str = "consultorio_medico",
    overrides: dict | None = None,
    admin_email: str | None = None,
    embedder=None,
):
    """Onboarding vía servicio (tests unitarios/E2E sin HTTP)."""
    return await onboard_tenant(
        session,
        template_name=template,
        slug=slug,
        nombre=None,
        overrides=overrides,
        admin_email=admin_email or f"{slug}@test.mx",
        admin_password=PW,
        embedder=embedder if embedder is not None else FakeEmbedder(),
    )


async def _tenant_count(session) -> int:
    return await session.scalar(select(func.count()).select_from(Tenant))


# ── Schema del perfil ─────────────────────────────────────────────────────


def test_schema_valid_templates():
    p = load_template(CLINIC_YAML)
    assert p.giro == "consultorio_medico"
    assert p.schema_version == "1.0"
    assert len(p.conocimiento_semilla) >= 1
    assert "book_appointment" in p.herramientas_habilitadas
    e = load_template(ESTETICA_YAML)
    assert e.giro == "estetica"


def test_schema_missing_system_prompt_fails():
    import yaml

    data = yaml.safe_load(open(CLINIC_YAML, encoding="utf-8").read())
    del data["system_prompt"]
    with pytest.raises(Exception, match="system_prompt"):
        PerfilGiro(**data)


def test_schema_unknown_key_fails():
    import yaml

    data = yaml.safe_load(open(CLINIC_YAML, encoding="utf-8").read())
    data["campo_inventado"] = "x"
    with pytest.raises(Exception, match="campo_inventado"):
        PerfilGiro(**data)


def test_schema_unknown_tool_fails():
    import yaml

    data = yaml.safe_load(open(CLINIC_YAML, encoding="utf-8").read())
    data["herramientas_habilitadas"] = ["herramienta_que_no_existe"]
    with pytest.raises(Exception, match="herramientas desconocidas"):
        PerfilGiro(**data)


def test_schema_bad_timezone_and_slug_fail():
    import yaml

    data = yaml.safe_load(open(CLINIC_YAML, encoding="utf-8").read())
    bad = deepcopy(data)
    bad["timezone"] = "Marte/Olimpo"
    with pytest.raises(Exception, match="timezone"):
        PerfilGiro(**bad)
    bad = deepcopy(data)
    bad["slug"] = "Slug Con Mayúsculas"
    with pytest.raises(Exception, match="slug"):
        PerfilGiro(**bad)


def test_overrides_merge_and_reject_unknown():
    import yaml

    data = yaml.safe_load(open(CLINIC_YAML, encoding="utf-8").read())
    merged = apply_overrides(data, {"tono": "formal", "nombre": "Clínica Real"})
    assert merged["tono"] == "formal"
    assert merged["nombre"] == "Clínica Real"
    # merge profundo de dicts
    merged = apply_overrides(data, {"horarios": {"sunday": {"open": "09:00", "close": "13:00"}}})
    assert merged["horarios"]["sunday"] == {"open": "09:00", "close": "13:00"}
    assert merged["horarios"]["monday"]["open"] == "09:00"
    with pytest.raises(ValueError, match="override inválido"):
        apply_overrides(data, {"clave_fantasma": 1})


def test_list_templates():
    items = list_templates()
    names = {t["template"] for t in items if "error" not in t}
    assert {"consultorio_medico", "estetica"} <= names


# ── Onboarding E2E ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_onboard_e2e_creates_everything():
    async with db_mod.async_session_maker() as s:
        result = await _onboard(s, "clinica-norte")
    assert result["slug"] == "clinica-norte"
    assert result["giro"] == "consultorio_medico"
    assert result["api_key"].startswith("liah_live_sk_")
    # la password jamás se devuelve ni se persiste en claro
    assert "admin_password" not in result
    assert result["summary"]["reglas"] == 2
    assert result["summary"]["plantillas_hsm"] == 4
    assert result["summary"]["conocimiento_items"] == 4

    async with db_mod.async_session_maker() as s:
        tenant = (
            await s.execute(select(Tenant).where(Tenant.slug == "clinica-norte"))
        ).scalar_one()
        assert tenant.business_type == "consultorio_medico"

        cfg = (
            await s.execute(
                select(TenantConfig).where(TenantConfig.tenant_id == tenant.id)
            )
        ).scalar_one()
        assert "jamás das diagnósticos" in cfg.system_prompt
        assert cfg.tone == "cálido-profesional"
        assert cfg.business_hours["sunday"] == "closed"
        # model_routing comercial por default (openai), salvo override
        assert cfg.model_routing["llm_provider"] == "openai"
        assert cfg.model_routing["embedder"] == "openai"
        assert cfg.extra["enabled_tools"] == [
            "search_knowledge_base", "check_availability", "book_appointment",
            "cancel_appointment", "reschedule_appointment", "escalate_to_human",
        ]
        assert "urgencias médicas o emergencias" in cfg.extra["temas_sensibles"]

        rules = (
            await s.execute(
                select(AutomationRule).where(AutomationRule.tenant_id == tenant.id)
            )
        ).scalars().all()
        tipos = {r.type for r in rules}
        assert "appointment_reminder" in tipos
        reminder = next(r for r in rules if r.type == "appointment_reminder")
        assert reminder.params["hours_before"] == [24, 2]
        assert reminder.params["template_name"] == "recordatorio_cita"

        tpls = (
            await s.execute(
                select(Template).where(Template.tenant_id == tenant.id)
            )
        ).scalars().all()
        assert {t.name for t in tpls} == {
            "recordatorio_cita", "confirmacion_cita",
            "aviso_cancelacion", "seguimiento_consulta",
        }
        assert all(t.status == "pending" for t in tpls)  # requieren aprobación de Meta

        chunks = (
            await s.execute(
                select(func.count()).select_from(KnowledgeChunk).where(
                    KnowledgeChunk.tenant_id == tenant.id
                )
            )
        ).scalar()
        assert chunks > 0

        admin = (
            await s.execute(
                select(PlatformUser).where(
                    PlatformUser.email == "clinica-norte@test.mx"
                )
            )
        ).scalar_one()
        assert admin.role == ROLE_TENANT_ADMIN
        assert admin.tenant_id == tenant.id
        # la password quedó hasheada, jamás en claro
        assert admin.password_hash != PW
        assert verify_password(PW, admin.password_hash)


@pytest.mark.asyncio
async def test_onboard_model_routing_override():
    """El override model_routing se valida y persiste (embedder fake en test)."""
    async with db_mod.async_session_maker() as s:
        # sin inyección de embedder: el factory debe construir el fake
        result = await onboard_tenant(
            s,
            template_name="consultorio_medico",
            slug="clinica-fake",
            nombre=None,
            overrides={"model_routing": {"embedder": "fake", "llm_model": "gpt-4o"}},
            admin_email="fake@test.mx",
            admin_password=PW,
            embedder=None,
        )
    async with db_mod.async_session_maker() as s:
        tenant = (
            await s.execute(select(Tenant).where(Tenant.slug == "clinica-fake"))
        ).scalar_one()
        cfg = (
            await s.execute(
                select(TenantConfig).where(TenantConfig.tenant_id == tenant.id)
            )
        ).scalar_one()
        assert cfg.model_routing["embedder"] == "fake"
        assert cfg.model_routing["llm_model"] == "gpt-4o"
        assert cfg.model_routing["llm_provider"] == "openai"  # default conservado


@pytest.mark.asyncio
async def test_seed_knowledge_is_retrievable_by_rag():
    """El conocimiento semilla se recupera por RAG (pregunta por un precio)."""
    async with db_mod.async_session_maker() as s:
        result = await _onboard(s, "clinica-rag")
        tid = result["tenant_id"]
    async with db_mod.async_session_maker() as s:
        import uuid as uuid_mod

        hits = await search_knowledge(
            s, uuid_mod.UUID(tid), "¿cuánto cuesta el electrocardiograma?",
            FakeEmbedder(), threshold=0.0,
        )
        assert hits, "el RAG no recuperó nada del conocimiento semilla"
        assert any("450" in h["content"] for h in hits)


@pytest.mark.asyncio
async def test_onboard_duplicate_slug_nothing_persists():
    async with db_mod.async_session_maker() as s:
        await _onboard(s, "clinica-dup")
        before = await _tenant_count(s)
        with pytest.raises(OnboardingError) as exc:
            await _onboard(s, "clinica-dup", admin_email="otro@test.mx")
        assert exc.value.code == "slug_exists"
        assert await _tenant_count(s) == before


@pytest.mark.asyncio
async def test_onboard_rolls_back_on_mid_transaction_failure(monkeypatch):
    """Si la ingesta falla a mitad del alta, el rollback es total."""
    import app.api.onboarding as ob

    async def _boom(*a, **k):
        raise RuntimeError("fallo simulado de ingesta")

    monkeypatch.setattr(ob, "ingest_knowledge", _boom)
    async with db_mod.async_session_maker() as s:
        with pytest.raises(RuntimeError, match="fallo simulado"):
            await _onboard(s, "clinica-boom", admin_email="boom@test.mx")
    async with db_mod.async_session_maker() as s:
        assert await _tenant_count(s) == 0
        assert (await s.execute(select(func.count()).select_from(AutomationRule))).scalar() == 0
        assert (await s.execute(select(func.count()).select_from(Template))).scalar() == 0
        assert (await s.execute(select(func.count()).select_from(KnowledgeSource))).scalar() == 0
        assert (
            await s.execute(
                select(func.count()).select_from(PlatformUser).where(
                    PlatformUser.email == "boom@test.mx"
                )
            )
        ).scalar() == 0


@pytest.mark.asyncio
async def test_onboard_duplicate_admin_email_rejected():
    async with db_mod.async_session_maker() as s:
        await _onboard(s, "clinica-mail1", admin_email="mismo@test.mx")
        with pytest.raises(OnboardingError) as exc:
            await _onboard(s, "clinica-mail2", admin_email="mismo@test.mx")
        assert exc.value.code == "email_exists"
        assert await _tenant_count(s) == 1


# ── Aislamiento entre tenants ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tenants_do_not_share_knowledge():
    """Dos tenants del mismo template no comparten conocimiento en el RAG."""
    import uuid as uuid_mod

    from app.agent.rag import ingest_knowledge

    async with db_mod.async_session_maker() as s:
        ra = await _onboard(s, "clinica-aisla-a")
        rb = await _onboard(s, "clinica-aisla-b")
        tid_a, tid_b = uuid_mod.UUID(ra["tenant_id"]), uuid_mod.UUID(rb["tenant_id"])
        # Contenido único agregado solo al tenant B (post-alta, como haría el operador)
        await ingest_knowledge(
            s, tid_b, "Nota interna B", "Palabra secreta del tenant B: xochimilco77",
            FakeEmbedder(),
        )
    async with db_mod.async_session_maker() as s:
        # A no ve el contenido exclusivo de B...
        hits_a = await search_knowledge(
            s, tid_a, "xochimilco77", FakeEmbedder(), threshold=0.0
        )
        assert not any("xochimilco77" in h["content"] for h in hits_a)
        # ...y todo lo que recupera A pertenece a A.
        hits = await search_knowledge(
            s, tid_a, "precio consulta medicina", FakeEmbedder(), threshold=0.0
        )
        assert hits
        for h in hits:
            src = await s.get(KnowledgeSource, uuid_mod.UUID(h["source_id"]))
            assert src.tenant_id == tid_a
        # B sí ve su contenido.
        hits_b = await search_knowledge(
            s, tid_b, "xochimilco77", FakeEmbedder(), threshold=0.0
        )
        assert any("xochimilco77" in h["content"] for h in hits_b)


# ── Criterio estrella: dos clientes del mismo template, ambos operan ──────


@pytest.mark.asyncio
async def test_two_clients_same_template_both_operate():
    """Un segundo cliente se activa con YAML + credenciales, sin tocar código.

    Ambos tenants (mismo template, distinto slug) agendan/cancelan/
    reprograman citas y consultan su conocimiento de forma aislada.
    """
    import uuid as uuid_mod

    async with db_mod.async_session_maker() as s:
        ra = await _onboard(s, "clinica-sur", admin_email="sur@test.mx")
        rb = await _onboard(s, "clinica-poniente", admin_email="poniente@test.mx")
        tids = [uuid_mod.UUID(ra["tenant_id"]), uuid_mod.UUID(rb["tenant_id"])]

        for i, tid in enumerate(tids):
            contact = Contact(
                tenant_id=tid, wa_id=f"52155500{i:04d}",
                name=f"Paciente {i}", consent_status="granted",
            )
            s.add(contact)
            await s.flush()
            ctx = AgentContext(s, tid, contact.id, FakeEmbedder())
            # agendar
            r = await run_tool(
                "book_appointment",
                {"date": "2026-10-05", "time_slot": "10:00", "type": "consultation"},
                ctx,
            )
            assert r["ok"], r
            # reprogramar (atómica: cancela la vieja, reserva la nueva)
            r2 = await run_tool(
                "reschedule_appointment",
                {"old_date": "2026-10-05", "old_time_slot": "10:00",
                 "new_date": "2026-10-06", "new_time_slot": "11:00",
                 "type": "consultation"},
                ctx,
            )
            assert r2["ok"], r2
            assert r2["cancelled_event_id"] == r["event_id"]
            # el RAG de cada tenant trae sus precios
            hits = await search_knowledge(
                s, tid, "precio consulta medicina general", FakeEmbedder(),
                threshold=0.0,
            )
            assert any("600" in h["content"] for h in hits), f"tenant {i} sin RAG"

        # aislamiento de citas: cada tenant ve solo las suyas
        for tid in tids:
            n = await s.scalar(
                select(func.count()).select_from(Appointment).where(
                    Appointment.tenant_id == tid, Appointment.status == "confirmed"
                )
            )
            assert n == 1


@pytest.mark.asyncio
async def test_cancel_and_reschedule_tools():
    """cancel_appointment y reschedule_appointment vía tools + calendario."""
    async with db_mod.async_session_maker() as s:
        result = await _onboard(s, "clinica-tools")
        import uuid as uuid_mod

        tid = uuid_mod.UUID(result["tenant_id"])
        contact = Contact(tenant_id=tid, wa_id="521555009999", name="Ana",
                          consent_status="granted")
        s.add(contact)
        await s.flush()
        ctx = AgentContext(s, tid, contact.id, FakeEmbedder())

        r = await run_tool(
            "book_appointment",
            {"date": "2026-11-02", "time_slot": "09:00", "type": "consultation"},
            ctx,
        )
        assert r["ok"]

        # cancelar una cita inexistente -> ok=False, sin crash
        r_none = await run_tool(
            "cancel_appointment", {"date": "2026-11-03", "time_slot": "09:00"}, ctx
        )
        assert r_none["ok"] is False

        # cancelar la real
        r_cancel = await run_tool(
            "cancel_appointment", {"date": "2026-11-02", "time_slot": "09:00"}, ctx
        )
        assert r_cancel["ok"] is True
        appt = await s.get(Appointment, uuid_mod.UUID(r["event_id"]))
        assert appt.status == "cancelled"

        # reprogramar sobre slot ocupado conserva la cita original
        r1 = await run_tool(
            "book_appointment",
            {"date": "2026-11-04", "time_slot": "09:00", "type": "consultation"},
            ctx,
        )
        r2 = await run_tool(
            "book_appointment",
            {"date": "2026-11-04", "time_slot": "10:00", "type": "consultation"},
            ctx,
        )
        assert r1["ok"] and r2["ok"]
        r_bad = await run_tool(
            "reschedule_appointment",
            {"old_date": "2026-11-04", "old_time_slot": "10:00",
             "new_date": "2026-11-04", "new_time_slot": "09:00",
             "type": "consultation"},
            ctx,
        )
        assert r_bad["ok"] is False
        assert "conserva" in r_bad["error"]
        orig = await s.get(Appointment, uuid_mod.UUID(r2["event_id"]))
        assert orig.status == "confirmed"


@pytest.mark.asyncio
async def test_appointment_reminder_rule_targets():
    """La regla appointment_reminder del template genera targets 24h/2h."""
    async with db_mod.async_session_maker() as s:
        result = await _onboard(s, "clinica-rem")
        import uuid as uuid_mod

        tid = uuid_mod.UUID(result["tenant_id"])
        rule = (
            await s.execute(
                select(AutomationRule).where(
                    AutomationRule.tenant_id == tid,
                    AutomationRule.type == "appointment_reminder",
                )
            )
        ).scalar_one()
        contact = Contact(tenant_id=tid, wa_id="521555008888", name="Luis",
                          consent_status="granted")
        s.add(contact)
        await s.flush()

        now = datetime.utcnow().replace(microsecond=0)
        # cita en 23h -> dispara el recordatorio de 24h (el de 2h aún no)
        s.add(Appointment(tenant_id=tid, contact_id=contact.id, type="consultation",
                          start_at=now + timedelta(hours=23), status="confirmed"))
        # cita en 30h -> ningún recordatorio todavía
        s.add(Appointment(tenant_id=tid, contact_id=contact.id, type="consultation",
                          start_at=now + timedelta(hours=30), status="confirmed"))
        await s.commit()

        targets = await dispatch.load_rule_targets(
            s, tid, "Clínica Ejemplo Norte", "appointment_reminder", now
        )
        assert len(targets) == 1
        r, c, scheduled_for, vars_, appt_id = targets[0]
        assert c.id == contact.id
        assert appt_id is not None
        assert vars_ == ["Luis", "Clínica Ejemplo Norte",
                         (now + timedelta(hours=23)).strftime("%d/%m/%Y"), 
                         (now + timedelta(hours=23)).strftime("%H:%M")]
        # cita en 1h -> disparan AMBOS (24h y 2h, scheduled_for distintos)
        s.add(Appointment(tenant_id=tid, contact_id=contact.id, type="consultation",
                          start_at=now + timedelta(hours=1), status="confirmed"))
        await s.commit()
        targets = await dispatch.load_rule_targets(
            s, tid, "Clínica Ejemplo Norte", "appointment_reminder", now
        )
        assert len(targets) == 3
        scheduled = {(t[2], t[4]) for t in targets}
        assert len(scheduled) == 3  # cada (cita, h) es un recordatorio distinto


# ── Auth del endpoint ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_onboard_requires_platform_admin():
    async with _client() as c:
        # sin credenciales -> 401
        r = await c.post(
            "/api/v1/admin/tenants/onboard",
            json={"template": "consultorio_medico", "slug": "x",
                  "admin_email": "a@b.mx", "admin_password": PW},
        )
        assert r.status_code == 401
        r = await c.get("/api/v1/admin/templates")
        assert r.status_code == 401

    async with db_mod.async_session_maker() as s:
        result = await _onboard(s, "clinica-auth", admin_email="tenantadmin@test.mx")
        import uuid as uuid_mod

        tenant_admin = (
            await s.execute(
                select(PlatformUser).where(
                    PlatformUser.email == "tenantadmin@test.mx"
                )
            )
        ).scalar_one()
    # tenant_admin NO puede onboardear -> 403
    async with _client() as c:
        r = await c.post(
            "/api/v1/admin/tenants/onboard",
            json={"template": "consultorio_medico", "slug": "y",
                  "admin_email": "b@c.mx", "admin_password": PW},
            headers=_bearer(tenant_admin),
        )
        assert r.status_code == 403
        r = await c.get("/api/v1/admin/templates", headers=_bearer(tenant_admin))
        assert r.status_code == 403


@pytest.mark.asyncio
async def test_onboard_http_e2e_and_tenant_admin_login():
    """HTTP: platform_admin onboardea; el tenant_admin creado puede loguearse."""
    async with db_mod.async_session_maker() as s:
        op = await _mk_platform_admin(s, "op-http@liah.mx")
        await s.commit()
    async with _client() as c:
        headers = _bearer(op)
        r = await c.get("/api/v1/admin/templates", headers=headers)
        assert r.status_code == 200
        names = {t["template"] for t in r.json() if "error" not in t}
        assert {"consultorio_medico", "estetica"} <= names

        r = await c.post(
            "/api/v1/admin/tenants/onboard",
            json={
                "template": "estetica",
                "slug": "estetica-http",
                "nombre": "Estética HTTP",
                "overrides": {"model_routing": {"embedder": "fake"}},
                "admin_email": "duena@estetica-http.mx",
                "admin_password": PW,
            },
            headers=headers,
        )
        assert r.status_code == 201, r.text
        data = r.json()
        assert data["slug"] == "estetica-http"
        assert data["nombre"] == "Estética HTTP"
        assert data["giro"] == "estetica"
        assert data["api_key"].startswith("liah_live_sk_")
        assert "admin_password" not in data

        # slug duplicado -> 409
        r2 = await c.post(
            "/api/v1/admin/tenants/onboard",
            json={"template": "estetica", "slug": "estetica-http",
                  "admin_email": "otra@x.mx", "admin_password": PW},
            headers=headers,
        )
        assert r2.status_code == 409

        # la API key del tenant sirve en los endpoints X-Tenant-API-Key...
        r3 = await c.get(
            "/tenants/me/config",
            headers={"X-Tenant-API-Key": data["api_key"]},
        )
        assert r3.status_code == 200, r3.text

        # ...y el tenant_admin creado puede loguearse al panel
        r4 = await c.post(
            "/api/v1/admin/auth/login",
            json={"email": "duena@estetica-http.mx", "password": PW},
        )
        assert r4.status_code == 200, r4.text
        th = {"Authorization": f"Bearer {r4.json()['access_token']}"}
        r5 = await c.get("/api/v1/admin/tenants", headers=th)
        assert r5.status_code == 200
        tenants = r5.json()
        assert len(tenants) == 1 and tenants[0]["slug"] == "estetica-http"


# ── CLI ───────────────────────────────────────────────────────────────────


def _run_cli(args: list[str]):
    """Corre el CLI como lo haría el operador (proceso aparte, misma BD test)."""
    import subprocess
    import sys

    env = dict(os.environ)
    env["LIAH_TENANT_ADMIN_PASSWORD"] = PW
    env["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
    return subprocess.run(
        [sys.executable, os.path.join(REPO_ROOT, "scripts", "onboard_tenant.py")]
        + args,
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
        timeout=180,
    )


@pytest.mark.asyncio
async def test_cli_onboards_tenant():
    """El CLI crea el tenant contra la BD (password por env, sin default)."""
    proc = await _run_in_thread(_run_cli, [
        CLINIC_YAML,
        "--slug", "clinica-cli",
        "--nombre", "Clínica CLI",
        "--admin-email", "cli@test.mx",
        "--overrides-json", '{"model_routing": {"embedder": "fake"}}',
    ])
    assert proc.returncode == 0, proc.stderr
    assert "liah_live_sk_" in proc.stdout  # la API key se muestra una vez
    async with db_mod.async_session_maker() as s:
        tenant = (
            await s.execute(select(Tenant).where(Tenant.slug == "clinica-cli"))
        ).scalar_one()
        assert tenant.name == "Clínica CLI"
        admin = (
            await s.execute(
                select(PlatformUser).where(PlatformUser.email == "cli@test.mx")
            )
        ).scalar_one()
        assert admin.role == ROLE_TENANT_ADMIN
        assert verify_password(PW, admin.password_hash)
        n_rules = await s.scalar(
            select(func.count()).select_from(AutomationRule).where(
                AutomationRule.tenant_id == tenant.id
            )
        )
        assert n_rules == 2


@pytest.mark.asyncio
async def test_cli_duplicate_slug_exits_2():
    args = [CLINIC_YAML, "--slug", "clinica-cli2", "--admin-email", "c2@test.mx",
            "--overrides-json", '{"model_routing": {"embedder": "fake"}}']
    proc1 = await _run_in_thread(_run_cli, args)
    assert proc1.returncode == 0, proc1.stderr
    proc2 = await _run_in_thread(_run_cli, args)
    assert proc2.returncode == 2  # slug duplicado
    async with db_mod.async_session_maker() as s:
        assert await _tenant_count(s) == 1


async def _run_in_thread(fn, *args):
    """Corre una función bloqueante (subprocess) sin atascar el loop de pytest."""
    import asyncio

    return await asyncio.to_thread(fn, *args)


# ── UI ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_onboard_page_requires_platform_admin():
    async with db_mod.async_session_maker() as s:
        op = await _mk_platform_admin(s, "op-ui@liah.mx")
        ta_user = PlatformUser(
            email="ta-ui@test.mx", password_hash=hash_password(PW),
            role=ROLE_TENANT_ADMIN, tenant_id=None,
        )
        s.add(ta_user)
        await s.commit()
    async with _client() as c:
        # sin sesión -> redirect al login
        r = await c.get("/admin/onboard", follow_redirects=False)
        assert r.status_code == 303
        # tenant_admin -> página con error (no puede dar de alta)
        r = await c.get("/admin/onboard", headers=_bearer(ta_user))
        assert r.status_code == 200
        assert "Solo un platform_admin" in r.text
        # platform_admin -> formulario
        r = await c.get("/admin/onboard", headers=_bearer(op))
        assert r.status_code == 200
        assert "Nuevo cliente desde plantilla" in r.text
        assert "/api/v1/admin/tenants/onboard" in r.text
