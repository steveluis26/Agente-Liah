"""Registro de acciones de negocio con clave de idempotencia.

Cada efecto externo (agendar, enviar mensaje, etc.) se registra aquí con una
`idempotency_key` única. Antes de ejecutar la acción, el ejecutor consulta por
la clave: si ya existe, devuelve el resultado guardado sin repetir el efecto.

La unicidad la garantiza la BD (no el proceso), así que funciona con
reintentos, workers paralelos y reinicios.
"""
import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String, Text, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, TenantMixin


class ActionLog(Base, TenantMixin):
    __tablename__ = "action_log"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v4()")
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("contacts.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(
        String(60), nullable=False
    )  # book_appointment|send_message|send_template|...
    idempotency_key: Mapped[str] = mapped_column(
        String(120), nullable=False, unique=True
    )
    status: Mapped[str] = mapped_column(
        String(20), default="ok", nullable=False
    )  # ok|failed
    result: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        server_default=text("now()"), nullable=False
    )
