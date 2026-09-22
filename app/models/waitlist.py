"""Lista de espera por tipo de servicio (Fase 7b).

Cuando no hay hueco para un service_type, el contacto entra a la waitlist.
El flujo de vida: waiting -> offered (se le ofrece un hueco liberado) ->
converted (acepta y se convierte en cita) | cancelled (rechaza o expira).
`current_appointment_id` enlaza la oferta con la cita creada al convertir.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    String,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, TenantMixin

WAITLIST_STATUSES = ("waiting", "offered", "converted", "cancelled")


class WaitlistEntry(Base, TenantMixin):
    """Una entrada en la lista de espera de un tipo de servicio."""

    __tablename__ = "waitlist_entries"
    __table_args__ = (
        CheckConstraint(
            "status IN ('waiting', 'offered', 'converted', 'cancelled')",
            name="ck_waitlist_entries_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v4()")
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    service_type_slug: Mapped[str] = mapped_column(String(60), nullable=False)
    current_appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("appointments.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(20), default="waiting", nullable=False
    )  # waiting|offered|converted|cancelled
    created_at: Mapped[datetime] = mapped_column(
        server_default=text("now()"), nullable=False
    )
