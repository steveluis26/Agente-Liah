"""Tests de Fase 0 para el webhook de WhatsApp.

Requieren una base Postgres con pgvector accesible vía TEST_DATABASE_URL.
En CI local (Mac) se levanta con: docker compose -f infra/docker-compose.yml up -d
"""
import hashlib
import hmac
import json
import os

import asyncpg
import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.base import Base  # asegura import de modelos
from app.main import app

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://pyme:pyme@localhost:5432/pyme_agent_test",
)

# Reconfigura el engine de la app para los tests (NullPool evita el error
# "another operation is in progress" de asyncpg al reusar conexiones).
import app.core.db as db_mod

test_engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
db_mod.engine = test_engine
db_mod.async_session_maker = async_sessionmaker(test_engine, expire_on_commit=False)


@pytest_asyncio.fixture(autouse=True)
async def _schema():
    # Extensiones con asyncpg standalone (conexión propia, autocommit natural).
    raw_url = TEST_DATABASE_URL.replace("+asyncpg", "")
    conn = await asyncpg.connect(raw_url)
    try:
        await conn.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp";')
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    finally:
        await conn.close()

    # drop/create vía begin() (NullPool evita el conflicto de asyncpg).
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def seeded_tenant():
    from app.models import Tenant, TenantConfig, WhatsappChannel

    async with db_mod.async_session_maker() as session:
        tenant = Tenant(slug="academia-danza-demo", name="Academia Demo", business_type="academy")
        session.add(tenant)
        await session.flush()
        session.add(TenantConfig(tenant_id=tenant.id, system_prompt="x"))
        session.add(
            WhatsappChannel(
                tenant_id=tenant.id,
                phone_number_id="123456789",
                verify_token="test_token_123",
            )
        )
        await session.commit()
        return tenant.id


def _sign(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


@pytest.mark.asyncio
async def test_get_verify_returns_challenge():
    params = {
        "hub.mode": "subscribe",
        "hub.verify_token": "test_token_123",
        "hub.challenge": "CHALLENGE_42",
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/webhook/whatsapp", params=params)
    assert r.status_code == 200
    assert r.text == "CHALLENGE_42"


@pytest.mark.asyncio
async def test_get_verify_wrong_token_forbidden():
    params = {
        "hub.mode": "subscribe",
        "hub.verify_token": "wrong",
        "hub.challenge": "CHALLENGE_42",
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/webhook/whatsapp", params=params)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_post_persists_message_with_valid_signature(seeded_tenant):
    secret = "test_app_secret"

    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "+5215555550000",
                                "phone_number_id": "123456789",
                            },
                            "messages": [
                                {
                                    "from": "5215550001111",
                                    "id": "wamid.TEST1",
                                    "type": "text",
                                    "text": {"body": "Hola, quiero horarios"},
                                    "timestamp": "1690000000",
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }
    body = json.dumps(payload).encode()
    headers = {"X-Hub-Signature-256": _sign(body, secret)}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/webhook/whatsapp", content=body, headers=headers)

    assert r.status_code == 200

    from app.models import Contact, Message

    async with db_mod.async_session_maker() as session:
        contact = (
            await session.execute(
                select(Contact).where(Contact.tenant_id == seeded_tenant)
            )
        ).scalar_one()
        assert contact.wa_id == "5215550001111"
        msg = (
            await session.execute(
                select(Message).where(Message.contact_id == contact.id)
            )
        ).scalar_one()
        assert msg.content == "Hola, quiero horarios"
        assert msg.direction == "inbound"
        assert msg.tenant_id == seeded_tenant


@pytest.mark.asyncio
async def test_post_invalid_signature_forbidden():
    body = b'{"object":"whatsapp_business_account","entry":[]}'
    headers = {"X-Hub-Signature-256": "sha256=invalid"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/webhook/whatsapp", content=body, headers=headers)
    assert r.status_code == 403
