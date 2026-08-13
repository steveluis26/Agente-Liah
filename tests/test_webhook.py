"""Tests de Fase 0 para el webhook de WhatsApp (Fase 1 mantiene compatibilidad).

Requieren Postgres + pgvector. La BD de test y el engine de la app se
configuran en tests/conftest.py (pytest_configure).
"""
import hashlib
import hmac
import json
import os

import asyncpg
import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from app.core.base import Base  # asegura import de modelos
from app.core.config import get_settings
from app.core import db as db_mod
from app.main import app


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


@pytest_asyncio.fixture
async def seeded_tenant():
    from app.models import Tenant, TenantConfig, WhatsappChannel

    async with db_mod.async_session_maker() as s:
        t = Tenant(slug="academia-danza-demo", name="Academia Demo", business_type="academy")
        s.add(t)
        await s.flush()
        s.add(TenantConfig(tenant_id=t.id, system_prompt="x"))
        s.add(WhatsappChannel(tenant_id=t.id, phone_number_id="123456789",
                              verify_token="test_token_123"))
        await s.commit()
        return t.id


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.mark.asyncio
async def test_get_verify_returns_challenge():
    params = {"hub.mode": "subscribe", "hub.verify_token": "test_token_123",
              "hub.challenge": "CHALLENGE_42"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/webhook/whatsapp", params=params)
    assert r.status_code == 200 and r.text == "CHALLENGE_42"


@pytest.mark.asyncio
async def test_get_verify_wrong_token_forbidden():
    params = {"hub.mode": "subscribe", "hub.verify_token": "wrong",
              "hub.challenge": "CHALLENGE_42"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/webhook/whatsapp", params=params)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_post_persists_message_with_valid_signature(seeded_tenant):
    secret = get_settings().whatsapp_app_secret
    payload = {
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA1", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": "+5215555550000",
                         "phone_number_id": "123456789"},
            "messages": [{"from": "5215550001111", "id": "wamid.TEST1",
                          "type": "text", "text": {"body": "Hola, quiero horarios"},
                          "timestamp": "1690000000"}]}}]}],
    }
    body = json.dumps(payload).encode()
    headers = {"X-Hub-Signature-256": _sign(body, secret)}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/webhook/whatsapp", content=body, headers=headers)
    assert r.status_code == 200

    from app.models import Contact, Message

    async with db_mod.async_session_maker() as s:
        contact = (await s.execute(select(Contact).where(
            Contact.tenant_id == seeded_tenant,
            Contact.wa_id == "5215550001111"))).scalar_one()
        assert contact.wa_id == "5215550001111"
        msg = (await s.execute(select(Message).where(
            Message.contact_id == contact.id,
            Message.direction == "inbound"))).scalar_one()
        assert msg.content == "Hola, quiero horarios"
        assert msg.direction == "inbound"
        assert msg.tenant_id == seeded_tenant


@pytest.mark.asyncio
async def test_post_invalid_signature_forbidden():
    body = b'{"object":"whatsapp_business_account","entry":[]}'
    headers = {"X-Hub-Signature-256": "sha256=invalid"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/webhook/whatsapp", content=body, headers=headers)
    assert r.status_code == 403
