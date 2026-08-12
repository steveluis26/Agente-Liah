"""Adaptadores de calendario (fuente de verdad de disponibilidad/reservas).

MemoryCalendarAdapter: usa la tabla `appointments` del tenant como fuente de
verdad (para dev/local, sin proveedor externo). CalComAdapter: stub listo para
la API real de la academia (token por tenant en whatsapp_channels.token_secret_ref
o tenant_configs.extra['calcom_api_key']).
"""
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Appointment


class MemoryCalendarAdapter:
    """Calendario en-memoria persistente vía tabla appointments."""

    def __init__(self, session: AsyncSession, tenant_id: uuid.UUID):
        self.session = session
        self.tenant_id = tenant_id

    async def check_availability(self, date: str, time_slot: str) -> dict:
        # time_slot esperado "HH:MM" o "HH:MM-HH:MM"; usamos inicio.
        start_key = time_slot.split("-")[0].strip()
        # Busca conflicto exacto (mismo día + misma hora de inicio).
        result = await self.session.execute(
            select(Appointment).where(
                Appointment.tenant_id == self.tenant_id,
                Appointment.status == "confirmed",
            )
        )
        taken = {
            (a.start_at.date().isoformat(), a.start_at.strftime("%H:%M"))
            for a in result.scalars().all()
        }
        slot = f"{date} {start_key}"
        available = (date, start_key) not in taken
        return {"available": available, "alternatives": []}

    async def book(
        self,
        contact_id: str,
        date: str,
        time_slot: str,
        appointment_type: str,
    ) -> dict:
        start_key = time_slot.split("-")[0].strip()
        try:
            start_at = datetime.fromisoformat(f"{date}T{start_key}:00")
        except ValueError:
            return {"ok": False, "event_id": None, "start_at": None}
        appt = Appointment(
            tenant_id=self.tenant_id,
            contact_id=uuid.UUID(contact_id),
            type=appointment_type,
            start_at=start_at,
            status="confirmed",
        )
        self.session.add(appt)
        await self.session.commit()
        await self.session.refresh(appt)
        return {"ok": True, "event_id": str(appt.id), "start_at": start_at.isoformat()}


class CalComAdapter:
    """Stub para la API real de Cal.com (Fase 1 MVP academia).

    La implementación completa consume /v1/slots y /v1/bookings con el API key
    por tenant. Dejamos la firma y el contrato; el cuerpo real se conecta en
    integración con el dashboard de Cal.com del cliente.
    """

    def __init__(self, api_key: str, event_type_id: str | None = None):
        self.api_key = api_key
        self.event_type_id = event_type_id

    async def check_availability(self, date: str, time_slot: str) -> dict:
        raise NotImplementedError(
            "CalComAdapter.check_availability: implementar contra GET /v1/slots"
        )

    async def book(
        self, contact_id: str, date: str, time_slot: str, appointment_type: str
    ) -> dict:
        raise NotImplementedError(
            "CalComAdapter.book: implementar contra POST /v1/bookings"
        )
