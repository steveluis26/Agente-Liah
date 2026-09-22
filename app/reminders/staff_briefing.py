"""Resumen matutino proactivo para el staff (Fase 7f).

Job del scheduler: a la hora configurada (zona del tenant) cada miembro del
staff recibe su agenda del día (nº de citas, horas, contactos, tipo de
servicio, venue). Cada rol recibe su alcance: el specialist solo lo suyo
(vía `app/agent/staff.py::today_appointments`).

Config por tenant (`TenantConfig.extra["staff_briefing"]`; se edita con el
endpoint de config del panel, igual que el resto de `extra`):
    {
      "enabled": true,
      "hour": "07:30",                       # HH:MM en la zona del tenant
      "roles": ["owner", "receptionist", "specialist"]
    }
Defaults: enabled=False, hour="07:30", roles=todos. `enabled=false` o un
tenant sin staff -> no se envía nada.

Disparo: `send_due_staff_briefings()` se llama periódicamente (worker y
scheduler in-process). Es "due" si la hora configurada de HOY ya pasó y aún
no pasaron 45 minutos desde entonces; la idempotencia por
`idempotency_key=f"staff-briefing:{tenant_id}:{YYYY-MM-DD}"` (registrada en
action_log incluso en dry_run) evita el reenvío dentro de la ventana.
`now` es inyectable para tests.
"""
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent import staff as staffmod
from app.agent.staff_notify import notify_staff
from app.core.audit import log_event
from app.models import StaffMember, Tenant, TenantConfig

logger = logging.getLogger("reminders.staff_briefing")

BRIEFING_WINDOW_MINUTES = 45
DEFAULT_HOUR = "07:30"
DEFAULT_ROLES = ("owner", "receptionist", "specialist")


def _briefing_cfg(extra: dict | None) -> dict:
    cfg = ((extra or {}).get("staff_briefing") or {})
    hour = str(cfg.get("hour") or DEFAULT_HOUR)
    try:
        datetime.strptime(hour, "%H:%M")
    except ValueError:
        logger.warning("staff_briefing.hour inválida %r; uso %s", hour, DEFAULT_HOUR)
        hour = DEFAULT_HOUR
    roles = cfg.get("roles") or list(DEFAULT_ROLES)
    return {"enabled": bool(cfg.get("enabled")), "hour": hour, "roles": roles}


def _is_due(now: datetime, hour: str) -> bool:
    """La hora configurada de hoy ya pasó y estamos dentro de la ventana."""
    hh, mm = int(hour[:2]), int(hour[3:5])
    scheduled = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return scheduled <= now < scheduled + timedelta(minutes=BRIEFING_WINDOW_MINUTES)


async def _tenant_briefing(
    session: AsyncSession, tenant: Tenant, *, now: datetime, dry_run: bool
) -> dict:
    """Envía el briefing del tenant si está due. Devuelve estadísticas."""
    stats = {"sent": 0, "skipped": 0}
    cfg_row = (
        await session.execute(
            select(TenantConfig).where(TenantConfig.tenant_id == tenant.id)
        )
    ).scalar_one_or_none()
    cfg = _briefing_cfg(cfg_row.extra if cfg_row else None)
    if not cfg["enabled"]:
        return stats
    try:
        tz = ZoneInfo(tenant.timezone or "UTC")
    except Exception:  # noqa: BLE001 - zona inválida: no tumbar el job
        logger.warning("Zona inválida %r en tenant %s; omito briefing",
                       tenant.timezone, tenant.id)
        return stats
    local_now = now.astimezone(tz).replace(tzinfo=None) if now.tzinfo else now
    if not _is_due(local_now, cfg["hour"]):
        return stats

    staff_rows = (
        await session.execute(
            select(StaffMember).where(
                StaffMember.tenant_id == tenant.id,
                StaffMember.role.in_(tuple(cfg["roles"])),
            )
        )
    ).scalars().all()
    if not staff_rows:
        await log_event(session, tenant.id, "staff.briefing_skipped",
                        {"reason": "sin staff"})
        return stats

    fecha_txt = local_now.strftime("%d/%m/%Y")
    day_key = local_now.strftime("%Y-%m-%d")
    for s in staff_rows:
        appts = await staffmod.today_appointments(
            session, tenant.id, s, day=local_now
        )
        text = (
            f"☀️ Buenos días, {s.nombre}.\n\n"
            + staffmod.format_agenda(appts, who=None, fecha_txt=fecha_txt)
        )
        res = await notify_staff(
            session, tenant.id, text,
            roles=(s.role,),
            idempotency_key=f"staff-briefing:{tenant.id}:{day_key}",
            dry_run=dry_run,
        )
        if res.get(str(s.id)) == "sent":
            stats["sent"] += 1
        else:
            stats["skipped"] += 1
    await log_event(session, tenant.id, "staff.briefing_sent",
                    {"day": day_key, **stats})
    return stats


async def send_due_staff_briefings(
    session_factory: async_sessionmaker,
    *,
    dry_run: bool | None = None,
    now: datetime | None = None,
) -> dict:
    """Una pasada: briefing matutino de los tenants donde esté due.

    `now`: override para tests (default: ahora UTC). Devuelve
    {str(tenant_id): {"sent": n, "skipped": n}}.
    """
    import os
    if dry_run is None:
        dry_run = os.getenv("LIAH_SEND_DRY_RUN", "1") == "1"
    now = now or datetime.now().astimezone()
    out: dict = {}
    async with session_factory() as session:
        tenants = (await session.execute(select(Tenant))).scalars().all()
        tenant_ids = [t.id for t in tenants]
    for tid in tenant_ids:
        async with session_factory() as session:
            try:
                tenant = await session.get(Tenant, tid)
                if tenant is None:
                    continue
                stats = await _tenant_briefing(session, tenant, now=now,
                                               dry_run=dry_run)
                out[str(tid)] = stats
                await session.commit()
            except Exception:  # noqa: BLE001 - un tenant no tumba el resto
                logger.exception("Fallo briefing de staff tenant %s", tid)
                await session.rollback()
    return out


__all__ = ["send_due_staff_briefings", "BRIEFING_WINDOW_MINUTES"]
