import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Integer, Numeric, String, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, TenantMixin


class UsageRecord(Base, TenantMixin):
    """Uso de LLM por turno del agente (una fila por llamada al LLM).

    `conversation_id` es nullable: aún no hay modelo de conversación (llega
    con el panel de Fase 3); hoy se agrupa por tenant/contacto/mes.
    """

    __tablename__ = "usage_records"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v4()")
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    model: Mapped[str] = mapped_column(String(60), nullable=False, default="unknown")
    tokens_in: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        server_default=text("now()"), nullable=False
    )


class UsageMonthly(Base, TenantMixin):
    """Agregado mensual por tenant (lo que ve el panel de costos).

    Lo recalcula `aggregate_monthly_usage()` (upsert idempotente); el cron
    programado que lo invoque llega en Fase 6.
    """

    __tablename__ = "usage_monthly"
    __table_args__ = (
        UniqueConstraint("tenant_id", "year", "month", name="uq_usage_monthly_tenant_ym"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v4()")
    )
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    month: Mapped[int] = mapped_column(Integer, nullable=False)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=text("now()"), onupdate=text("now()"), nullable=False
    )
