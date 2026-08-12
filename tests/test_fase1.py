"""Tests de Fase 1: RAG (ingesta + búsqueda), agent loop y endpoint de ingesta.

Usan FakeEmbedder (bag-of-words, determinístico, sin red/API). El engine de
test se configura en conftest. El umbral semántico de producción (0.75) está
pensado para OpenAI real; aquí validamos el RANKING y el AISLAMIENTO multi-tenant
usando threshold=0.0 (el FakeEmbedder no es semántico).
"""
import os

import httpx
import pytest
import pytest_asyncio

from app.agent.embedder import FakeEmbedder
from app.agent.rag import ingest_knowledge, search_knowledge
from app.core.base import Base
from app.core import db as db_mod
from app.main import app
from app.models import Contact, Tenant, TenantConfig, WhatsappChannel


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


@pytest_asyncio.fixture
async def tenant():
    async with db_mod.async_session_maker() as s:
        t = Tenant(slug="academia-test", name="Academia Test", business_type="academy")
        s.add(t)
        await s.flush()
        s.add(TenantConfig(tenant_id=t.id, system_prompt="Eres Liah, asistente."))
        s.add(WhatsappChannel(tenant_id=t.id, phone_number_id="123456789",
                              verify_token="test_token_123"))
        await s.commit()
        return t.id


@pytest.mark.asyncio
async def test_search_ranks_relevant_chunk(tenant):
    embedder = FakeEmbedder()
    text = (
        "Clases de ballet los martes y jueves a las 17:00. "
        "Clases de salsa los lunes a las 19:00. "
        "El costo mensual es de 800 pesos."
    )
    async with db_mod.async_session_maker() as s:
        await ingest_knowledge(s, tenant, "Horarios", text, embedder)
        # threshold=0.0: validamos ranking, no semántica (FakeEmbedder no es semántico)
        hits = await search_knowledge(s, tenant, "costo mensual 800 pesos", embedder, threshold=0.0)
    assert len(hits) >= 1
    assert "800" in hits[0]["content"]
    # el chunk relevante (que contiene 800) debe ranks primero
    assert "800" in hits[0]["content"]


@pytest.mark.asyncio
async def test_search_isolates_tenant(tenant):
    """Un tenant NO debe ver los chunks del otro AUN con tokens compartidos.

    El tenant 'otro' ingesta texto con los MISMOS tokens que la query; el
    tenant bajo prueba no tiene esos chunks. El filtro duro por tenant_id debe
    excluirlos. Esto prueba aislamiento, no solo ranking.
    """
    embedder = FakeEmbedder()
    other = await _make_tenant("otro-negocio")
    async with db_mod.async_session_maker() as s:
        await ingest_knowledge(
            s, other, "Horarios Otro",
            "Clases de ballet martes y jueves 17:00. Costo 800 pesos mensual.",
            embedder,
        )
        hits = await search_knowledge(
            s, tenant, "clases ballet martes jueves costo 800 pesos", embedder, threshold=0.0
        )
    assert hits == [], "el tenant no debe ver chunks del otro tenant"
    # sanity: el otro tenant SÍ los ve (confirma que no es un fallo de ranking)
    async with db_mod.async_session_maker() as s:
        own = await search_knowledge(
            s, other, "clases ballet martes jueves costo 800 pesos", embedder, threshold=0.0
        )
    assert len(own) == 1 and "800" in own[0]["content"]


@pytest.mark.asyncio
async def test_knowledge_endpoint_ingests(tenant):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post(
            f"/tenants/{tenant}/knowledge",
            json={"title": "Precios", "content": "Mensualidad 800 pesos."},
        )
    assert r.status_code == 200
    assert r.json()["status"] == "ready"

    async with db_mod.async_session_maker() as s:
        hits = await search_knowledge(s, tenant, "800 pesos", FakeEmbedder(), threshold=0.0)
    assert any("800" in h["content"] for h in hits)


@pytest.mark.asyncio
async def test_agent_loop_answers_from_rag(tenant):
    from app.agent.engine import run_agent
    from app.agent.embedder import FakeEmbedder
    from app.agent.ports import LLMResponse
    from app.agent.rag import search_knowledge
    from app.models import Contact

    async with db_mod.async_session_maker() as s:
        await ingest_knowledge(
            s, tenant, "Horarios",
            "Clases de ballet martes y jueves 17:00. Costo 800 pesos mensual.",
            FakeEmbedder(),
        )
        contact = Contact(tenant_id=tenant, wa_id="5215550009999")
        s.add(contact)
        await s.flush()
        cid = contact.id

    class StubLLM:
        session = None
        tenant_id = None

        async def chat(self, messages, tools=None, tool_choice=None):
            q = messages[-1]["content"]
            # threshold=0.0 para el stub de test (igual que producción usaría su umbral)
            hits = await search_knowledge(self.session, self.tenant_id, q, FakeEmbedder(), threshold=0.0)
            body = hits[0]["content"] if hits else "No sé."
            return LLMResponse(content=body, finish_reason="stop")

    async with db_mod.async_session_maker() as s:
        reply = await run_agent(s, StubLLM(), tenant, cid, "costo 800 pesos mensual")
    assert "800" in reply


async def _make_tenant(slug: str) -> object:
    async with db_mod.async_session_maker() as s:
        t = Tenant(slug=slug, name=slug, business_type="other")
        s.add(t)
        await s.flush()
        tid = t.id
        await s.commit()
        return tid
