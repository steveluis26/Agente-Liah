"""Adaptadores de calendario (fuente de verdad de disponibilidad/reservas).

MemoryCalendarAdapter: usa la tabla `appointments` del tenant como fuente de
verdad (para dev/local, sin proveedor externo). CalComAdapter: stub listo para
la API real (token por tenant en whatsapp_channels.token_secret_ref
o tenant_configs.extra['calcom_api_key']).

Convención de tiempo: la BD guarda datetimes naive que representan la HORA
LOCAL del tenant (`tz`). Toda interpretación de fecha/hora de entrada se hace
explícitamente en esa zona (zoneinfo), nunca en la zona del servidor.
"""
import logging
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import availability as availmod
from app.agent.ports import AvailabilityResult, BookingResult, CancelResult
from app.agent.staff_notify import notify_staff
from app.agent.waitlist import offer_on_cancel
from app.models import ActionLog, Appointment, AppointmentResource, Contact, ServiceType

logger = logging.getLogger("liah.calendar")

DEFAULT_TZ = "America/Mexico_City"
# Ventana genérica para buscar alternativas (el tenant la refina vía config).
ALT_DAY_START_HOUR = 8
ALT_DAY_END_HOUR = 20
ALT_STEP_MINUTES = 30
ALT_MAX_RESULTS = 3
ALT_LOOKAHEAD_DAYS = 2


class MemoryCalendarAdapter:
    """Calendario persistente vía tabla appointments."""

    def __init__(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        tz: str = DEFAULT_TZ,
    ):
        self.session = session
        self.tenant_id = tenant_id
        self.tz = ZoneInfo(tz)

    # ── helpers ──
    def _parse_start(self, date: str, time_slot: str) -> datetime:
        """Parsea 'YYYY-MM-DD' + 'HH:MM[-HH:MM]' en hora local del tenant.

        Lanza ValueError si el formato es inválido (el llamador lo convierte
        en error de negocio, no en crash).
        """
        start_key = time_slot.split("-")[0].strip()
        naive = datetime.strptime(f"{date} {start_key}", "%Y-%m-%d %H:%M")
        return naive  # naive = hora local del tenant (convención documentada)

    async def _taken_slots(self) -> set[tuple[str, str]]:
        """(fecha, HH:MM) ocupados por citas confirmadas del tenant."""
        result = await self.session.execute(
            select(Appointment).where(
                Appointment.tenant_id == self.tenant_id,
                Appointment.status == "confirmed",
            )
        )
        return {
            (a.start_at.date().isoformat(), a.start_at.strftime("%H:%M"))
            for a in result.scalars().all()
        }

    def _free_alternatives(
        self, date: str, start_key: str, taken: set[tuple[str, str]]
    ) -> list[str]:
        """Slots libres reales cercanos: mismo día ±ventana y días siguientes.

        Todo en hora local del tenant. Devuelve hasta ALT_MAX_RESULTS como
        "YYYY-MM-DD HH:MM".
        """
        base_date = datetime.strptime(date, "%Y-%m-%d").date()
        base_time = datetime.strptime(start_key, "%H:%M").time()
        candidates: list[tuple] = []

        # Mismo día: pasos de 30 min dentro de la ventana, ordenados por
        # cercanía al slot pedido.
        day_start = datetime.combine(base_date, datetime.min.time()).replace(
            hour=ALT_DAY_START_HOUR
        )
        day_end = day_start.replace(hour=ALT_DAY_END_HOUR)
        t = day_start
        while t <= day_end:
            candidates.append((base_date, t.time()))
            t += timedelta(minutes=ALT_STEP_MINUTES)
        # Días siguientes: misma hora.
        for d in range(1, ALT_LOOKAHEAD_DAYS + 1):
            candidates.append((base_date + timedelta(days=d), base_time))

        def _dist(c):
            cdt = datetime.combine(c[0], c[1])
            req = datetime.combine(base_date, base_time)
            day_penalty = 0 if c[0] == base_date else 10_000
            return day_penalty + abs((cdt - req).total_seconds())

        out = []
        for cdate, ctime in sorted(candidates, key=_dist):
            key = (cdate.isoformat(), ctime.strftime("%H:%M"))
            if key == (date, start_key):
                continue
            if key not in taken:
                out.append(f"{key[0]} {key[1]}")
                if len(out) >= ALT_MAX_RESULTS:
                    break
        return out

    # ── puerto ──
    async def check_availability(
        self,
        date: str,
        time_slot: str,
        service_type_slug: str | None = None,
        venue: str | None = None,
        contact_id: str | None = None,
    ) -> AvailabilityResult:
        """Disponibilidad del slot.

        Con `service_type_slug` usa el motor de recursos (Fase 7c:
        capacidad, buffers, traslados, traslape de persona); sin él, el
        comportamiento legacy por slot (los tests viejos llaman sin
        service_type y siguen pasando).
        """
        if service_type_slug:
            check = await availmod.check_resource_availability(
                self.session, self.tenant_id, service_type_slug,
                date, time_slot, venue=venue, contact_id=contact_id,
            )
            return {
                "available": check["available"],
                "alternatives": check["alternatives"],
                "error": None if check["available"] else check["reason"],
            }
        try:
            start_at = self._parse_start(date, time_slot)
        except ValueError:
            return {"available": False, "alternatives": [],
                    "error": "formato inválido: usa YYYY-MM-DD y HH:MM"}
        start_key = start_at.strftime("%H:%M")
        taken = await self._taken_slots()
        if (date, start_key) in taken:
            return {
                "available": False,
                "alternatives": self._free_alternatives(date, start_key, taken),
                "error": None,
            }
        return {"available": True, "alternatives": [], "error": None}

    async def book(
        self,
        contact_id: str,
        date: str,
        time_slot: str,
        appointment_type: str,
        *,
        idempotency_key: str | None = None,
        service_type_slug: str | None = None,
        venue: str | None = None,
    ) -> BookingResult:
        # 1) Idempotencia: reintento con la misma clave -> resultado guardado.
        if idempotency_key:
            prev = await self.session.execute(
                select(ActionLog).where(
                    ActionLog.idempotency_key == idempotency_key
                )
            )
            prev = prev.scalar_one_or_none()
            if prev is not None:
                res = dict(prev.result or {})
                res["ok"] = prev.status == "ok"
                return res  # type: ignore[return-value]

        # 2) Validación de formato (no confiamos en el LLM).
        try:
            start_at = self._parse_start(date, time_slot)
        except ValueError:
            return {"ok": False, "event_id": None, "start_at": None,
                    "error": "formato inválido: usa YYYY-MM-DD y HH:MM"}

        # 2b) Ruta con tipo de servicio: motor de recursos (Fase 7c).
        if service_type_slug:
            return await self._book_with_resources(
                contact_id, date, time_slot, appointment_type, start_at,
                service_type_slug, venue, idempotency_key,
            )

        # 3) Re-validación de disponibilidad DENTRO de la misma transacción:
        #    si el slot se ocupó entre el check y el book (carrera), el
        #    índice único (tenant_id, start_at) lo rechaza y no hay doble
        #    agenda ni en el peor caso.
        appt = Appointment(
            tenant_id=self.tenant_id,
            contact_id=uuid.UUID(contact_id),
            type=appointment_type,
            start_at=start_at,
            status="confirmed",
        )
        self.session.add(appt)
        try:
            await self.session.flush()
        except IntegrityError:
            await self.session.rollback()
            taken = await self._taken_slots()
            return {
                "ok": False,
                "event_id": None,
                "start_at": None,
                "error": "slot ocupado (validación en transacción)",
                "alternatives": self._free_alternatives(
                    date, start_at.strftime("%H:%M"), taken
                ),
            }

        result: BookingResult = {
            "ok": True,
            "event_id": str(appt.id),
            "start_at": start_at.isoformat(),
            "error": None,
        }
        if idempotency_key:
            self.session.add(
                ActionLog(
                    tenant_id=self.tenant_id,
                    contact_id=uuid.UUID(contact_id),
                    action="book_appointment",
                    idempotency_key=idempotency_key,
                    status="ok",
                    result=dict(result),
                )
            )
        await self.session.commit()
        return result

    async def _book_with_resources(
        self,
        contact_id: str,
        date: str,
        time_slot: str,
        appointment_type: str,
        start_at: datetime,
        service_type_slug: str,
        venue: str | None,
        idempotency_key: str | None,
    ) -> BookingResult:
        """Reserva con tipo de servicio: valida con el motor de recursos y
        crea la cita + filas `AppointmentResource` en la misma transacción.

        Conserva idempotencia (ActionLog) y el guard del índice único
        (tenant_id, start_at): dos citas NO pueden compartir el instante
        exacto aunque usen recursos distintos (limitación documentada en
        docs/DECISIONES_FASE7C.md; requiere migración para relajarla).
        """
        st, resolved, err = await availmod.resolve_service_resources(
            self.session, self.tenant_id, service_type_slug
        )
        if err:
            return {"ok": False, "event_id": None, "start_at": None,
                    "error": err}
        check = await availmod.check_resource_availability(
            self.session, self.tenant_id, service_type_slug, date, time_slot,
            venue=venue, contact_id=contact_id,
        )
        if not check["available"]:
            return {
                "ok": False,
                "event_id": None,
                "start_at": None,
                "error": check["reason"],
                "alternatives": check["alternatives"],
            }

        appt = Appointment(
            tenant_id=self.tenant_id,
            contact_id=uuid.UUID(contact_id),
            type=appointment_type,
            start_at=start_at,
            end_at=start_at + timedelta(minutes=st.duracion_min),
            status="confirmed",
            service_type_slug=service_type_slug,
            venue=venue,
        )
        self.session.add(appt)
        try:
            await self.session.flush()
            for resource in resolved:
                self.session.add(
                    AppointmentResource(
                        tenant_id=self.tenant_id,
                        appointment_id=appt.id,
                        resource_id=resource.id,
                    )
                )
            await self.session.flush()
        except IntegrityError:
            await self.session.rollback()
            # Carrera en el guard único: re-chequea para dar alternativas
            # reales del motor de recursos.
            recheck = await availmod.check_resource_availability(
                self.session, self.tenant_id, service_type_slug, date,
                time_slot, venue=venue, contact_id=contact_id,
            )
            return {
                "ok": False,
                "event_id": None,
                "start_at": None,
                "error": "slot ocupado (validación en transacción)",
                "alternatives": recheck["alternatives"],
            }

        result: BookingResult = {
            "ok": True,
            "event_id": str(appt.id),
            "start_at": start_at.isoformat(),
            "error": None,
        }
        if idempotency_key:
            self.session.add(
                ActionLog(
                    tenant_id=self.tenant_id,
                    contact_id=uuid.UUID(contact_id),
                    action="book_appointment",
                    idempotency_key=idempotency_key,
                    status="ok",
                    result=dict(result),
                )
            )
        await self.session.commit()
        return result

    async def cancel(
        self,
        contact_id: str,
        date: str,
        time_slot: str,
        *,
        notify_waitlist: bool = True,
        waitlist_sender=None,
    ) -> CancelResult:
        """Cancela la cita confirmada del contacto en fecha/hora dadas.

        Marca `status="cancelled"` (no borra: queda rastro auditable).
        Devuelve además `freed_slot` (service_type_slug, start_at, venue)
        para la lista de espera. Si la cita tenía tipo de servicio, ofrece
        el hueco liberado al primer contacto en espera que califique
        (Fase 7c; no auto-agenda). Un fallo en la oferta NO revierte la
        cancelación: se reporta en `waitlist_error`.
        """
        try:
            start_at = self._parse_start(date, time_slot)
        except ValueError:
            return {"ok": False, "event_id": None,
                    "error": "formato inválido: usa YYYY-MM-DD y HH:MM"}
        appt = (
            await self.session.execute(
                select(Appointment).where(
                    Appointment.tenant_id == self.tenant_id,
                    Appointment.contact_id == uuid.UUID(contact_id),
                    Appointment.start_at == start_at,
                    Appointment.status == "confirmed",
                )
            )
        ).scalar_one_or_none()
        if appt is None:
            return {
                "ok": False,
                "event_id": None,
                "error": "no hay cita confirmada en ese horario",
            }
        appt.status = "cancelled"
        # Capturar ANTES del commit (expire_on_commit puede vaciar el objeto).
        freed_slot = {
            "service_type_slug": appt.service_type_slug,
            "start_at": appt.start_at.isoformat(),
            "venue": appt.venue,
        }
        freed_dt = appt.start_at
        appt_id = appt.id
        # Fase 7f: datos para la alerta al staff (antes del commit).
        contact = await self.session.get(Contact, appt.contact_id)
        contact_name = (contact.name if contact else None) or "sin nombre"
        servicio_txt = appt.service_type_slug or appt.type
        if appt.service_type_slug:
            st = (
                await self.session.execute(
                    select(ServiceType).where(
                        ServiceType.tenant_id == self.tenant_id,
                        ServiceType.slug == appt.service_type_slug,
                    )
                )
            ).scalar_one_or_none()
            if st is not None:
                servicio_txt = st.nombre
        fecha_txt = appt.start_at.strftime("%d/%m/%Y %H:%M")
        await self.session.commit()
        # Fase 7f: alerta al staff (owner/receptionist). La cancelación ya
        # está commiteada: un fallo notificando jamás la revierte.
        try:
            await notify_staff(
                self.session,
                self.tenant_id,
                f"❌ Cita cancelada: {contact_name} — {fecha_txt} ({servicio_txt}).",
                idempotency_key=f"staff-alert:cancel:{appt_id}",
            )
        except Exception:  # noqa: BLE001 - notificar no revierte cancelar
            logger.exception("Fallo alerta de staff tras cancelar %s", appt_id)
        result: CancelResult = {
            "ok": True,
            "event_id": str(appt_id),
            "error": None,
            "freed_slot": freed_slot,
        }
        if notify_waitlist and freed_slot["service_type_slug"]:
            try:
                result["waitlist"] = await offer_on_cancel(  # type: ignore[typeddict-unknown-key]
                    self.session,
                    self.tenant_id,
                    {
                        "service_type_slug": freed_slot["service_type_slug"],
                        "start_at": freed_dt,
                        "venue": freed_slot["venue"],
                        "exclude_appointment_id": appt_id,
                    },
                    sender=waitlist_sender,
                )
            except Exception as e:  # la cancelación ya es un hecho
                logger.exception(
                    "Fallo oferta de waitlist tras cancelar %s", appt_id
                )
                result["waitlist_error"] = str(e)  # type: ignore[typeddict-unknown-key]
        return result

    async def reschedule(
        self,
        contact_id: str,
        old_date: str,
        old_time_slot: str,
        new_date: str,
        new_time_slot: str,
        appointment_type: str,
        *,
        idempotency_key: str | None = None,
        service_type_slug: str | None = None,
        venue: str | None = None,
    ) -> BookingResult:
        """Reprograma atómicamente: cancela la cita vieja y reserva la nueva.

        Si el nuevo slot está ocupado (o el formato es inválido), la cita
        original se conserva: el rollback revierte también la cancelación.
        Con `service_type_slug`, el nuevo slot se valida con el motor de
        recursos (Fase 7c) y se enlazan los `AppointmentResource`.
        NOTA: el hueco de la cita vieja NO se ofrece a la lista de espera
        aquí (offer_on_cancel hace commit y rompería la atomicidad); la
        oferta ocurre solo en `cancel`.
        """
        if idempotency_key:
            prev = await self.session.execute(
                select(ActionLog).where(
                    ActionLog.idempotency_key == idempotency_key
                )
            )
            prev = prev.scalar_one_or_none()
            if prev is not None:
                res = dict(prev.result or {})
                res["ok"] = prev.status == "ok"
                return res  # type: ignore[return-value]

        try:
            old_start = self._parse_start(old_date, old_time_slot)
            new_start = self._parse_start(new_date, new_time_slot)
        except ValueError:
            return {"ok": False, "event_id": None, "start_at": None,
                    "error": "formato inválido: usa YYYY-MM-DD y HH:MM"}

        appt = (
            await self.session.execute(
                select(Appointment).where(
                    Appointment.tenant_id == self.tenant_id,
                    Appointment.contact_id == uuid.UUID(contact_id),
                    Appointment.start_at == old_start,
                    Appointment.status == "confirmed",
                )
            )
        ).scalar_one_or_none()
        if appt is None:
            return {
                "ok": False,
                "event_id": None,
                "start_at": None,
                "error": "no hay cita confirmada en el horario original",
            }

        # Cancelación sin commit: viaja en la misma transacción que el book.
        # Si el book falla (slot ocupado), el rollback restaura la cita vieja.
        old_event_id = str(appt.id)
        appt.status = "cancelled"
        await self.session.flush()

        if service_type_slug:
            return await self._reschedule_with_resources(
                contact_id, new_date, new_time_slot, appointment_type,
                new_start, service_type_slug, venue, idempotency_key,
                old_event_id, appt.id,
            )

        new_appt = Appointment(
            tenant_id=self.tenant_id,
            contact_id=uuid.UUID(contact_id),
            type=appointment_type,
            start_at=new_start,
            status="confirmed",
        )
        self.session.add(new_appt)
        try:
            await self.session.flush()
        except IntegrityError:
            await self.session.rollback()
            taken = await self._taken_slots()
            return {
                "ok": False,
                "event_id": None,
                "start_at": None,
                "error": "nuevo slot ocupado (la cita original se conserva)",
                "alternatives": self._free_alternatives(
                    new_date, new_start.strftime("%H:%M"), taken
                ),
            }

        result: BookingResult = {
            "ok": True,
            "event_id": str(new_appt.id),
            "start_at": new_start.isoformat(),
            "cancelled_event_id": old_event_id,
            "error": None,
        }
        if idempotency_key:
            self.session.add(
                ActionLog(
                    tenant_id=self.tenant_id,
                    contact_id=uuid.UUID(contact_id),
                    action="reschedule_appointment",
                    idempotency_key=idempotency_key,
                    status="ok",
                    result=dict(result),
                )
            )
        await self.session.commit()
        return result

    async def _reschedule_with_resources(
        self,
        contact_id: str,
        new_date: str,
        new_time_slot: str,
        appointment_type: str,
        new_start: datetime,
        service_type_slug: str,
        venue: str | None,
        idempotency_key: str | None,
        old_event_id: str,
        old_appt_id,
    ) -> BookingResult:
        """Segunda mitad de `reschedule` con tipo de servicio.

        La cita vieja ya está marcada `cancelled` (sin commit). Si el nuevo
        slot no califica, el rollback restaura la cita vieja.
        """
        st, resolved, err = await availmod.resolve_service_resources(
            self.session, self.tenant_id, service_type_slug
        )
        if err:
            await self.session.rollback()
            return {"ok": False, "event_id": None, "start_at": None,
                    "error": err}
        check = await availmod.check_resource_availability(
            self.session, self.tenant_id, service_type_slug, new_date,
            new_time_slot, venue=venue,
            exclude_appointment_id=old_appt_id, contact_id=contact_id,
        )
        if not check["available"]:
            await self.session.rollback()
            return {
                "ok": False,
                "event_id": None,
                "start_at": None,
                "error": check["reason"] + " (la cita original se conserva)",
                "alternatives": check["alternatives"],
            }

        new_appt = Appointment(
            tenant_id=self.tenant_id,
            contact_id=uuid.UUID(contact_id),
            type=appointment_type,
            start_at=new_start,
            end_at=new_start + timedelta(minutes=st.duracion_min),
            status="confirmed",
            service_type_slug=service_type_slug,
            venue=venue,
        )
        self.session.add(new_appt)
        try:
            await self.session.flush()
            for resource in resolved:
                self.session.add(
                    AppointmentResource(
                        tenant_id=self.tenant_id,
                        appointment_id=new_appt.id,
                        resource_id=resource.id,
                    )
                )
            await self.session.flush()
        except IntegrityError:
            await self.session.rollback()
            recheck = await availmod.check_resource_availability(
                self.session, self.tenant_id, service_type_slug, new_date,
                new_time_slot, venue=venue,
                exclude_appointment_id=old_appt_id, contact_id=contact_id,
            )
            return {
                "ok": False,
                "event_id": None,
                "start_at": None,
                "error": "nuevo slot ocupado (la cita original se conserva)",
                "alternatives": recheck["alternatives"],
            }

        result: BookingResult = {
            "ok": True,
            "event_id": str(new_appt.id),
            "start_at": new_start.isoformat(),
            "cancelled_event_id": old_event_id,
            "error": None,
        }
        if idempotency_key:
            self.session.add(
                ActionLog(
                    tenant_id=self.tenant_id,
                    contact_id=uuid.UUID(contact_id),
                    action="reschedule_appointment",
                    idempotency_key=idempotency_key,
                    status="ok",
                    result=dict(result),
                )
            )
        await self.session.commit()
        return result


class CalComAdapter:
    """Stub para la API real de Cal.com.

    La implementación completa consume /v1/slots y /v1/bookings con el API key
    por tenant. Dejamos la firma y el contrato; el cuerpo real se conecta en
    integración con el dashboard de Cal.com del cliente.
    """

    def __init__(self, api_key: str, event_type_id: str | None = None):
        self.api_key = api_key
        self.event_type_id = event_type_id

    async def check_availability(
        self, date: str, time_slot: str,
        service_type_slug: str | None = None,
        venue: str | None = None,
    ) -> AvailabilityResult:
        raise NotImplementedError(
            "CalComAdapter.check_availability: implementar contra GET /v1/slots"
        )

    async def book(
        self,
        contact_id: str,
        date: str,
        time_slot: str,
        appointment_type: str,
        *,
        idempotency_key: str | None = None,
        service_type_slug: str | None = None,
        venue: str | None = None,
    ) -> BookingResult:
        raise NotImplementedError(
            "CalComAdapter.book: implementar contra POST /v1/bookings"
        )

    async def cancel(
        self, contact_id: str, date: str, time_slot: str
    ) -> CancelResult:
        raise NotImplementedError(
            "CalComAdapter.cancel: implementar contra DELETE /v1/bookings"
        )

    async def reschedule(
        self,
        contact_id: str,
        old_date: str,
        old_time_slot: str,
        new_date: str,
        new_time_slot: str,
        appointment_type: str,
        *,
        idempotency_key: str | None = None,
        service_type_slug: str | None = None,
        venue: str | None = None,
    ) -> BookingResult:
        raise NotImplementedError(
            "CalComAdapter.reschedule: implementar contra PATCH /v1/bookings"
        )
