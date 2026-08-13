"""Dispatch de recordatorios HSM (Fase 2).

Responsabilidades:
- Gate de privacidad (LFPDPPP): solo contactos con consent_status='granted'.
- Idempotencia: no reenvía si ya existe reminder_log (pending/sent) para la
  combinación (tenant, rule, contact, scheduled_for).
- Arma las variables del template desde la BD (fuente de verdad).
"""
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.sender import send_template
from app.models import (
    Appointment,
    AutomationRule,
    Contact,
    ReminderLog,
    Template,
)


async def _consent_ok(session: AsyncSession, contact: Contact) -> bool:
    # LFPDPPP: solo enviar si el consentimiento está expresamente concedido.
    return contact.consent_status == "granted"


async def _already_sent(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    rule_id: uuid.UUID,
    contact_id: uuid.UUID,
    scheduled_for: datetime,
) -> bool:
    existing = await session.execute(
        select(ReminderLog).where(
            ReminderLog.tenant_id == tenant_id,
            ReminderLog.rule_id == rule_id,
            ReminderLog.contact_id == contact_id,
            ReminderLog.scheduled_for == scheduled_for,
            ReminderLog.status.in_(["pending", "sent"]),
        )
    )
    return existing.scalar_one_or_none() is not None


def _build_components(template: Template, variables: list[str]) -> list[dict]:
    """Arma components type=body con los parámetros posicionales {{1}} {{2}}..."""
    params = [{"type": "text", "text": str(v)} for v in variables]
    return [{"type": "body", "parameters": params}]


async def dispatch_reminder(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    tenant_name: str,
    rule: AutomationRule,
    contact: Contact,
    template: Template,
    scheduled_for: datetime,
    variables: list[str],
    dry_run: bool = False,
) -> bool:
    """Envía (o registra) un recordatorio HSM para un contacto.

    Devuelve True si se envió/registró; False si fue bloqueado por
    consentimiento o idempotencia.
    """
    if not await _consent_ok(session, contact):
        return False
    if await _already_sent(session, tenant_id, rule.id, contact.id, scheduled_for):
        return False

    components = _build_components(template, variables)
    # registramos el intento ANTES del envío (idempotencia fuerte)
    log = ReminderLog(
        tenant_id=tenant_id,
        rule_id=rule.id,
        contact_id=contact.id,
        template_id=template.id,
        scheduled_for=scheduled_for,
        status="pending",
    )
    session.add(log)
    await session.commit()
    await session.refresh(log)

    meta_id = await send_template(
        session,
        str(tenant_id),
        str(contact.id),
        contact.wa_id,
        template.name,
        template.language,
        components,
        dry_run=dry_run,
    )
    log.status = "sent" if meta_id is not None or dry_run else "failed"
    log.sent_at = datetime.utcnow()
    log.meta_message_id = meta_id
    await session.commit()
    return True


async def load_rule_targets(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    tenant_name: str,
    rule_type: str,
    now: datetime,
):
    """Resuelve (contact, scheduled_for, variables) según el tipo de regla.

    Fuente de verdad = appointments/contacts. No inventa fechas.
    """
    targets = []
    rule = (
        await session.execute(
            select(AutomationRule).where(
                AutomationRule.tenant_id == tenant_id,
                AutomationRule.type == rule_type,
                AutomationRule.enabled.is_(True),
            )
        )
    ).scalars().all()

    for r in rule:
        if rule_type == "trial_class":
            # recordatorio el día anterior a una clase muestra confirmada
            rows = await session.execute(
                select(Appointment, Contact).join(
                    Contact, Contact.id == Appointment.contact_id
                ).where(
                    Appointment.tenant_id == tenant_id,
                    Appointment.type == "trial_class",
                    Appointment.status == "confirmed",
                )
            )
            for appt, contact in rows.all():
                scheduled_for = appt.start_at
                target_date = scheduled_for.date()
                if (target_date - now.date()).days == 1:
                    vars_ = [
                        contact.name or "Hola",
                        tenant_name,
                        appt.start_at.strftime("%H:%M"),
                        "ballet",
                    ]
                    targets.append((r, contact, scheduled_for, vars_))
        elif rule_type == "colegiatura":
            # días 1-10 del mes en curso: avisa a todos los contactos del tenant
            if 1 <= now.day <= 10:
                contacts = (
                    await session.execute(
                        select(Contact).where(Contact.tenant_id == tenant_id)
                    )
                ).scalars().all()
                for contact in contacts:
                    vars_ = [contact.name or "Hola", tenant_name, "10"]
                    targets.append((r, contact, now, vars_))
        elif rule_type == "followup_30d":
            # 30 días tras última interacción/consulta
            rows = await session.execute(
                select(Appointment, Contact).join(
                    Contact, Contact.id == Appointment.contact_id
                ).where(
                    Appointment.tenant_id == tenant_id,
                    Appointment.type == "consultation",
                    Appointment.status == "confirmed",
                )
            )
            for appt, contact in rows.all():
                if (now - appt.start_at).days >= 30:
                    vars_ = [contact.name or "Hola", tenant_name]
                    targets.append((r, contact, now, vars_))
    return targets
