"""Servicio de campañas y avisos (Fase 6).

Ciclo de vida: draft → scheduled → sending → done | cancelled.

Reglas duras:
1. El launch EXIGE plantilla aprobada por Meta (templates.status='approved').
   Si no, 422 con mensaje accionable y la campaña sigue en draft.
2. El dispatch EXCLUYE a quien no tenga marketing_opt_in=true (promo Y
   aviso: sin opt-in es spam y Meta banea el número). El opt-out es
   irreversible por campaña porque el filtro se aplica en cada envío, no
   por snapshot al lanzar.
3. unique(campaign_id, contact_id): re-correr el dispatch jamás duplica.
4. Pacing anti-baneo: N mensajes/segundo configurable por tenant
   (tenant_configs.extra["campaign_msgs_per_sec"], default env
   LIAH_CAMPAIGN_RATE_PER_SEC=1, conservador). El dispatch corre en el
   ciclo del worker, no en el request.
5. Cada envío exitoso registra su costo en usage_records (kind='campaign',
   model='whatsapp_marketing', campaign_id): el cliente paga las
   conversaciones de marketing de Meta por separado.
"""
import asyncio
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.sender import send_template
from app.core.audit import log_event
from app.core.billing import is_tenant_active
from app.core.config import get_settings
from app.models import (
    Campaign,
    CampaignSend,
    Contact,
    ContactTag,
    Template,
    TenantConfig,
    UsageRecord,
)

logger = logging.getLogger("liah.marketing.campaigns")

TEMPLATE_APPROVED = "approved"

# Costo por conversación de marketing (USD). Placeholder calibrable:
# Meta publica su matriz de precios por país/categoría y cambia; el valor
# se sobreescribe por tenant vía extra["campaign_cost_usd"].
_DEFAULT_CAMPAIGN_COST_USD = 0.06
# Ventana supuesta por ciclo del worker para el presupuesto de envíos.
_DISPATCH_WINDOW_S = 60


class CampaignError(Exception):
    """Error de negocio de campaña (el API lo traduce a 4xx)."""


def _now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _campaign_cost_usd(extra: dict) -> float:
    try:
        return float(extra.get("campaign_cost_usd",
                              get_settings().liah_campaign_cost_usd))
    except (TypeError, ValueError):
        return _DEFAULT_CAMPAIGN_COST_USD


def _rate_per_sec(extra: dict) -> float:
    try:
        rate = float(extra.get("campaign_msgs_per_sec",
                               get_settings().liah_campaign_rate_per_sec))
    except (TypeError, ValueError):
        rate = get_settings().liah_campaign_rate_per_sec
    return max(rate, 0.01)


async def _tenant_extra(session: AsyncSession, tenant_id) -> dict:
    cfg = (
        await session.execute(
            select(TenantConfig).where(TenantConfig.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    return (cfg.extra or {}) if cfg else {}


# ── Segmentación ──────────────────────────────────────────────────────


def _recipient_ids_stmt(tenant_id, segment: dict):
    """IDs de contactos del segmento CON opt-in de marketing.

    segment = {"contact_type": "client"|"prospect", "tags": ["vip", ...]}.
    Los tags se evalúan con OR: basta UNO para calificar.
    El opt-in se exige siempre (regla dura).
    """
    stmt = (
        select(Contact.id)
        .where(
            Contact.tenant_id == tenant_id,
            Contact.marketing_opt_in.is_(True),
        )
    )
    contact_type = (segment or {}).get("contact_type")
    if contact_type:
        if contact_type not in ("client", "prospect"):
            raise CampaignError(
                f"segment.contact_type inválido: {contact_type!r} "
                "(válidos: 'client' | 'prospect')"
            )
        stmt = stmt.where(Contact.contact_type == contact_type)
    tags = (segment or {}).get("tags") or []
    if tags:
        tag_exists = (
            select(ContactTag.id)
            .where(
                ContactTag.tenant_id == tenant_id,
                ContactTag.contact_id == Contact.id,
                ContactTag.tag.in_(tags),
            )
            .exists()
        )
        stmt = stmt.where(tag_exists)
    return stmt


async def estimate_recipients(
    session: AsyncSession, tenant_id, segment: dict
) -> int:
    """Destinatarios estimados ANTES de lanzar (con opt-in aplicado)."""
    stmt = select(func.count()).select_from(
        _recipient_ids_stmt(tenant_id, segment).subquery()
    )
    return (await session.execute(stmt)).scalar() or 0


# ── Creación ──────────────────────────────────────────────────────────


async def resolve_template(
    session: AsyncSession, tenant_id, template_name: str
) -> Template:
    tpl = (
        await session.execute(
            select(Template).where(
                Template.tenant_id == tenant_id,
                Template.name == template_name,
            )
        )
    ).scalar_one_or_none()
    if tpl is None:
        raise CampaignError(
            f"Plantilla {template_name!r} no existe para este tenant. "
            "Créala en /me/templates o en el panel y espera la aprobación "
            "de Meta antes de lanzar."
        )
    return tpl


async def create_campaign(
    session: AsyncSession,
    tenant_id,
    *,
    name: str,
    type: str,
    template_name: str,
    params: dict | None = None,
    segment: dict | None = None,
    scheduled_at: datetime | None = None,
    created_by: str | None = None,
) -> Campaign:
    """Crea la campaña en draft (sin validar aprobación: eso es del launch)."""
    if type not in ("promo", "notice"):
        raise CampaignError(f"type inválido: {type!r} (válidos: 'promo' | 'notice')")
    tpl = await resolve_template(session, tenant_id, template_name)
    segment = segment or {}
    # Validación temprana del segmento (barata): falla en el POST, no en el launch.
    await estimate_recipients(session, tenant_id, segment)
    campaign = Campaign(
        tenant_id=tenant_id,
        name=name,
        type=type,
        template_id=tpl.id,
        template_name=tpl.name,
        params=params or {},
        segment=segment,
        status="scheduled" if scheduled_at else "draft",
        scheduled_at=scheduled_at,
        created_by=created_by,
    )
    session.add(campaign)
    await session.flush()
    await log_event(
        session, tenant_id, "campaign.created",
        {"campaign_id": str(campaign.id), "name": name, "type": type,
         "template": template_name},
    )
    return campaign


# ── Launch ────────────────────────────────────────────────────────────


async def launch_campaign(
    session: AsyncSession,
    tenant_id,
    campaign_id,
    *,
    dry_run: bool = False,
) -> Campaign:
    """Pasa la campaña a 'sending' y encola UN CampaignSend por destinatario.

    Valida plantilla aprobada por Meta: si no lo está → CampaignError
    (el API lo traduce a 422) y la campaña NO sale de draft/scheduled.
    Idempotente: re-lanzar usa INSERT ... ON CONFLICT DO NOTHING.
    """
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None or campaign.tenant_id != tenant_id:
        raise CampaignError("campaña no encontrada")
    if campaign.status not in ("draft", "scheduled"):
        raise CampaignError(
            f"solo se puede lanzar desde draft/scheduled (actual: {campaign.status})"
        )
    tpl = None
    if campaign.template_id:
        tpl = await session.get(Template, campaign.template_id)
    if tpl is None or tpl.tenant_id != tenant_id:
        raise CampaignError(
            f"La plantilla {campaign.template_name!r} ya no existe para este "
            "tenant: edita la campaña con una plantilla vigente."
        )
    if tpl.status != TEMPLATE_APPROVED:
        raise CampaignError(
            f"La plantilla {tpl.name!r} está en estado {tpl.status!r}, no "
            "aprobada por Meta. Las campañas solo se envían con plantillas "
            "APROBADAS: espera la aprobación en el Business Manager y marca "
            "la plantilla como aprobada en el panel (Campañas → Plantillas)."
        )

    recipient_ids = (
        await session.execute(_recipient_ids_stmt(tenant_id, campaign.segment))
    ).scalars().all()

    # Inserción idempotente: re-lanzar no duplica (unique campaign+contact).
    if recipient_ids:
        stmt = (
            pg_insert(CampaignSend)
            .values([
                {
                    "tenant_id": tenant_id,
                    "campaign_id": campaign.id,
                    "contact_id": cid,
                    "status": "queued",
                }
                for cid in recipient_ids
            ])
            .on_conflict_do_nothing(
                constraint="uq_campaign_sends_campaign_contact"
            )
        )
        await session.execute(stmt)

    campaign.status = "sending"
    campaign.launched_at = _now_naive()
    campaign.total_targets = len(recipient_ids)
    campaign.last_error = None
    await log_event(
        session, tenant_id, "campaign.launched",
        {"campaign_id": str(campaign.id), "name": campaign.name,
         "targets": len(recipient_ids), "template": campaign.template_name,
         "dry_run": dry_run},
    )
    await session.flush()
    logger.info(
        "Campaña %s lanzada: %d destinatarios (dry_run=%s)",
        campaign.id, len(recipient_ids), dry_run,
    )
    return campaign


async def cancel_campaign(session: AsyncSession, tenant_id, campaign_id) -> Campaign:
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None or campaign.tenant_id != tenant_id:
        raise CampaignError("campaña no encontrada")
    if campaign.status in ("done", "cancelled"):
        raise CampaignError(f"la campaña ya está {campaign.status}")
    campaign.status = "cancelled"
    # Los queued pendientes se marcan failed para que el dispatch los ignore.
    await session.execute(
        CampaignSend.__table__.update()
        .where(
            CampaignSend.campaign_id == campaign.id,
            CampaignSend.status == "queued",
        )
        .values(status="failed")
    )
    await log_event(
        session, tenant_id, "campaign.cancelled",
        {"campaign_id": str(campaign.id), "name": campaign.name},
    )
    await session.flush()
    return campaign


# ── Dispatch (ciclo del worker, con pacing) ───────────────────────────


def _build_components(template: Template, params: dict) -> list[dict]:
    """Components type=body con parámetros {{1}} {{2}}... desde params.

    params puede ser dict {nombre: valor} (se ordena por variables de la
    plantilla) o lista posicional. Valores faltantes → "" (Meta rechaza
    parámetros vacíos solo en headers; en body se toleran).
    """
    if isinstance(params, dict):
        variables = template.variables or []
        values = [params.get(v, "") for v in variables] if variables else list(params.values())
    else:
        values = list(params or [])
    body_params = [{"type": "text", "text": str(v)} for v in values]
    return [{"type": "body", "parameters": body_params}]


async def _send_one(
    session: AsyncSession,
    tenant_id,
    campaign: Campaign,
    send: CampaignSend,
    contact: Contact,
    template: Template,
    cost_usd: float,
    dry_run: bool,
) -> None:
    """Envía un HSM a un contacto y registra costo. Nunca propaga excepción."""
    components = _build_components(template, campaign.params or {})
    try:
        meta_id = await send_template(
            session,
            str(tenant_id),
            str(contact.id),
            contact.wa_id,
            template.name,
            template.language,
            components,
            dry_run=dry_run,
            idempotency_key=f"campaign:{campaign.id}:{contact.id}",
        )
    except Exception as e:
        send.status = "failed"
        send.sent_at = _now_naive()
        await log_event(
            session, tenant_id, "campaign.send_failed",
            {"campaign_id": str(campaign.id), "contact_id": str(contact.id),
             "error": f"{type(e).__name__}: {e}"[:500]},
        )
        return
    send.status = "sent"
    send.wamid = meta_id  # None en dry-run: sin wamid no hay statuses
    send.sent_at = _now_naive()
    # Costo de la conversación de marketing (lo paga el cliente). En
    # dry_run se registra como estimado (no hubo cargo real de Meta).
    session.add(
        UsageRecord(
            tenant_id=tenant_id,
            contact_id=contact.id,
            model="whatsapp_marketing",
            tokens_in=0,
            tokens_out=0,
            cost_usd=cost_usd,
            kind="campaign",
            campaign_id=campaign.id,
        )
    )


async def dispatch_campaigns(
    session_maker: async_sessionmaker,
    *,
    dry_run: bool = False,
    tenant_id=None,
) -> dict:
    """Avanza campañas 'sending' y auto-lanza 'scheduled' vencidas.

    Pacing: presupuesto por campaña = rate_per_sec × ventana(60s) envíos
    por llamada, con pausa de 1/rate entre envíos. Al vaciarse los queued,
    la campaña pasa a done. Un fallo individual no tumba el ciclo.
    """
    stats = {"campaigns": 0, "sent": 0, "failed": 0, "auto_launched": 0}
    now = _now_naive()

    async with session_maker() as session:
        q = select(Campaign).where(Campaign.status.in_(("sending", "scheduled")))
        if tenant_id is not None:
            q = q.where(Campaign.tenant_id == tenant_id)
        campaigns = (await session.execute(q.order_by(Campaign.created_at.asc()))).scalars().all()
        campaign_ids = [c.id for c in campaigns]

    for campaign_id in campaign_ids:
        async with session_maker() as session:
            campaign = await session.get(Campaign, campaign_id)
            if campaign is None:
                continue
            tenant = campaign.tenant_id
            # Fase 8: no gastar Meta en tenants suspendidos/inactivos.
            if not await is_tenant_active(session, tenant):
                continue
            extra = await _tenant_extra(session, tenant)

            # Auto-launch de programadas vencidas.
            if campaign.status == "scheduled":
                if campaign.scheduled_at and campaign.scheduled_at > now:
                    continue
                try:
                    await launch_campaign(session, tenant, campaign.id, dry_run=dry_run)
                    await session.commit()
                    stats["auto_launched"] += 1
                except CampaignError as e:
                    # p.ej. plantilla aún no aprobada: vuelve a draft con
                    # el motivo visible para el operador.
                    campaign.status = "draft"
                    campaign.last_error = str(e)
                    await log_event(
                        session, tenant, "campaign.autolaunch_failed",
                        {"campaign_id": str(campaign.id), "error": str(e)[:500]},
                    )
                    await session.commit()
                    continue
                # Re-lee tras el launch para el dispatch de abajo.
                await session.refresh(campaign)

            if campaign.status != "sending":
                continue
            template = await session.get(Template, campaign.template_id) \
                if campaign.template_id else None
            if template is None:
                campaign.status = "draft"
                campaign.last_error = "plantilla eliminada durante el envío"
                await session.commit()
                continue

            rate = _rate_per_sec(extra)
            budget = max(int(rate * _DISPATCH_WINDOW_S), 1)
            cost_usd = _campaign_cost_usd(extra)

            queued = (
                await session.execute(
                    select(CampaignSend)
                    .where(
                        CampaignSend.campaign_id == campaign.id,
                        CampaignSend.status == "queued",
                    )
                    .order_by(CampaignSend.created_at.asc())
                    .limit(budget)
                )
            ).scalars().all()

            for send in queued:
                # REGLA DURA re-verificada en cada envío: el opt-in se
                # consulta AHORA, no al lanzar. Un opt-out posterior al
                # launch excluye al contacto automáticamente.
                contact = await session.get(Contact, send.contact_id)
                if contact is None or not contact.marketing_opt_in:
                    send.status = "failed"
                    send.sent_at = _now_naive()
                    await log_event(
                        session, tenant, "campaign.send_skipped_no_optin",
                        {"campaign_id": str(campaign.id),
                         "contact_id": str(send.contact_id)},
                    )
                    stats["failed"] += 1
                    continue
                await _send_one(
                    session, tenant, campaign, send, contact, template,
                    cost_usd, dry_run,
                )
                if send.status == "sent":
                    stats["sent"] += 1
                else:
                    stats["failed"] += 1
                await session.commit()
                # Pacing anti-baneo.
                if rate > 0:
                    await asyncio.sleep(1.0 / rate)

            remaining = (
                await session.execute(
                    select(func.count()).select_from(CampaignSend).where(
                        CampaignSend.campaign_id == campaign.id,
                        CampaignSend.status == "queued",
                    )
                )
            ).scalar() or 0
            if remaining == 0:
                campaign.status = "done"
                await log_event(
                    session, tenant, "campaign.done",
                    {"campaign_id": str(campaign.id), "name": campaign.name,
                     "targets": campaign.total_targets},
                )
            await session.commit()
            stats["campaigns"] += 1
    return stats


# ── Statuses del webhook (delivered/read por wamid) ───────────────────


async def process_delivery_statuses(
    session: AsyncSession, tenant_id, statuses: list[dict]
) -> dict:
    """Matchea statuses de Meta (por wamid) contra campaign_sends.

    Actualiza a delivered/read/failed SIN degradar (un 'read' no vuelve a
    'delivered' por un status tardío). Los wamid que no son de campaña se
    ignoran en silencio (pertenecen a recordatorios u otros envíos).
    """
    from app.models.campaigns import send_progress

    updated = {"delivered": 0, "read": 0, "failed": 0, "ignored": 0}
    for st in statuses or []:
        wamid = st.get("id")
        new_status = st.get("status")
        if not wamid or new_status not in ("delivered", "read", "failed"):
            updated["ignored"] += 1
            continue
        send = (
            await session.execute(
                select(CampaignSend).where(
                    CampaignSend.tenant_id == tenant_id,
                    CampaignSend.wamid == wamid,
                )
            )
        ).scalar_one_or_none()
        if send is None:
            updated["ignored"] += 1
            continue
        if new_status == "failed":
            # Un mensaje ya entregado/leído no puede "fallar" después.
            if send.status not in ("delivered", "read"):
                send.status = "failed"
                updated["failed"] += 1
            else:
                updated["ignored"] += 1
        elif send_progress(new_status) > send_progress(send.status):
            send.status = new_status
            updated[new_status] += 1
        else:
            updated["ignored"] += 1
    if any(v for k, v in updated.items() if k != "ignored"):
        await log_event(
            session, tenant_id, "campaign.statuses_applied", dict(updated)
        )
    await session.flush()
    return updated


# ── Métricas ──────────────────────────────────────────────────────────


async def campaign_metrics(
    session: AsyncSession, tenant_id, campaign_id
) -> dict:
    """Enviados, entregados, leídos, fallidos + tasa de lectura + costo.

    Tasa de lectura = read / delivered (los no entregados no pudieron
    leerse; si delivered=0, None en vez de un número mentiroso).
    """
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None or campaign.tenant_id != tenant_id:
        raise CampaignError("campaña no encontrada")
    rows = (
        await session.execute(
            select(CampaignSend.status, func.count(CampaignSend.id))
            .where(CampaignSend.campaign_id == campaign.id)
            .group_by(CampaignSend.status)
        )
    ).all()
    counts = {s: 0 for s in ("queued", "sent", "delivered", "read", "failed")}
    counts.update({s: int(n) for s, n in rows})
    delivered = counts["delivered"] + counts["read"]
    read_rate = (
        round(counts["read"] / delivered * 100, 1) if delivered else None
    )
    cost = (
        await session.execute(
            select(func.coalesce(func.sum(UsageRecord.cost_usd), 0)).where(
                UsageRecord.tenant_id == tenant_id,
                UsageRecord.campaign_id == campaign.id,
                UsageRecord.kind == "campaign",
            )
        )
    ).scalar() or 0
    return {
        "campaign_id": str(campaign.id),
        "status": campaign.status,
        "total_targets": campaign.total_targets,
        "queued": counts["queued"],
        "sent": counts["sent"],
        "delivered": delivered,
        "read": counts["read"],
        "failed": counts["failed"],
        "read_rate_pct": read_rate,
        "cost_usd": round(float(cost), 6),
    }
