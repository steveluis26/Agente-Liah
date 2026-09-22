import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, String, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, TenantMixin


class AutomationRule(Base, TenantMixin):
    __tablename__ = "automation_rules"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v4()")
    )
    type: Mapped[str] = mapped_column(
        String(40), nullable=False
    )  # appointment_reminder|followup_30d|custom (genérico; legacy: trial_class|colegiatura)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    params: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    last_run_at: Mapped[datetime | None] = mapped_column()


class ReminderLog(Base, TenantMixin):
    __tablename__ = "reminder_log"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v4()")
    )
    rule_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("automation_rules.id", ondelete="CASCADE"), nullable=False
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    template_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("templates.id"), nullable=False
    )
    # Cita origen del recordatorio (Fase 4). NULL para reglas legacy que no
    # son por cita (colegiatura, followup_30d). La idempotencia de
    # dispatch_reminder es (rule, contact, appointment, scheduled_for): dos
    # citas del mismo contacto con el mismo `scheduled_for` calculado ya no
    # se pisan entre sí.
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("appointments.id", ondelete="SET NULL")
    )
    scheduled_for: Mapped[datetime] = mapped_column(nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column()
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    meta_message_id: Mapped[str | None] = mapped_column(String(80))
