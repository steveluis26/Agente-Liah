"""Notificaciones al staff por el canal (Fase 7f).

`notify_staff()` es el helper reutilizable para alertas proactivas:
cancelaciones, huecos liberados, handoffs. Habla contra `SenderPort`
(`app/agent/sender.py::WhatsAppCloudSender`), igual que el resto del
sistema.

Identidad de canal: el envío persiste `Message`/`ActionLog`, que exigen un
`Contact`. El staff NO es un cliente: `notify_staff` asegura un Contact con
`contact_type="staff"` por wa_id (creándolo si no existe). Ese tipo queda
fuera de la segmentación de clientes/curiosos (las campañas solo aceptan
client|prospect y el listado del panel excluye "staff" por defecto).

Garantía: un fallo notificando a un miembro no tumba a los demás ni a la
operación principal (los llamadores —cancel, waitlist, handoff— envuelven
además en try/except: notificar jamás revierte lo ya hecho).
"""
import logging
import os

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.sender import WhatsAppCloudSender
from app.core.audit import log_event
from app.models import Contact, StaffMember

logger = logging.getLogger("liah.staff_notify")


def _dry_run_default() -> bool:
    return os.getenv("LIAH_SEND_DRY_RUN", "1") == "1"


async def ensure_staff_contact(
    session: AsyncSession, tenant_id, staff: StaffMember
) -> Contact:
    """Contact de canal para el staff (contact_type="staff").

    Si el wa_id ya existía como contacto de cliente (p.ej. antes de darlo
    de alta como staff), se reclasifica a "staff": un número staff nunca
    debe tratarse como cliente.
    """
    contact = (
        await session.execute(
            select(Contact).where(
                Contact.tenant_id == tenant_id,
                Contact.wa_id == staff.wa_id,
            )
        )
    ).scalar_one_or_none()
    if contact is None:
        contact = Contact(
            tenant_id=tenant_id,
            wa_id=staff.wa_id,
            name=staff.nombre,
            contact_type="staff",
            consent_status="granted",
        )
        session.add(contact)
        await session.flush()
    else:
        changed = False
        if contact.contact_type != "staff":
            contact.contact_type = "staff"
            changed = True
        if not contact.name and staff.nombre:
            contact.name = staff.nombre
            changed = True
        if changed:
            await session.flush()
    return contact


async def notify_staff(
    session: AsyncSession,
    tenant_id,
    text: str,
    *,
    roles: tuple[str, ...] = ("owner", "receptionist"),
    exclude_staff_id=None,
    idempotency_key: str | None = None,
    dry_run: bool | None = None,
) -> dict:
    """Envía `text` al staff del tenant con los roles dados.

    `idempotency_key` (opcional) se sufija por miembro: reintentar no
    duplica. Devuelve {str(staff_id): "sent"|"error: ..."}; nunca levanta.
    """
    dry_run = _dry_run_default() if dry_run is None else dry_run
    results: dict = {}
    staff_rows = (
        await session.execute(
            select(StaffMember)
            .where(
                StaffMember.tenant_id == tenant_id,
                StaffMember.role.in_(roles),
            )
            .order_by(StaffMember.nombre)
        )
    ).scalars().all()
    sender = WhatsAppCloudSender(session)
    for s in staff_rows:
        if exclude_staff_id is not None and s.id == exclude_staff_id:
            continue
        try:
            contact = await ensure_staff_contact(session, tenant_id, s)
            key = f"{idempotency_key}:{s.id}" if idempotency_key else None
            result = await sender.send_text(
                str(tenant_id), str(contact.id), s.wa_id, text,
                idempotency_key=key, dry_run=dry_run,
            )
            results[str(s.id)] = (
                "sent" if result.get("status") != "skipped_duplicate"
                else "skipped_duplicate"
            )
            await log_event(
                session, tenant_id, "staff.notified",
                {"staff_id": str(s.id), "role": s.role},
            )
        except Exception as e:  # noqa: BLE001 - un staff no tumba a otros
            logger.exception("Fallo notificando a staff %s", s.id)
            results[str(s.id)] = f"error: {e}"
    await session.commit()
    return results


__all__ = ["ensure_staff_contact", "notify_staff"]
