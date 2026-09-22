"""Lista de espera: alta y oferta ante cancelaciones (Fase 7c).

- `join_waitlist()`: el contacto entra a esperar un `service_type_slug`.
  Idempotente: si ya tiene una entrada `waiting` para ese tipo, se devuelve
  la existente (no se duplica).
- `offer_on_cancel()`: ante un hueco liberado (cancelación), busca las
  entradas `waiting` del mismo tipo de servicio ordenadas por antigüedad;
  para cada una verifica disponibilidad real con el motor de recursos
  (Fase 7c); a la PRIMERA que califique se le marca `offered` y se le avisa
  por el canal. NO auto-agenda: la conversión ocurre cuando el contacto
  confirma (p.ej. vía `reschedule_appointment`).

El envío usa el contrato `SenderPort`; si no se inyecta sender se construye
el de WhatsApp con dry_run según `LIAH_SEND_DRY_RUN` (default: dry-run, no
se toca Meta en dev/tests).
"""
import logging
import os
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import availability as availmod
from app.agent.ports import SenderPort
from app.agent.staff_notify import notify_staff
from app.core.audit import log_event
from app.models import Contact, ServiceType, WaitlistEntry

logger = logging.getLogger("liah.waitlist")


def _dry_run_default() -> bool:
    return os.getenv("LIAH_SEND_DRY_RUN", "1") == "1"


async def join_waitlist(
    session: AsyncSession,
    tenant_id,
    contact_id,
    service_type_slug: str,
) -> WaitlistEntry:
    """Da de alta al contacto en la lista de espera (idempotente)."""
    existing = (
        await session.execute(
            select(WaitlistEntry).where(
                WaitlistEntry.tenant_id == tenant_id,
                WaitlistEntry.contact_id == contact_id,
                WaitlistEntry.service_type_slug == service_type_slug,
                WaitlistEntry.status == "waiting",
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    entry = WaitlistEntry(
        tenant_id=tenant_id,
        contact_id=contact_id,
        service_type_slug=service_type_slug,
        status="waiting",
    )
    session.add(entry)
    await session.flush()
    await log_event(
        session, tenant_id, "waitlist.joined",
        {"contact_id": str(contact_id),
         "service_type_slug": service_type_slug},
    )
    return entry


def _freed_start(freed: dict) -> datetime:
    start_at = freed.get("start_at")
    if isinstance(start_at, datetime):
        return start_at
    return datetime.fromisoformat(str(start_at))


async def offer_on_cancel(
    session: AsyncSession,
    tenant_id,
    freed: dict,
    sender: SenderPort | None = None,
    dry_run: bool | None = None,
) -> dict:
    """Ofrece el hueco liberado al primer contacto en espera que califique.

    `freed` = {"service_type_slug": str, "start_at": datetime|ISO,
               "venue": str|None, "exclude_appointment_id": UUID|None}.
    Devuelve {"offered": bool, "contact_id": str|None,
              "waitlist_entry_id": str|None, "reason": str|None}.
    """
    dry_run = _dry_run_default() if dry_run is None else dry_run
    result = {"offered": False, "contact_id": None,
              "waitlist_entry_id": None, "reason": None}

    service_type_slug = (freed or {}).get("service_type_slug")
    if not service_type_slug:
        result["reason"] = "el hueco liberado no tiene tipo de servicio"
        return result
    try:
        start_at = _freed_start(freed)
    except (ValueError, TypeError):
        result["reason"] = "fecha del hueco liberado inválida"
        return result

    st = (
        await session.execute(
            select(ServiceType).where(
                ServiceType.tenant_id == tenant_id,
                ServiceType.slug == service_type_slug,
            )
        )
    ).scalar_one_or_none()

    entries = (
        await session.execute(
            select(WaitlistEntry).where(
                WaitlistEntry.tenant_id == tenant_id,
                WaitlistEntry.service_type_slug == service_type_slug,
                WaitlistEntry.status == "waiting",
            ).order_by(WaitlistEntry.created_at.asc())
        )
    ).scalars().all()
    if not entries:
        result["reason"] = "lista de espera vacía para ese tipo de servicio"
        return result

    date = start_at.date().isoformat()
    hhmm = start_at.strftime("%H:%M")
    venue = freed.get("venue")
    exclude_id = freed.get("exclude_appointment_id")

    for entry in entries:
        check = await availmod.check_resource_availability(
            session, tenant_id, service_type_slug, date, hhmm,
            venue=venue, exclude_appointment_id=exclude_id,
            contact_id=entry.contact_id,
        )
        if not check["available"]:
            logger.info(
                "Waitlist: entrada %s no califica para el hueco (%s)",
                entry.id, check.get("reason"),
            )
            continue
        # Califica: marcar offered + avisar. NO se agenda solo.
        entry.status = "offered"
        contact = await session.get(Contact, entry.contact_id)
        wa_id = contact.wa_id if contact else None
        offered_name = (contact.name if contact else None) or "sin nombre"
        nombre_servicio = st.nombre if st else service_type_slug
        fecha_txt = start_at.strftime("%d/%m/%Y")
        text = (
            f"¡Buenas noticias! Se liberó un lugar para {nombre_servicio} "
            f"el {fecha_txt} a las {hhmm}"
            + (f" en {venue}" if venue else "")
            + ". ¿Lo tomas? Responde a este mensaje para confirmar."
        )
        if sender is None:
            from app.agent.sender import WhatsAppCloudSender
            sender = WhatsAppCloudSender(session)
        if wa_id:
            await sender.send_text(
                str(tenant_id), str(entry.contact_id), wa_id, text,
                idempotency_key=(
                    f"waitlist-offer:{entry.id}:{date}-{hhmm}"
                ),
                dry_run=dry_run,
            )
        await log_event(
            session, tenant_id, "waitlist.offered",
            {"waitlist_entry_id": str(entry.id),
             "contact_id": str(entry.contact_id),
             "service_type_slug": service_type_slug,
             "start_at": start_at.isoformat()},
        )
        await session.commit()
        logger.info("Waitlist: hueco ofrecido a contacto %s", entry.contact_id)
        # Fase 7f: alerta al staff ("hueco liberado"). La oferta ya está
        # commiteada: notificar jamás la revierte.
        try:
            await notify_staff(
                session,
                tenant_id,
                f"🟢 Hueco liberado: {nombre_servicio} el {fecha_txt} a las "
                f"{hhmm}" + (f" en {venue}" if venue else "") +
                f" — ofrecido a {offered_name}.",
                idempotency_key=f"staff-alert:waitlist-offer:{entry.id}",
            )
        except Exception:  # noqa: BLE001 - notificar no revierte la oferta
            logger.exception("Fallo alerta de staff tras oferta de waitlist")
        result.update({
            "offered": True,
            "contact_id": str(entry.contact_id),
            "waitlist_entry_id": str(entry.id),
        })
        return result

    result["reason"] = (
        "ningún contacto en espera califica para el hueco liberado"
    )
    return result
