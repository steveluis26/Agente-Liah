import uuid
from datetime import datetime

from sqlalchemy import String, Text, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, TenantMixin


class Contact(Base, TenantMixin):
    __tablename__ = "contacts"
    # Un wa_id identifica un contacto dentro de un tenant; el constraint evita
    # duplicados por condiciones de carrera en el webhook.
    __table_args__ = (
        UniqueConstraint("tenant_id", "wa_id", name="uq_contacts_tenant_wa"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v4()")
    )
    wa_id: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str | None] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(20))
    consent_status: Mapped[str] = mapped_column(
        String(20), default="none", nullable=False
    )  # none|pending|granted|revoked
    consent_at: Mapped[datetime | None] = mapped_column()
    last_interaction_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(
        server_default=text("now()"), nullable=False
    )
