"""Asignación de recursos concretos a una cita (Fase 7b).

Enlaza (cita, recurso): qué sala/especialista/equipo/personal atiende esa
cita. El unique evita asignar dos veces el mismo recurso a la misma cita
(el anti-traslape de recursos entre citas distintas lo hará el engine de
agenda en una fase posterior, usando estos enlaces).
"""
import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, TenantMixin


class AppointmentResource(Base, TenantMixin):
    """Recurso concreto asignado a una cita."""

    __tablename__ = "appointment_resources"
    __table_args__ = (
        UniqueConstraint(
            "appointment_id",
            "resource_id",
            name="uq_appointment_resources_appt_resource",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v4()")
    )
    appointment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("appointments.id", ondelete="CASCADE"), nullable=False
    )
    resource_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("resources.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        server_default=text("now()"), nullable=False
    )
