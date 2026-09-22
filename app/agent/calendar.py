"""Adaptadores de calendario (fuente de verdad de disponibilidad/reservas).

MemoryCalendarAdapter: usa la tabla `appointments` del tenant como fuente de
verdad (para dev/local, sin proveedor externo). CalComAdapter: stub listo para
la API real (token por tenant en whatsapp_channels.token_secret_ref
o tenant_configs.extra['calcom_api_key']).

Convención de tiempo: la BD guarda datetimes naive que representan la HORA
LOCAL del tenant (`tz`). Toda interpretación de fecha/hora de entrada se hace
explícitamente en esa zona (zoneinfo), nunca en la zona del servidor.
"""
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.ports import AvailabilityResult, BookingResult, CancelResult
from app.models import ActionLog, Appointment

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
    async def check_availability(self, date: str, time_slot: str) -> AvailabilityResult:
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

    async def cancel(
        self, contact_id: str, date: str, time_slot: str
    ) -> CancelResult:
        """Cancela la cita confirmada del contacto en fecha/hora dadas.

        Marca `status="cancelled"` (no borra: queda rastro auditable).
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
        await self.session.commit()
        return {"ok": True, "event_id": str(appt.id), "error": None}

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
    ) -> BookingResult:
        """Reprograma atómicamente: cancela la cita vieja y reserva la nueva.

        Si el nuevo slot está ocupado (o el formato es inválido), la cita
        original se conserva: el rollback revierte también la cancelación.
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


class CalComAdapter:
    """Stub para la API real de Cal.com.

    La implementación completa consume /v1/slots y /v1/bookings con el API key
    por tenant. Dejamos la firma y el contrato; el cuerpo real se conecta en
    integración con el dashboard de Cal.com del cliente.
    """

    def __init__(self, api_key: str, event_type_id: str | None = None):
        self.api_key = api_key
        self.event_type_id = event_type_id

    async def check_availability(self, date: str, time_slot: str) -> AvailabilityResult:
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
    ) -> BookingResult:
        raise NotImplementedError(
            "CalComAdapter.reschedule: implementar contra PATCH /v1/bookings"
        )
