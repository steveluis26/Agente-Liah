"""Personal del negocio con acceso de staff por WhatsApp (Fase 7f).

Un `StaffMember` es un número de WhatsApp (wa_id) del tenant que NO es un
cliente: el dueño, un especialista o un recepcionista. Cuando el webhook
recibe un mensaje de un wa_id registrado aquí, el drenador lo rutea al
manejador de staff (`app/agent/staff.py`) ANTES del flujo de cliente y del
consentimiento de privacidad: un número staff jamás recibe el aviso de
privacidad de cliente ni crea un Contact de cliente.

`resource_id` (nullable, FK a resources.id): para role="specialist" enlaza
al staff con su recurso concreto (p.ej. el doctor es el recurso
specialist "dr-x"). Es lo que permite el scoping: un especialista solo ve
su propia agenda. owner/receptionist no necesitan recurso.

Roles genéricos: "owner" | "specialist" | "receptionist". Cero literales de
vertical.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, TenantMixin

STAFF_ROLES = ("owner", "specialist", "receptionist")


class StaffMember(Base, TenantMixin):
    """Número de WhatsApp del personal del negocio."""

    __tablename__ = "staff_members"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "wa_id", name="uq_staff_members_tenant_wa_id"
        ),
        CheckConstraint(
            "role IN ('owner', 'specialist', 'receptionist')",
            name="ck_staff_members_role",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v4()")
    )
    # wa_id de WhatsApp del miembro del staff (con código de país, sin "+").
    wa_id: Mapped[str] = mapped_column(String(40), nullable=False)
    nombre: Mapped[str] = mapped_column(String(120), nullable=False)
    role: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # owner|specialist|receptionist
    # Recurso que representa a este staff en la agenda (solo specialist).
    resource_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("resources.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        server_default=text("now()"), nullable=False
    )
