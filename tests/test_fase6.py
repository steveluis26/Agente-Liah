"""Tests de Fase 6: módulo de campañas y avisos.

Requieren Postgres + pgvector. El engine de test se configura en conftest.
dry_run=True en los envíos (no consume Meta API). Valida:
- opt-in/opt-out por palabra clave (regla simple, configurable, con
  límites de palabra) y su hook en el drenador;
- launch solo a opted-in del segmento; opt-out posterior al launch bloquea;
- re-dispatch no duplica (unique + idempotencia);
- plantilla no aprobada -> 422 sin salir de draft;
- statuses delivered/read por wamid actualizan campaign_sends;
- métricas cuadran y el costo se registra en usage_records;
- panel: crear campaña sin ser platform_admin/tenant_admin -> 403.
"""
import os

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.channels.whatsapp.queue import drain_jobs, enqueue_job
from app.core import db as db_mod
from app.core.auth import create_access_token
from app.core.base import Base  # asegura import de modelos
from app.main import app
from app.marketing import campaigns as campaign_svc
from app.marketing.optin import (
    OPTIN_ACK,
    OPTOUT_ACK,
    classify,
    mark_contact_client,
    process_marketing_keyword,
    set_opt_in,
)
from app.models import (
    Campaign,
    CampaignSend,
    Contact,
    ContactTag,
    Message,
    PlatformUser,
    Template,
    Tenant,
    TenantConfig,
    UsageRecord,
    WhatsappChannel,
)
from app.models.platform_users import (
    ROLE_PLATFORM_ADMIN,
    ROLE_TENANT_ADMIN,
    ROLE_TENANT_AGENT,
    hash_password,
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


# ── Helpers ───────────────────────────────────────────────────────────


async def _make_tenant(slug="clinica", **extra):
    async with db_mod.async_session_maker() as s:
        t = Tenant(slug=slug, name=f"Tenant {slug}", business_type="consultorio")
        s.add(t)
        await s.flush()
        cfg_extra = dict(extra)
        s.add(TenantConfig(tenant_id=t.id, system_prompt="x", extra=cfg_extra))
        s.add(WhatsappChannel(tenant_id=t.id, phone_number_id=f"pn-{slug}",
                              verify_token="tok"))
        await s.commit()
        return t.id


async def _make_contact(tid, wa_id, *, opt_in=False, contact_type="prospect",
                       tags=()):
    async with db_mod.async_session_maker() as s:
        c = Contact(tenant_id=tid, wa_id=wa_id, name=f"Contacto {wa_id[-4:]}",
                    contact_type=contact_type, marketing_opt_in=opt_in,
                    marketing_opt_in_source="import" if opt_in else None)
        s.add(c)
        await s.flush()
        for tag in tags:
            s.add(ContactTag(tenant_id=tid, contact_id=c.id, tag=tag))
        await s.commit()
        return c.id


async def _make_template(tid, name="promo_mes", status="approved"):
    async with db_mod.async_session_maker() as s:
        tpl = Template(tenant_id=tid, name=name, category="marketing",
                       language="es", body="Hola {{1}}, promo {{2}}",
                       variables=["nombre", "promo"], status=status)
        s.add(tpl)
        await s.commit()
        return tpl.id


async def _mk_user(email, role, tenant_id=None, password="Secret-12345678"):
    async with db_mod.async_session_maker() as s:
        u = PlatformUser(email=email, password_hash=hash_password(password),
                         role=role, tenant_id=tenant_id)
        s.add(u)
        await s.commit()
        return u.id


def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API)


def _headers(uid, role, tenant_id=None):
    token = create_access_token(uid, role, tenant_id)
    return {"Authorization": f"Bearer {token}"}


async def _launch(tid, template_name="promo_mes", segment=None, name="C1",
                 type="promo"):
    async with db_mod.async_session_maker() as s:
        c = await campaign_svc.create_campaign(
            s, tid, name=name, type=type, template_name=template_name,
            params={"nombre": "Ana", "promo": "15%"}, segment=segment or {},
            created_by="test",
        )
        c = await campaign_svc.launch_campaign(s, tid, c.id, dry_run=True)
        cid = c.id
        await s.commit()
        return cid


# ── 1. Opt-in/out por palabra clave ───────────────────────────────────


def test_classify_optin_keywords():
    assert classify("Quiero recibir promociones") == "optin"
    assert classify("sí quiero promos!") == "optin"
    assert classify("QUIERO PROMOS") == "optin"
    assert classify("hola, ¿a qué hora abren?") is None
    assert classify("") is None


def test_classify_optout_keywords():
    assert classify("baja") == "optout"
    assert classify("BAJA") == "optout"
    assert classify("no me manden más mensajes") == "optout"
    assert classify("stop") == "optout"
    assert classify("dame de baja por favor") == "optout"


def test_classify_word_boundaries_no_false_positives():
    # "trabajan" contiene "baja" como subcadena: NO debe disparar opt-out.
    assert classify("¿ustedes trabajan los domingos?") is None
    # "estopa"/"stop" dentro de otra palabra: no dispara.
    assert classify("me encanta la estopa") is None
    # Pero "stop" solo sí dispara.
    assert classify("stop") == "optout"


def test_classify_optout_wins_tie():
    # "quiero darme de baja": matchea listas de opt-in y opt-out -> gana opt-out.
    assert classify("quiero darme de baja") == "optout"


def test_classify_configurable_per_tenant():
    assert classify("alta promos", optin_keywords=["alta promos"]) == "optin"
    assert classify("alta promos") is None  # no está en los defaults


@pytest.mark.asyncio
async def test_process_keyword_sets_optin_and_source():
    tid = await _make_tenant()
    cid = await _make_contact(tid, "5215550001001")
    async with db_mod.async_session_maker() as s:
        contact = await s.get(Contact, cid)
        verdict = await process_marketing_keyword(
            s, tid, contact, "Hola, quiero recibir promociones")
        assert verdict == "optin"
        await s.commit()
    async with db_mod.async_session_maker() as s:
        contact = await s.get(Contact, cid)
        assert contact.marketing_opt_in is True
        assert contact.marketing_opt_in_source == "keyword"
        assert contact.marketing_opt_in_at is not None


@pytest.mark.asyncio
async def test_process_keyword_optout_is_immediate():
    tid = await _make_tenant()
    cid = await _make_contact(tid, "5215550001002", opt_in=True)
    async with db_mod.async_session_maker() as s:
        contact = await s.get(Contact, cid)
        assert await process_marketing_keyword(s, tid, contact, "baja") == "optout"
        await s.commit()
    async with db_mod.async_session_maker() as s:
        contact = await s.get(Contact, cid)
        assert contact.marketing_opt_in is False
        assert contact.marketing_opt_in_source == "keyword"


@pytest.mark.asyncio
async def test_keyword_rule_in_drainer_skips_agent_with_canned_reply():
    """El drenador procesa 'baja' sin llamar al agente y confirma enlatado."""
    tid = await _make_tenant(slug="kw")
    cid = await _make_contact(tid, "5215550001003", opt_in=True)
    async with db_mod.async_session_maker() as s:
        await enqueue_job(s, tid, "pn-kw", {
            "messages": [{
                "from": "5215550001003", "id": "wamid.KW1", "type": "text",
                "text": {"body": "baja, no me manden más"},
            }],
            "contacts": [{"profile": {"name": "Juan"}}],
        })
        await s.commit()
    stats = await drain_jobs(db_mod.async_session_maker, dry_run=True)
    assert stats["done"] == 1
    async with db_mod.async_session_maker() as s:
        contact = await s.get(Contact, cid)
        assert contact.marketing_opt_in is False
        outbound = (await s.execute(
            select(Message).where(Message.contact_id == cid,
                                  Message.direction == "outbound")
        )).scalars().all()
        # Solo la confirmación enlatada de opt-out (el agente no respondió).
        assert len(outbound) == 1
        assert outbound[0].content == OPTOUT_ACK


@pytest.mark.asyncio
async def test_keyword_rule_in_drainer_optin_ack():
    tid = await _make_tenant(slug="kw2")
    cid = await _make_contact(tid, "5215550001004")
    async with db_mod.async_session_maker() as s:
        await enqueue_job(s, tid, "pn-kw2", {
            "messages": [{
                "from": "5215550001004", "id": "wamid.KW2", "type": "text",
                "text": {"body": "quiero recibir promociones"},
            }],
            "contacts": [{"profile": {"name": "Ana"}}],
        })
        await s.commit()
    await drain_jobs(db_mod.async_session_maker, dry_run=True)
    async with db_mod.async_session_maker() as s:
        contact = await s.get(Contact, cid)
        assert contact.marketing_opt_in is True
        outbound = (await s.execute(
            select(Message).where(Message.contact_id == cid,
                                  Message.direction == "outbound")
        )).scalars().all()
        assert len(outbound) == 1
        assert outbound[0].content == OPTIN_ACK


# ── 2. Launch solo a opted-in del segmento ─────────────────────────────


@pytest.mark.asyncio
async def test_launch_only_opted_in_segment():
    tid = await _make_tenant(campaign_msgs_per_sec=100)
    await _make_template(tid)
    a = await _make_contact(tid, "5215550002001", opt_in=True,
                            contact_type="client", tags=["vip"])
    b = await _make_contact(tid, "5215550002002", opt_in=False,
                            contact_type="client", tags=["vip"])
    c = await _make_contact(tid, "5215550002003", opt_in=True,
                            contact_type="prospect", tags=[])
    async with db_mod.async_session_maker() as s:
        est = await campaign_svc.estimate_recipients(
            s, tid, {"contact_type": "client", "tags": ["vip"]})
        assert est == 1
    cid = await _launch(tid, segment={"contact_type": "client",
                                      "tags": ["vip"]})
    async with db_mod.async_session_maker() as s:
        sends = (await s.execute(
            select(CampaignSend).where(CampaignSend.campaign_id == cid)
        )).scalars().all()
        assert len(sends) == 1
        assert sends[0].contact_id == a
        camp = await s.get(Campaign, cid)
        assert camp.status == "sending"
        assert camp.total_targets == 1
    # contact_id de b y c: nunca encolados
    async with db_mod.async_session_maker() as s:
        n = (await s.execute(
            select(func.count()).select_from(CampaignSend).where(
                CampaignSend.campaign_id == cid,
                CampaignSend.contact_id.in_([b, c])))).scalar()
        assert n == 0


@pytest.mark.asyncio
async def test_dispatch_sends_and_marks_done_with_cost():
    tid = await _make_tenant(campaign_msgs_per_sec=100)
    await _make_template(tid)
    cid_contact = await _make_contact(tid, "5215550002101", opt_in=True)
    cid = await _launch(tid)
    stats = await campaign_svc.dispatch_campaigns(
        db_mod.async_session_maker, dry_run=True)
    assert stats["sent"] == 1
    async with db_mod.async_session_maker() as s:
        send = (await s.execute(
            select(CampaignSend).where(CampaignSend.campaign_id == cid)
        )).scalar_one()
        assert send.status == "sent"
        camp = await s.get(Campaign, cid)
        assert camp.status == "done"
        # Costo registrado en usage_records (kind=campaign).
        cost = (await s.execute(
            select(UsageRecord).where(UsageRecord.campaign_id == cid)
        )).scalars().all()
        assert len(cost) == 1
        assert cost[0].kind == "campaign"
        assert float(cost[0].cost_usd) > 0
        assert cost[0].contact_id == cid_contact
        # El mensaje outbound se persistió (dry-run registra sin llamar a Meta).
        n_out = (await s.execute(
            select(func.count()).select_from(Message).where(
                Message.contact_id == cid_contact,
                Message.direction == "outbound"))).scalar()
        assert n_out == 1


@pytest.mark.asyncio
async def test_optout_after_launch_blocks_dispatch():
    """Opt-out posterior al lanzamiento excluye al contacto (filtro por envío)."""
    tid = await _make_tenant(campaign_msgs_per_sec=100)
    await _make_template(tid)
    cid_contact = await _make_contact(tid, "5215550002201", opt_in=True)
    cid = await _launch(tid)
    # El contacto se da de baja DESPUÉS del launch (el send ya está queued).
    async with db_mod.async_session_maker() as s:
        contact = await s.get(Contact, cid_contact)
        await set_opt_in(s, tid, contact, False, "keyword")
        await s.commit()
    stats = await campaign_svc.dispatch_campaigns(
        db_mod.async_session_maker, dry_run=True)
    assert stats["sent"] == 0
    async with db_mod.async_session_maker() as s:
        send = (await s.execute(
            select(CampaignSend).where(CampaignSend.campaign_id == cid)
        )).scalar_one()
        assert send.status == "failed"  # excluido, no enviado
        n_cost = (await s.execute(
            select(func.count()).select_from(UsageRecord).where(
                UsageRecord.campaign_id == cid))).scalar()
        assert n_cost == 0
        n_out = (await s.execute(
            select(func.count()).select_from(Message).where(
                Message.contact_id == cid_contact,
                Message.direction == "outbound"))).scalar()
        assert n_out == 0


@pytest.mark.asyncio
async def test_redispatch_does_not_duplicate():
    tid = await _make_tenant(campaign_msgs_per_sec=100)
    await _make_template(tid)
    cid_contact = await _make_contact(tid, "5215550002301", opt_in=True)
    cid = await _launch(tid)
    await campaign_svc.dispatch_campaigns(db_mod.async_session_maker,
                                          dry_run=True)
    # Re-correr el dispatch: no duplica nada.
    stats = await campaign_svc.dispatch_campaigns(db_mod.async_session_maker,
                                                  dry_run=True)
    assert stats["sent"] == 0 and stats["failed"] == 0
    async with db_mod.async_session_maker() as s:
        n_sends = (await s.execute(
            select(func.count()).select_from(CampaignSend).where(
                CampaignSend.campaign_id == cid))).scalar()
        n_cost = (await s.execute(
            select(func.count()).select_from(UsageRecord).where(
                UsageRecord.campaign_id == cid))).scalar()
        n_out = (await s.execute(
            select(func.count()).select_from(Message).where(
                Message.contact_id == cid_contact,
                Message.direction == "outbound"))).scalar()
        assert n_sends == 1 and n_cost == 1 and n_out == 1


# ── 3. Plantilla no aprobada -> 422 ───────────────────────────────────


@pytest.mark.asyncio
async def test_launch_unapproved_template_422_and_stays_draft():
    tid = await _make_tenant()
    admin = await _mk_user("pa@liah.local", ROLE_PLATFORM_ADMIN)
    await _make_template(tid, name="promo_sin_aprobar", status="pending")
    await _make_contact(tid, "5215550003001", opt_in=True)
    async with _client() as c:
        h = _headers(admin, ROLE_PLATFORM_ADMIN)
        r = await c.post(f"/api/v1/admin/tenants/{tid}/campaigns",
                         headers=h,
                         json={"name": "C sin aprobar", "type": "promo",
                               "template_name": "promo_sin_aprobar",
                               "params": {}, "segment": {}})
        assert r.status_code == 201, r.text
        cid = r.json()["id"]
        assert r.json()["status"] == "draft"
        r2 = await c.post(
            f"/api/v1/admin/tenants/{tid}/campaigns/{cid}/launch", headers=h)
        assert r2.status_code == 422, r2.text
        assert "aprobada" in r2.json()["detail"].lower()
        # La campaña NO salió de draft y no se encoló nada.
        r3 = await c.get(f"/api/v1/admin/tenants/{tid}/campaigns/{cid}",
                         headers=h)
        assert r3.json()["campaign"]["status"] == "draft"
    async with db_mod.async_session_maker() as s:
        n = (await s.execute(
            select(func.count()).select_from(CampaignSend))).scalar()
        assert n == 0


@pytest.mark.asyncio
async def test_launch_after_approval_succeeds():
    tid = await _make_tenant()
    admin = await _mk_user("pa2@liah.local", ROLE_PLATFORM_ADMIN)
    tpl_id = await _make_template(tid, name="promo_tardia", status="pending")
    await _make_contact(tid, "5215550003002", opt_in=True)
    async with _client() as c:
        h = _headers(admin, ROLE_PLATFORM_ADMIN)
        r = await c.post(f"/api/v1/admin/tenants/{tid}/campaigns",
                         headers=h,
                         json={"name": "C tardía", "type": "promo",
                               "template_name": "promo_tardia",
                               "params": {}, "segment": {}})
        cid = r.json()["id"]
        # Meta aprueba -> el operador lo refleja en el panel.
        rp = await c.patch(
            f"/api/v1/admin/tenants/{tid}/templates/{tpl_id}/status",
            headers=h, json={"status": "approved"})
        assert rp.status_code == 200
        r2 = await c.post(
            f"/api/v1/admin/tenants/{tid}/campaigns/{cid}/launch", headers=h)
        assert r2.status_code == 200, r2.text
        assert r2.json()["status"] == "sending"


# ── 4. Statuses por wamid ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_statuses_update_sends_no_downgrade():
    tid = await _make_tenant()
    await _make_template(tid)
    cid_contact = await _make_contact(tid, "5215550004001", opt_in=True)
    cid = await _launch(tid)
    async with db_mod.async_session_maker() as s:
        send = (await s.execute(
            select(CampaignSend).where(CampaignSend.campaign_id == cid)
        )).scalar_one()
        send.status = "sent"
        send.wamid = "wamid.STATUS1"
        await s.commit()
    async with db_mod.async_session_maker() as s:
        upd = await campaign_svc.process_delivery_statuses(
            s, tid, [{"id": "wamid.STATUS1", "status": "delivered"}])
        assert upd["delivered"] == 1
        await s.commit()
        upd = await campaign_svc.process_delivery_statuses(
            s, tid, [{"id": "wamid.STATUS1", "status": "read"}])
        assert upd["read"] == 1
        await s.commit()
        # Status tardío no degrada read -> delivered.
        upd = await campaign_svc.process_delivery_statuses(
            s, tid, [{"id": "wamid.STATUS1", "status": "delivered"},
                     {"id": "wamid.DESCONOCIDO", "status": "read"}])
        assert upd["ignored"] == 2
        await s.commit()
    async with db_mod.async_session_maker() as s:
        send = (await s.execute(
            select(CampaignSend).where(CampaignSend.wamid == "wamid.STATUS1")
        )).scalar_one()
        assert send.status == "read"


# ── 5. Métricas + costo ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_campaign_metrics_add_up():
    tid = await _make_tenant(campaign_msgs_per_sec=100)
    await _make_template(tid)
    contacts = [await _make_contact(tid, f"52155500050{i:02d}", opt_in=True)
                for i in range(4)]
    cid = await _launch(tid)
    async with db_mod.async_session_maker() as s:
        sends = (await s.execute(
            select(CampaignSend).where(CampaignSend.campaign_id == cid)
            .order_by(CampaignSend.created_at.asc())
        )).scalars().all()
        assert len(sends) == 4
        # delivered x2 (uno leído), sent x1, failed x1
        sends[0].status = "delivered"; sends[0].wamid = "w1"
        sends[1].status = "read"; sends[1].wamid = "w2"
        sends[2].status = "sent"; sends[2].wamid = "w3"
        sends[3].status = "failed"
        # Costo: 3 envíos exitosos a 0.06
        for snd in sends[:3]:
            s.add(UsageRecord(tenant_id=tid, contact_id=snd.contact_id,
                              model="whatsapp_marketing", cost_usd=0.06,
                              kind="campaign", campaign_id=cid))
        camp = await s.get(Campaign, cid)
        camp.status = "done"
        await s.commit()
    async with db_mod.async_session_maker() as s:
        m = await campaign_svc.campaign_metrics(s, tid, cid)
    assert m["sent"] == 1
    assert m["delivered"] == 2  # delivered + read
    assert m["read"] == 1
    assert m["failed"] == 1
    assert m["read_rate_pct"] == 50.0  # 1 leído / 2 entregados
    assert abs(m["cost_usd"] - 0.18) < 1e-9


# ── 6. Conversión prospect -> client ──────────────────────────────────


@pytest.mark.asyncio
async def test_mark_contact_client_promotes_once():
    tid = await _make_tenant()
    cid = await _make_contact(tid, "5215550006001")
    async with db_mod.async_session_maker() as s:
        contact = await s.get(Contact, cid)
        assert await mark_contact_client(s, tid, contact) is True
        assert contact.contact_type == "client"
        assert await mark_contact_client(s, tid, contact) is False
        await s.commit()


# ── 7. Panel: RBAC + contactos + tags ─────────────────────────────────


@pytest.mark.asyncio
async def test_create_campaign_as_agent_forbidden():
    tid = await _make_tenant()
    agent = await _mk_user("ag@liah.local", ROLE_TENANT_AGENT, tenant_id=tid)
    await _make_template(tid)
    async with _client() as c:
        h = _headers(agent, ROLE_TENANT_AGENT, tenant_id=tid)
        r = await c.post(f"/api/v1/admin/tenants/{tid}/campaigns",
                         headers=h,
                         json={"name": "C prohibida", "type": "promo",
                               "template_name": "promo_mes",
                               "params": {}, "segment": {}})
        assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_tenant_admin_can_create_but_not_other_tenant():
    tid = await _make_tenant()
    other = await _make_tenant(slug="otro")
    ta = await _mk_user("ta@liah.local", ROLE_TENANT_ADMIN, tenant_id=tid)
    await _make_template(tid)
    async with _client() as c:
        h = _headers(ta, ROLE_TENANT_ADMIN, tenant_id=tid)
        r = await c.post(f"/api/v1/admin/tenants/{tid}/campaigns",
                         headers=h,
                         json={"name": "C admin", "type": "notice",
                               "template_name": "promo_mes",
                               "params": {}, "segment": {}})
        assert r.status_code == 201, r.text
        r2 = await c.post(f"/api/v1/admin/tenants/{other}/campaigns",
                          headers=h,
                          json={"name": "C ajena", "type": "promo",
                                "template_name": "promo_mes",
                                "params": {}, "segment": {}})
        assert r2.status_code == 403, r2.text


@pytest.mark.asyncio
async def test_contacts_list_shows_optin_and_tags():
    tid = await _make_tenant()
    admin = await _mk_user("pa3@liah.local", ROLE_PLATFORM_ADMIN)
    cid = await _make_contact(tid, "5215550007001", opt_in=True,
                              contact_type="client", tags=["vip"])
    async with _client() as c:
        h = _headers(admin, ROLE_PLATFORM_ADMIN)
        r = await c.get(f"/api/v1/admin/tenants/{tid}/contacts", headers=h)
        assert r.status_code == 200, r.text
        items = r.json()
        assert len(items) == 1
        assert items[0]["marketing_opt_in"] is True
        assert items[0]["contact_type"] == "client"
        assert items[0]["tags"] == ["vip"]
        assert items[0]["marketing_opt_in_source"] == "import"
        # Toggle por panel.
        r2 = await c.post(
            f"/api/v1/admin/tenants/{tid}/contacts/{cid}/opt-in",
            headers=h, json={"opt_in": False, "source": "panel"})
        assert r2.status_code == 200
        assert r2.json()["marketing_opt_in"] is False
        # Agregar y quitar tag.
        r3 = await c.post(
            f"/api/v1/admin/tenants/{tid}/contacts/{cid}/tags",
            headers=h, json={"tag": "Moroso VIP"})
        assert r3.status_code == 201
        assert r3.json()["tag"] == "moroso_vip"  # normalizado
        r4 = await c.delete(
            f"/api/v1/admin/tenants/{tid}/contacts/{cid}/tags/moroso_vip",
            headers=h)
        assert r4.status_code == 200


@pytest.mark.asyncio
async def test_cancel_campaign_stops_pending():
    tid = await _make_tenant(campaign_msgs_per_sec=100)
    admin = await _mk_user("pa4@liah.local", ROLE_PLATFORM_ADMIN)
    await _make_template(tid)
    await _make_contact(tid, "5215550008001", opt_in=True)
    async with _client() as c:
        h = _headers(admin, ROLE_PLATFORM_ADMIN)
        r = await c.post(f"/api/v1/admin/tenants/{tid}/campaigns",
                         headers=h,
                         json={"name": "C cancel", "type": "promo",
                               "template_name": "promo_mes",
                               "params": {}, "segment": {}})
        cid = r.json()["id"]
        rc = await c.post(
            f"/api/v1/admin/tenants/{tid}/campaigns/{cid}/cancel", headers=h)
        assert rc.status_code == 200
        assert rc.json()["status"] == "cancelled"
    # El dispatch no envía nada de una campaña cancelada.
    stats = await campaign_svc.dispatch_campaigns(
        db_mod.async_session_maker, dry_run=True)
    assert stats["sent"] == 0
