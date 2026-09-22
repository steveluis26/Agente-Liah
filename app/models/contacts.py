import uuid
from datetime import datetime

from sqlalchemy import Boolean, String, Text, UniqueConstraint, Uuid, text
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
    # Fase 6 — campañas y avisos.
    # `contact_type`: prospect = llegó por el canal y aún no convierte;
    # client = ya agendó/compró (el engine lo promueve al confirmar un
    # book_appointment; ver docs/DECISIONES_FASE6.md).
    contact_type: Mapped[str] = mapped_column(
        String(10), default="prospect", nullable=False
    )  # prospect|client
    # Opt-in de marketing: OBLIGATORIO para recibir campañas Y avisos
    # (sin opt-in es spam y Meta banea el número; ver DECISIONES_FASE6.md).
    marketing_opt_in: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    marketing_opt_in_at: Mapped[datetime | None] = mapped_column()
    marketing_opt_in_source: Mapped[str | None] = mapped_column(
        String(20)
    )  # keyword|panel|import|onboarding
    # Fase 7b: versión de los términos de privacidad que el contacto aceptó
    # desde el primer mensaje (NULL = no aceptada). Se compara contra
    # tenant_privacy_terms.version: una versión nueva exige re-aceptar.
    privacy_terms_version: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(
        server_default=text("now()"), nullable=False
    )
