"""Dispatch de recordatorios HSM (Fase 2).

Responsabilidades:
- Gate de privacidad (LFPDPPP): solo contactos con consent_status='granted'.
- Idempotencia: no reenvía si ya existe reminder_log (pending/sent) para la
  combinación (tenant, rule, contact, scheduled_for).
- Arma las variables del template desde la BD (fuente de verdad).
"""
import uuid
from datetime import datetime, timedelta, timezone

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
    appointment_id: uuid.UUID | None = None,
) -> bool:
    """Idempotencia por (rule, contact, appointment, scheduled_for).

    `appointment_id=None` conserva el comportamiento legacy (reglas no
    asociadas a una cita). Con cita, dos citas del mismo contacto que
    calculen el mismo `scheduled_for` no se pisan (Fase 4).
    """
    stmt = select(ReminderLog).where(
        ReminderLog.tenant_id == tenant_id,
        ReminderLog.rule_id == rule_id,
        ReminderLog.contact_id == contact_id,
        ReminderLog.scheduled_for == scheduled_for,
        ReminderLog.status.in_(["pending", "sent"]),
    )
    if appointment_id is None:
        stmt = stmt.where(ReminderLog.appointment_id.is_(None))
    else:
        stmt = stmt.where(ReminderLog.appointment_id == appointment_id)
    existing = await session.execute(stmt)
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
    appointment_id: uuid.UUID | None = None,
) -> bool:
    """Envía (o registra) un recordatorio HSM para un contacto.

    Devuelve True si se envió/registró; False si fue bloqueado por
    consentimiento o idempotencia.
    """
    if not await _consent_ok(session, contact):
        return False
    # Columnas naive en el esquema: normalizar scheduled_for a UTC naive una
    # sola vez a la entrada para no mezclar aware/naive en queries ni logs.
    if scheduled_for.tzinfo is not None:
        scheduled_for = scheduled_for.astimezone(timezone.utc).replace(tzinfo=None)
    if await _already_sent(
        session, tenant_id, rule.id, contact.id, scheduled_for, appointment_id
    ):
        return False

    components = _build_components(template, variables)
    # registramos el intento ANTES del envío (idempotencia fuerte)
    log = ReminderLog(
        tenant_id=tenant_id,
        rule_id=rule.id,
        contact_id=contact.id,
        template_id=template.id,
        appointment_id=appointment_id,
        scheduled_for=scheduled_for,
        status="pending",
    )
    session.add(log)
    await session.commit()
    await session.refresh(log)

    # Un fallo de envío (red, token, Meta) NO debe matar el cron: se marca
    # failed y el scheduler sigue con el siguiente recordatorio.
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
        )
    except Exception as e:
        log.status = "failed"
        # Columna naive: se guarda UTC sin tzinfo (convención del esquema).
        log.sent_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await session.commit()
        return False
    log.status = "sent" if meta_id is not None or dry_run else "failed"
    log.sent_at = datetime.now(timezone.utc).replace(tzinfo=None)
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
    """Resuelve (rule, contact, scheduled_for, variables, appointment_id) según
    el tipo de regla.

    Fuente de verdad = appointments/contacts. No inventa fechas.
    `appointment_id` es el id de la cita origen (None en reglas que no son
    por cita); viaja hasta reminder_log para una idempotencia correcta.
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
            # LEGACY (Fase 0, demo de academia): recordatorio el día anterior
            # a una cita confirmada de este tipo. Se conserva por
            # compatibilidad con tenants creados antes de la Fase 4; los
            # perfiles nuevos usan `appointment_reminder`.
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
                    # El nombre del servicio sale de los params de la regla
                    # (antes estaba hardcodeado: deuda del demo extraída en
                    # Fase 4; ver docs/DECISIONES_FASE4.md).
                    servicio = r.params.get("servicio") or appt.type
                    vars_ = [
                        contact.name or "Hola",
                        tenant_name,
                        appt.start_at.strftime("%H:%M"),
                        servicio,
                    ]
                    targets.append((r, contact, scheduled_for, vars_, appt.id))
        elif rule_type == "appointment_reminder":
            # Genérico (Fase 4): recordatorio N horas antes de cualquier cita
            # confirmada. params: {hours_before: [24, 2],
            # template_name: "recordatorio_cita", require_consent: true}.
            # `scheduled_for` = inicio_de_cita - h: cada h tiene su propio
            # scheduled_for, así el recordatorio de 24h no pisa al de 2h en
            # la idempotencia de reminder_log. El gate de consentimiento lo
            # aplica dispatch_reminder (LFPDPPP, siempre).
            hours = r.params.get("hours_before") or [24]
            rows = await session.execute(
                select(Appointment, Contact).join(
                    Contact, Contact.id == Appointment.contact_id
                ).where(
                    Appointment.tenant_id == tenant_id,
                    Appointment.status == "confirmed",
                )
            )
            for appt, contact in rows.all():
                delta_h = (appt.start_at - now).total_seconds() / 3600
                if delta_h <= 0:
                    continue
                for h in hours:
                    scheduled_for = appt.start_at - timedelta(hours=float(h))
                    if now >= scheduled_for:
                        vars_ = [
                            contact.name or "Hola",
                            tenant_name,
                            appt.start_at.strftime("%d/%m/%Y"),
                            appt.start_at.strftime("%H:%M"),
                        ]
                        targets.append(
                            (r, contact, scheduled_for, vars_, appt.id)
                        )
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
                    targets.append((r, contact, now, vars_, None))
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
                    targets.append((r, contact, now, vars_, appt.id))
    return targets
