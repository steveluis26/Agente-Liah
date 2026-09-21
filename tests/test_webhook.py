"""Tests del webhook de WhatsApp (Fase 1: ack inmediato + cola persistente).

El POST responde 200 de inmediato y encola en `webhook_jobs`; los tests
drenan la cola explícitamente con `drain_jobs()` para validar el proceso
diferido (idempotencia, tipos no-texto, statuses, canal desconocido).

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
from sqlalchemy import func, select

from app.channels.whatsapp.queue import drain_jobs
from app.core.base import Base  # asegura import de modelos
from app.core.config import get_settings
from app.core import db as db_mod
from app.main import app
from app.models import Contact, EventLog, Message, WebhookJob


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


def _message(wa_from="5215550001111", wamid="wamid.TEST1", msg_type="text",
             body="Hola, quiero horarios"):
    msg = {"from": wa_from, "id": wamid, "type": msg_type,
           "timestamp": "1690000000"}
    if msg_type == "text":
        msg["text"] = {"body": body}
    else:
        msg["image"] = {"id": "img_1", "mime_type": "image/jpeg"}
    return msg


def _payload(phone_number_id="123456789", messages=None, statuses=None):
    value = {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "+5215555550000",
                     "phone_number_id": phone_number_id},
    }
    if messages is not None:
        value["messages"] = messages
    if statuses is not None:
        value["statuses"] = statuses
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA1",
                   "changes": [{"field": "messages", "value": value}]}],
    }


async def _post(payload: dict):
    secret = get_settings().whatsapp_app_secret
    body = json.dumps(payload).encode()
    headers = {"X-Hub-Signature-256": _sign(body, secret)}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.post("/webhook/whatsapp", content=body, headers=headers)


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
async def test_post_acks_immediately_and_drains(seeded_tenant):
    # El POST solo encola: responde 200 sin haber procesado aún.
    r = await _post(_payload(messages=[_message()]))
    assert r.status_code == 200
    async with db_mod.async_session_maker() as s:
        n_jobs = (await s.execute(
            select(func.count()).select_from(WebhookJob))).scalar()
        n_msgs = (await s.execute(
            select(func.count()).select_from(Message))).scalar()
    assert n_jobs == 1 and n_msgs == 0

    # El drenado procesa el trabajo pendiente.
    stats = await drain_jobs(db_mod.async_session_maker)
    assert stats["done"] == 1

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
        assert msg.meta_message_id == "wamid.TEST1"


@pytest.mark.asyncio
async def test_post_invalid_signature_forbidden():
    body = b'{"object":"whatsapp_business_account","entry":[]}'
    headers = {"X-Hub-Signature-256": "sha256=invalid"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/webhook/whatsapp", content=body, headers=headers)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_duplicate_wamid_is_idempotent(seeded_tenant):
    """El retry de Meta con el mismo wamid no duplica mensajes ni respuestas."""
    payload = _payload(messages=[_message(wamid="wamid.DUP1")])
    r1 = await _post(payload)
    r2 = await _post(payload)
    assert r1.status_code == 200 and r2.status_code == 200

    stats = await drain_jobs(db_mod.async_session_maker)
    assert stats["done"] == 2  # dos trabajos encolados...

    async with db_mod.async_session_maker() as s:
        n_inbound = (await s.execute(
            select(func.count()).select_from(Message).where(
                Message.direction == "inbound",
                Message.meta_message_id == "wamid.DUP1"))).scalar()
        assert n_inbound == 1  # ...pero un solo mensaje persistido
        n_contacts = (await s.execute(
            select(func.count()).select_from(Contact).where(
                Contact.wa_id == "5215550001111"))).scalar()
        assert n_contacts == 1


@pytest.mark.asyncio
async def test_unknown_phone_number_id_rejected_safely(seeded_tenant):
    """phone_number_id sin canal: 200 (para que Meta no reintente) sin procesar."""
    r = await _post(_payload(phone_number_id="000000000",
                             messages=[_message()]))
    assert r.status_code == 200
    stats = await drain_jobs(db_mod.async_session_maker)
    assert stats["processed"] == 0
    async with db_mod.async_session_maker() as s:
        n_contacts = (await s.execute(
            select(func.count()).select_from(Contact))).scalar()
        n_jobs = (await s.execute(
            select(func.count()).select_from(WebhookJob))).scalar()
    assert n_contacts == 0 and n_jobs == 0


@pytest.mark.asyncio
async def test_non_text_message_does_not_crash(seeded_tenant):
    """Mensaje tipo imagen: no crashea, no inserta vacíos, se audita."""
    r = await _post(_payload(messages=[_message(wamid="wamid.IMG1",
                                                msg_type="image")]))
    assert r.status_code == 200
    stats = await drain_jobs(db_mod.async_session_maker)
    assert stats["done"] == 1
    async with db_mod.async_session_maker() as s:
        n_inbound = (await s.execute(
            select(func.count()).select_from(Message).where(
                Message.direction == "inbound"))).scalar()
        assert n_inbound == 0  # nada vacío insertado
        empties = (await s.execute(
            select(func.count()).select_from(Message).where(
                Message.content == ""))).scalar()
        assert empties == 0
        ev = (await s.execute(
            select(EventLog).where(EventLog.type == "message.unsupported")
        )).scalars().all()
        assert len(ev) == 1 and ev[0].payload["msg_type"] == "image"


@pytest.mark.asyncio
async def test_statuses_do_not_generate_work(seeded_tenant):
    """Los statuses de Meta solo se auditan: no encolan ni responden."""
    statuses = [{"id": "wamid.X", "status": "delivered",
                 "timestamp": "1690000001", "recipient_id": "5215550001111"}]
    r = await _post(_payload(statuses=statuses))
    assert r.status_code == 200
    stats = await drain_jobs(db_mod.async_session_maker)
    assert stats["processed"] == 0
    async with db_mod.async_session_maker() as s:
        n_jobs = (await s.execute(
            select(func.count()).select_from(WebhookJob))).scalar()
        n_msgs = (await s.execute(
            select(func.count()).select_from(Message))).scalar()
        ev = (await s.execute(
            select(EventLog).where(EventLog.type == "webhook.statuses")
        )).scalars().all()
    assert n_jobs == 0 and n_msgs == 0
    assert len(ev) == 1 and ev[0].payload["count"] == 1


# ── Contrato ChannelAdapter ──────────────────────────
def test_whatsapp_adapter_parses_text_and_unsupported():
    from app.channels.adapter import TEXT, UNSUPPORTED, get_adapter

    adapter = get_adapter("whatsapp")
    events = adapter.parse_events({
        "contacts": [{"profile": {"name": "Ana"}}],
        "messages": [
            {"from": "5211", "id": "w1", "type": "text",
             "text": {"body": "Hola"}},
            {"from": "5211", "id": "w2", "type": "image",
             "image": {"id": "img"}},
            {"from": "", "id": "w3", "type": "text",  # sin remitente: se descarta
             "text": {"body": "x"}},
            {"from": "5211", "id": "w4", "type": "text",
             "text": {"body": "   "}},  # vacío: unsupported
        ],
    })
    assert len(events) == 3
    assert events[0].kind == TEXT and events[0].text == "Hola"
    assert events[0].sender_external_id == "5211"
    assert events[0].sender_name == "Ana"
    assert events[1].kind == UNSUPPORTED and events[1].text == ""
    assert events[2].kind == UNSUPPORTED


def test_unknown_channel_raises():
    from app.channels.adapter import get_adapter

    with __import__("pytest").raises(KeyError):
        get_adapter("instagram")
