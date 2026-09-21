"""Tests de Fase 2: conector OpenAI comercial + costeo por tenant.

Requieren Postgres + pgvector. El engine de test se configura en conftest.
Sin red real: la API de OpenAI se mockea con respx.
"""
import asyncio
import os

import httpx
import pytest
import pytest_asyncio
import respx
from sqlalchemy import func, select

from app.agent.costing import aggregate_monthly_usage, cost_usd, record_turn_usage
from app.agent.embedder import EMBED_DIM, FakeEmbedder, validate_embed_dim
from app.agent.engine import build_embedder_for_tenant, build_llm_for_tenant, run_agent
from app.agent.llm import OpenAILLM
from app.agent.ollama_embedder import OllamaEmbedder
from app.agent.ollama_llm import OllamaLLM
from app.agent.ports import LLMResponse
from app.agent.secrets import (
    EnvSecretProvider,
    SecretNotFoundError,
    VaultSecretProvider,
    resolve_tenant_openai_key,
    require_tenant_openai_key,
)
from app.api.knowledge import KnowledgeIngest
from app.core.base import Base
from app.core import db as db_mod
from app.main import app
from app.models import Contact, Tenant, TenantConfig, UsageMonthly, UsageRecord


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


async def _make_tenant(s, slug="clinica-test", routing=None):
    t = Tenant(slug=slug, name="Clínica Test", business_type="clinica")
    s.add(t)
    await s.flush()
    s.add(
        TenantConfig(
            tenant_id=t.id,
            system_prompt="Eres Liah, asistente de la clínica.",
            model_routing=routing or {},
        )
    )
    await s.flush()
    return t


async def _make_contact(s, tenant_id, wa_id="5215550000001"):
    c = Contact(tenant_id=tenant_id, wa_id=wa_id, name="Paciente Test")
    s.add(c)
    await s.flush()
    return c


def _ok_payload():
    return {
        "choices": [
            {"message": {"content": "Hola, ¿en qué te ayudo?",
                         "tool_calls": None}}
        ],
        "usage": {"prompt_tokens": 120, "completion_tokens": 30,
                  "total_tokens": 150},
    }


# ── OpenAILLM: usage, retry, trust_env ────────────────────────────

@respx.mock
async def test_openai_llm_captures_usage():
    respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=_ok_payload())
    )
    llm = OpenAILLM(api_key="sk-test", model="gpt-4o-mini")
    resp = await llm.chat([{"role": "user", "content": "hola"}])
    assert resp.content == "Hola, ¿en qué te ayudo?"
    assert resp.usage["prompt_tokens"] == 120
    assert resp.usage["completion_tokens"] == 30
    assert resp.usage["total_tokens"] == 150


@respx.mock
async def test_openai_llm_retries_on_429_then_succeeds(monkeypatch):
    async def _no_sleep(_delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    route = respx.post("https://api.openai.com/v1/chat/completions")
    route.side_effect = [
        httpx.Response(429, json={"error": {"message": "rate limit"}}),
        httpx.Response(200, json=_ok_payload()),
    ]
    llm = OpenAILLM(api_key="sk-test")
    resp = await llm.chat([{"role": "user", "content": "hola"}])
    assert route.call_count == 2
    assert resp.usage["prompt_tokens"] == 120


@respx.mock
async def test_openai_llm_fails_fast_on_400():
    respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(400, json={"error": {"message": "bad"}})
    )
    llm = OpenAILLM(api_key="sk-test")
    with pytest.raises(httpx.HTTPStatusError):
        await llm.chat([{"role": "user", "content": "hola"}])


async def test_openai_llm_uses_trust_env_false(monkeypatch):
    """La VM mete proxies por env vars: el cliente debe ignorarlos."""
    seen = {}

    class _Client:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            class _R:
                status_code = 200

                def raise_for_status(self):
                    pass

                def json(self):
                    return _ok_payload()

            return _R()

    monkeypatch.setattr("httpx.AsyncClient", _Client)
    llm = OpenAILLM(api_key="sk-test")
    await llm.chat([{"role": "user", "content": "hola"}])
    assert seen.get("trust_env") is False


async def test_openai_llm_sends_configured_max_tokens_temperature(monkeypatch):
    sent = {}

    class _Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            sent.update(json)

            class _R:
                status_code = 200

                def raise_for_status(self):
                    pass

                def json(self):
                    return _ok_payload()

            return _R()

    monkeypatch.setattr("httpx.AsyncClient", _Client)
    llm = OpenAILLM(api_key="sk-test", max_tokens=500, temperature=0.7)
    await llm.chat([{"role": "user", "content": "hola"}])
    assert sent["max_tokens"] == 500
    assert sent["temperature"] == 0.7
    assert sent["model"] == "gpt-4o-mini"


# ── Secretos por tenant ───────────────────────────────────────────

async def test_env_secret_provider_resolves_tenant_key(monkeypatch):
    p = EnvSecretProvider()
    monkeypatch.setenv("OPENAI_API_KEY_TENANT_CLINICA_SAN_ANGEL", "sk-tenant")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-global")
    # Específica del tenant gana sobre la global.
    assert resolve_tenant_openai_key(p, "clinica-san-angel") == "sk-tenant"
    # Sin específica, cae a la global.
    assert resolve_tenant_openai_key(p, "otro-negocio") == "sk-global"


async def test_env_secret_provider_missing_key_fails_clearly(monkeypatch):
    p = EnvSecretProvider()
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY_TENANT_DEMO", raising=False)
    assert resolve_tenant_openai_key(p, "demo") is None
    with pytest.raises(SecretNotFoundError, match="OPENAI_API_KEY_TENANT_DEMO"):
        require_tenant_openai_key(p, "demo")


async def test_vault_provider_is_documented_stub():
    with pytest.raises(NotImplementedError, match="stub documentado"):
        VaultSecretProvider()


# ── Factory por tenant ───────────────────────────────────────────

async def test_factory_commercial_tenant_gets_openai(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY_TENANT_CLINICA_TEST", "sk-tenant-1")
    async with db_mod.async_session_maker() as s:
        t = await _make_tenant(
            s, routing={"llm_provider": "openai", "llm_model": "gpt-4o-mini",
                        "llm_temperature": 0.3, "llm_max_tokens": 400}
        )
        await s.commit()
        llm = await build_llm_for_tenant(s, t.id)
    assert isinstance(llm, OpenAILLM)
    assert llm.model == "gpt-4o-mini"
    assert llm.temperature == 0.3
    assert llm.max_tokens == 400
    assert llm.api_key == "sk-tenant-1"


async def test_factory_dev_tenant_gets_ollama():
    async with db_mod.async_session_maker() as s:
        t = await _make_tenant(s, routing={"llm_provider": "ollama"})
        await s.commit()
        llm = await build_llm_for_tenant(s, t.id)
    assert isinstance(llm, OllamaLLM)


async def test_factory_openai_without_key_fails_fast(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY_TENANT_CLINICA_TEST", raising=False)
    async with db_mod.async_session_maker() as s:
        t = await _make_tenant(s)  # default comercial: openai
        await s.commit()
        with pytest.raises(RuntimeError, match="clinica-test"):
            await build_llm_for_tenant(s, t.id)


async def test_build_embedder_for_tenant_defaults_and_config():
    assert isinstance(
        build_embedder_for_tenant({}), FakeEmbedder
    )  # default: cero costo
    assert isinstance(
        build_embedder_for_tenant({"embedder": "ollama"}), OllamaEmbedder
    )
    with pytest.raises(RuntimeError, match="desconocido"):
        build_embedder_for_tenant({"embedder": "wat"})


# ── Costeo ───────────────────────────────────────────────────────

async def test_cost_usd_per_model():
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}
    assert cost_usd("gpt-4o-mini", usage) == pytest.approx(0.75)
    assert cost_usd("gpt-4o", usage) == pytest.approx(12.50)
    assert cost_usd("gpt-4o-mini", {"prompt_tokens": 100,
                                    "completion_tokens": 50}) == pytest.approx(
        100 / 1e6 * 0.15 + 50 / 1e6 * 0.60
    )
    # Modelo local/desconocido: costo 0, no excepción.
    assert cost_usd("llama3.2", usage) == 0.0
    assert cost_usd(None, usage) == 0.0
    assert cost_usd("gpt-4o-mini", {}) == 0.0


async def test_record_turn_usage_writes_row():
    async with db_mod.async_session_maker() as s:
        t = await _make_tenant(s)
        c = await _make_contact(s, t.id)
        rec = await record_turn_usage(
            s, t.id, c.id, None, "gpt-4o-mini",
            {"prompt_tokens": 1000, "completion_tokens": 500},
        )
        await s.commit()
        row = (await s.execute(
            select(UsageRecord).where(UsageRecord.id == rec.id)
        )).scalar_one()
        assert row.tokens_in == 1000
        assert row.tokens_out == 500
        assert float(row.cost_usd) == pytest.approx(
            1000 / 1e6 * 0.15 + 500 / 1e6 * 0.60
        )
        assert row.model == "gpt-4o-mini"
        assert row.tenant_id == t.id


async def test_aggregate_monthly_usage_sums_and_is_idempotent():
    async with db_mod.async_session_maker() as s:
        t1 = await _make_tenant(s, slug="t1")
        t2 = await _make_tenant(s, slug="t2")
        c1 = await _make_contact(s, t1.id, "5215550000001")
        c2 = await _make_contact(s, t2.id, "5215550000002")
        await record_turn_usage(s, t1.id, c1.id, None, "gpt-4o-mini",
                                {"prompt_tokens": 1000, "completion_tokens": 100})
        await record_turn_usage(s, t1.id, c1.id, None, "gpt-4o-mini",
                                {"prompt_tokens": 2000, "completion_tokens": 200})
        await record_turn_usage(s, t2.id, c2.id, None, "gpt-4o",
                                {"prompt_tokens": 500, "completion_tokens": 50})
        await s.commit()

        n = await aggregate_monthly_usage(s)
        await s.commit()
        assert n == 2

        rows = {
            r.tenant_id: r
            for r in (await s.execute(select(UsageMonthly))).scalars().all()
        }
        assert rows[t1.id].tokens_in == 3000
        assert rows[t1.id].tokens_out == 300
        assert rows[t2.id].tokens_in == 500

        # Re-ejecutar no duplica: recalcula los mismos totales.
        n2 = await aggregate_monthly_usage(s)
        await s.commit()
        assert n2 == 2
        count = (await s.execute(
            select(func.count()).select_from(UsageMonthly)
        )).scalar()
        assert count == 2
        rows2 = {
            r.tenant_id: r
            for r in (await s.execute(select(UsageMonthly))).scalars().all()
        }
        assert rows2[t1.id].tokens_in == 3000


# ── Engine E2E: un turno con usage deja su registro ───────────────

class _UsageStubLLM:
    model = "gpt-4o-mini"

    async def chat(self, messages, tools=None, tool_choice=None):
        return LLMResponse(
            content="Claro, te ayudo con tu cita.",
            finish_reason="stop",
            usage={"prompt_tokens": 200, "completion_tokens": 40,
                   "total_tokens": 240},
        )


async def test_engine_records_usage_per_turn():
    async with db_mod.async_session_maker() as s:
        t = await _make_tenant(s)
        c = await _make_contact(s, t.id)
        await s.commit()

        reply = await run_agent(
            s, _UsageStubLLM(), t.id, c.id, "hola",
            embedder=FakeEmbedder(),
        )
        assert "cita" in reply

        row = (await s.execute(select(UsageRecord))).scalar_one()
        assert row.tenant_id == t.id
        assert row.contact_id == c.id
        assert row.model == "gpt-4o-mini"
        assert row.tokens_in == 200
        assert row.tokens_out == 40
        assert float(row.cost_usd) == pytest.approx(
            200 / 1e6 * 0.15 + 40 / 1e6 * 0.60
        )


class _BrokenCostingStubLLM:
    """Simula un LLM cuyo usage rompería el registro (tokens no numéricos).

    El engine debe loguear y seguir: la respuesta llega igual y no hay
    usage_records.
    """
    model = "gpt-4o-mini"

    async def chat(self, messages, tools=None, tool_choice=None):
        return LLMResponse(
            content="respuesta ok",
            finish_reason="stop",
            usage={"prompt_tokens": "no-numérico", "completion_tokens": None},
        )


async def test_engine_usage_failure_does_not_break_flow():
    async with db_mod.async_session_maker() as s:
        t = await _make_tenant(s)
        c = await _make_contact(s, t.id)
        await s.commit()

        reply = await run_agent(
            s, _BrokenCostingStubLLM(), t.id, c.id, "hola",
            embedder=FakeEmbedder(),
        )
        assert reply == "respuesta ok"
        n = (await s.execute(
            select(func.count()).select_from(UsageRecord)
        )).scalar()
        assert n == 0  # falló el registro, pero el flujo siguió


# ── Knowledge: sin flag use_openai; el tenant decide ──────────────

async def test_knowledge_schema_has_no_use_openai_flag():
    assert "use_openai" not in KnowledgeIngest.model_fields
    # Campos extra se ignoran (pydantic default), no rompen el contrato.
    body = KnowledgeIngest(title="T", content="C", use_openai=True)
    assert body.title == "T"


async def test_knowledge_endpoint_uses_tenant_config_embedder():
    async with db_mod.async_session_maker() as s:
        t = await _make_tenant(s)
        await s.commit()
        tid = t.id

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://t") as c:
        # Sin config de embedder -> default fake (cero costo), funciona.
        r = await c.post(f"/tenants/{tid}/knowledge",
                         json={"title": "Precios",
                               "content": "Consulta 800 pesos."})
        assert r.status_code == 200
        assert r.json()["status"] == "ready"


async def test_knowledge_endpoint_embedder_openai_without_key_is_400(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    async with db_mod.async_session_maker() as s:
        t = await _make_tenant(s, routing={"embedder": "openai"})
        await s.commit()
        tid = t.id

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://t") as c:
        r = await c.post(f"/tenants/{tid}/knowledge",
                         json={"title": "T", "content": "C"})
        # La config del tenant pide OpenAI pero no hay key: error claro,
        # el cliente no puede elegir el embedder.
        assert r.status_code == 400


# ── EMBED_DIM: validación al arranque ─────────────────────────────

async def test_validate_embed_dim_ok():
    async with db_mod.async_session_maker() as s:
        assert await validate_embed_dim(s) == EMBED_DIM == 1536


async def test_validate_embed_dim_mismatch_fails_clearly(monkeypatch):
    monkeypatch.setattr("app.agent.embedder.EMBED_DIM", 999)
    async with db_mod.async_session_maker() as s:
        with pytest.raises(RuntimeError, match="MISMATCH.*999.*1536"):
            await validate_embed_dim(s)
