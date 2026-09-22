"""Recursos reservables del tenant (Fase 7b).

Un recurso es cualquier cosa que un servicio necesita para realizarse:
una sala (room), un especialista (specialist), un equipo (equipment) o
personal (staff). Los fijos viven en una sede; los móviles se desplazan
con el servicio (negocios de eventos: barras de snacks, espejo mágico).

Los appointment_resources enlazan citas con los recursos concretos
asignados. Cero literales de vertical: room|specialist|equipment|staff
son roles genéricos del motor de agenda.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, TenantMixin

RESOURCE_TYPES = ("room", "specialist", "equipment", "staff")
RESOURCE_MOBILITY = ("fixed", "mobile")


class Resource(Base, TenantMixin):
    """Recurso reservable: sala, especialista, equipo o personal."""

    __tablename__ = "resources"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "slug", name="uq_resources_tenant_slug"
        ),
        CheckConstraint(
            "tipo IN ('room', 'specialist', 'equipment', 'staff')",
            name="ck_resources_tipo",
        ),
        CheckConstraint(
            "movilidad IN ('fixed', 'mobile')",
            name="ck_resources_movilidad",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v4()")
    )
    slug: Mapped[str] = mapped_column(String(60), nullable=False)
    nombre: Mapped[str] = mapped_column(String(120), nullable=False)
    tipo: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # room|specialist|equipment|staff
    movilidad: Mapped[str] = mapped_column(
        String(10), default="fixed", nullable=False
    )  # fixed|mobile
    capacidad: Mapped[int] = mapped_column(
        Integer, default=1, nullable=False
    )
    especialidad: Mapped[str | None] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(
        server_default=text("now()"), nullable=False
    )
