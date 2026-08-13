"""Scheduler de recordatorios (Fase 2).

AsyncIOScheduler arrancado desde el lifespan de FastAPI. Job periódico que
evalúa reglas activas por tenant y dispara dispatch_reminder.

Idempotencia doble:
1. reminder_log previo (pending/sent) -> dispatch lo omite.
2. un solo scheduler en el proceso (no usamos múltiples workers para el job).
"""
import asyncio
import logging
from datetime import datetime

from sqlalchemy import select

from app.core import db as db_mod
from app.models import AutomationRule, ReminderLog, Template, Tenant
from app.reminders import dispatch

logger = logging.getLogger("reminders.scheduler")

RULE_TYPES = ["trial_class", "colegiatura", "followup_30d"]


async def run_once(dry_run: bool = False):
    """Una pasada del scheduler: para cada tenant, evalúa reglas y despacha."""
    async with db_mod.async_session_maker() as session:
        tenants = (await session.execute(select(Tenant))).scalars().all()

    for tenant in tenants:
        async with db_mod.async_session_maker() as session:
            # marca last_run de reglas activas
            rules = (
                await session.execute(
                    select(AutomationRule).where(
                        AutomationRule.tenant_id == tenant.id,
                        AutomationRule.enabled.is_(True),
                    )
                )
            ).scalars().all()
            now = datetime.utcnow()
            for rule in rules:
                targets = await dispatch.load_rule_targets(
                    session, tenant.id, tenant.name, rule.type, now
                )
                for (r, contact, scheduled_for, variables) in targets:
                    # la plantilla se resuelve por nombre esperado de la regla
                    template = (
                        await session.execute(
                            select(Template).where(
                                Template.tenant_id == tenant.id,
                                Template.name == _template_name_for(rule.type),
                            )
                        )
                    ).scalar_one_or_none()
                    if template is None:
                        logger.warning(
                            "Sin plantilla '%s' para tenant %s",
                            _template_name_for(rule.type),
                            tenant.id,
                        )
                        continue
                    await dispatch.dispatch_reminder(
                        session,
                        tenant.id,
                        tenant.name,
                        r,
                        contact,
                        template,
                        scheduled_for,
                        variables,
                        dry_run=dry_run,
                    )
                rule.last_run_at = now
                session.add(rule)
            await session.commit()


def _template_name_for(rule_type: str) -> str:
    return {
        "trial_class": "recordatorio_clase_muestra",
        "colegiatura": "aviso_colegiatura",
        "followup_30d": "seguimiento_consulta",
    }.get(rule_type, "recordatorio_generico")


async def _job_wrapper():
    try:
        await run_once(dry_run=False)
    except Exception:  # noqa: BLE001 - el scheduler no debe morir
        logger.exception("Error en job de recordatorios")


def start_scheduler():
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    sched = AsyncIOScheduler()
    # cada hora; en prod ajustar a la hora configurada
    sched.add_job(_job_wrapper, "interval", hours=1, id="reminders_hourly")
    sched.start()
    logger.info("Scheduler de recordatorios iniciado")
    return sched
