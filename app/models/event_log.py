"""Auditoría append-only de eventos del sistema.

Convención dura: a esta tabla solo se INSERTA. Nunca UPDATE ni DELETE
(retención/borrado, si algún día se necesita, se hace por partición o
archivado fuera de la app, no con deletes ad-hoc).

Eventos típicos (campo `type`):
  webhook.received | webhook.statuses | webhook.unknown_channel |
  message.processed | message.duplicate_skipped | message.unsupported |
  agent.replied | agent.escalated | send.failed | job.failed | job.done
"""
import uuid
from datetime import datetime

from sqlalchemy import String, Text, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, TenantMixin


class EventLog(Base, TenantMixin):
    __tablename__ = "event_log"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v4()")
    )
    type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        server_default=text("now()"), nullable=False
    )
