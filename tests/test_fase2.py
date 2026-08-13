"""Tests de Fase 2: Motor de Recordatorios HSM (dispatch + idempotencia + consentimiento).

Requieren Postgres + pgvector. El engine de test se configura en conftest.
Usa dry_run=True (no consume Meta API). Valida la lógica de negocio:
- gate de consentimiento (LFPDPPP)
- idempotencia vía reminder_log
- armado de variables desde la BD (fuente de verdad)
"""
import os
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.agent.sender import send_template
from app.core.base import Base
from app.core import db as db_mod
from app.models import (
    Appointment,
    AutomationRule,
    Contact,
    Message,
    ReminderLog,
    Template,
    Tenant,
    TenantConfig,
    WhatsappChannel,
)
from app.reminders import dispatch


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


async def _make_tenant(slug="academia"):
    async with db_mod.async_session_maker() as s:
        t = Tenant(slug=slug, name="Academia Demo", business_type="academy")
        s.add(t)
        await s.flush()
        s.add(TenantConfig(tenant_id=t.id, system_prompt="x"))
        s.add(WhatsappChannel(tenant_id=t.id, phone_number_id="123456789",
                              verify_token="tok", token_secret_ref="TEST"))
        await s.commit()
        return t.id


async def _make_template(s, tenant_id, name):
    tpl = Template(
        tenant_id=tenant_id, name=name, category="utility", language="es",
        body="Hola {{1}} de {{2}}", variables=["name", "tenant"],
        status="approved",
    )
    s.add(tpl)
    await s.commit()
    await s.refresh(tpl)
    return tpl


@pytest.mark.asyncio
async def test_dispatch_blocks_without_consent():
    tid = await _make_tenant()
    async with db_mod.async_session_maker() as s:
        contact = Contact(tenant_id=tid, wa_id="5215550001111",
                         name="Maria", consent_status="none")
        s.add(contact)
        await s.commit()
        await s.refresh(contact)
        tpl = await _make_template(s, tid, "recordatorio_clase_muestra")
        rule = AutomationRule(tenant_id=tid, type="trial_class", enabled=True)
        s.add(rule)
        await s.commit()
        await s.refresh(rule)

        ok = await dispatch.dispatch_reminder(
            s, tid, "Academia Demo", rule, contact, tpl,
            datetime.utcnow(), ["Maria", "Academia Demo"], dry_run=True,
        )
        assert ok is False
        n = (await s.execute(select(func.count()).select_from(ReminderLog))).scalar()
        assert n == 0


@pytest.mark.asyncio
async def test_dispatch_sends_with_consent_and_logs():
    tid = await _make_tenant()
    async with db_mod.async_session_maker() as s:
        contact = Contact(tenant_id=tid, wa_id="5215550002222",
                         name="Maria", consent_status="granted")
        s.add(contact)
        await s.commit()
        await s.refresh(contact)
        tpl = await _make_template(s, tid, "recordatorio_clase_muestra")
        rule = AutomationRule(tenant_id=tid, type="trial_class", enabled=True)
        s.add(rule)
        await s.commit()
        await s.refresh(rule)

        ok = await dispatch.dispatch_reminder(
            s, tid, "Academia Demo", rule, contact, tpl,
            datetime.utcnow(), ["Maria", "Academia Demo"], dry_run=True,
        )
        assert ok is True
        logs = (await s.execute(select(ReminderLog))).scalars().all()
        assert len(logs) == 1
        assert logs[0].status == "sent"


@pytest.mark.asyncio
async def test_dispatch_is_idempotent():
    tid = await _make_tenant()
    sch = datetime.utcnow()
    async with db_mod.async_session_maker() as s:
        contact = Contact(tenant_id=tid, wa_id="5215550003333",
                         name="Maria", consent_status="granted")
        s.add(contact)
        await s.commit()
        await s.refresh(contact)
        tpl = await _make_template(s, tid, "recordatorio_clase_muestra")
        rule = AutomationRule(tenant_id=tid, type="trial_class", enabled=True)
        s.add(rule)
        await s.commit()
        await s.refresh(rule)

        ok1 = await dispatch.dispatch_reminder(
            s, tid, "Academia Demo", rule, contact, tpl, sch,
            ["Maria", "Academia Demo"], dry_run=True,
        )
        ok2 = await dispatch.dispatch_reminder(
            s, tid, "Academia Demo", rule, contact, tpl, sch,
            ["Maria", "Academia Demo"], dry_run=True,
        )
        assert ok1 is True and ok2 is False
        logs = (await s.execute(select(ReminderLog))).scalars().all()
        assert len(logs) == 1


@pytest.mark.asyncio
async def test_load_targets_trial_class_day_before():
    tid = await _make_tenant()
    tomorrow = datetime.utcnow() + timedelta(days=1)
    async with db_mod.async_session_maker() as s:
        contact = Contact(tenant_id=tid, wa_id="5215550004444",
                         name="Sofia", consent_status="granted")
        s.add(contact)
        await s.commit()
        await s.refresh(contact)
        appt = Appointment(tenant_id=tid, contact_id=contact.id,
                           type="trial_class", start_at=tomorrow)
        s.add(appt)
        s.add(AutomationRule(tenant_id=tid, type="trial_class", enabled=True))
        await s.commit()

        targets = await dispatch.load_rule_targets(
            s, tid, "Academia Demo", "trial_class", datetime.utcnow()
        )
        assert len(targets) == 1
        (r, c, scheduled_for, vars_) = targets[0]
        assert c.id == contact.id
        assert vars_[0] == "Sofia"


@pytest.mark.asyncio
async def test_load_targets_colegiatura_days_1_10():
    tid = await _make_tenant()
    async with db_mod.async_session_maker() as s:
        contact = Contact(tenant_id=tid, wa_id="5215550005555",
                         name="Pedro", consent_status="granted")
        s.add(contact)
        await s.commit()
        await s.refresh(contact)
        s.add(AutomationRule(tenant_id=tid, type="colegiatura", enabled=True))
        await s.commit()

        now = datetime.utcnow().replace(day=5)
        targets = await dispatch.load_rule_targets(
            s, tid, "Academia Demo", "colegiatura", now
        )
        assert len(targets) == 1
        now20 = datetime.utcnow().replace(day=20)
        targets20 = await dispatch.load_rule_targets(
            s, tid, "Academia Demo", "colegiatura", now20
        )
        assert targets20 == []


@pytest.mark.asyncio
async def test_send_template_dry_run_returns_none():
    tid = await _make_tenant()
    async with db_mod.async_session_maker() as s:
        contact = Contact(tenant_id=tid, wa_id="5215550006666",
                         name="X", consent_status="granted")
        s.add(contact)
        await s.commit()
        await s.refresh(contact)
        meta = await send_template(
            s, str(tid), str(contact.id), contact.wa_id,
            "recordatorio_clase_muestra", "es",
            [{"type": "body", "parameters": [{"type": "text", "text": "X"}]}],
            dry_run=True,
        )
        assert meta is None
        msgs = (await s.execute(select(func.count()).select_from(Message))).scalar()
        assert msgs >= 1
