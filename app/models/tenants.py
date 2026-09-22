import uuid
from sqlalchemy import String, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v4()")
    )
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    business_type: Mapped[str] = mapped_column(String(40), nullable=False)
    timezone: Mapped[str] = mapped_column(String(40), nullable=False, default="America/Mexico_City")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    # Fase 8 (soporte/renta): modelo comercial del cliente y referencia de cobro.
    # status: 'active' | 'suspended' | 'trial'. Un tenant no 'active' no
    # procesa mensajes ni campañas (suspensión por falta de pago).
    plan: Mapped[str] = mapped_column(String(40), nullable=False, default="renta")
    billing_ref: Mapped[str | None] = mapped_column(String(120))  # p.ej. ID suscripción MercadoPago
    api_key_hash: Mapped[str | None] = mapped_column(String(64))  # SHA-256 hex
    # Sal aleatoria por clave (hex). Las claves creadas antes de la Fase 1
    # tienen salt NULL y se verifican con el esquema legacy (sin salt).
    api_key_salt: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[str] = mapped_column(
        server_default=text("now()"), nullable=False
    )
