import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, TenantMixin


class Appointment(Base, TenantMixin):
    __tablename__ = "appointments"
    # Refuerzo en BD del guard anti-doble-agenda: un tenant no puede tener dos
    # citas que empiecen en el mismo instante. El engine/calendario validan
    # antes, pero la última palabra la tiene este índice (carreras incluidas).
    __table_args__ = (
        Index("uq_appointments_tenant_start", "tenant_id", "start_at", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v4()")
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(String(40), nullable=False)  # consultation|followup|other (genérico; el vertical va en el perfil del tenant)
    calendar_id: Mapped[str | None] = mapped_column(String(80))
    external_event_id: Mapped[str | None] = mapped_column(String(120))
    start_at: Mapped[datetime] = mapped_column(nullable=False)
    end_at: Mapped[datetime | None] = mapped_column()
    status: Mapped[str] = mapped_column(String(20), default="confirmed", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    # Fase 7b: qué tipo de servicio se agenda (slug de service_types; NULL
    # para citas legacy o agendadas sin tipo) y dónde se realiza (sede/
    # ubicación del evento; NULL en negocios fijos).
    service_type_slug: Mapped[str | None] = mapped_column(String(60))
    venue: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(
        server_default=text("now()"), nullable=False
    )
