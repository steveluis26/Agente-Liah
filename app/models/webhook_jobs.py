"""Cola persistente de trabajos del webhook (Fase 1).

El endpoint POST /webhook/whatsapp responde 200 INMEDIATAMENTE y solo
inserta una fila aquí. Un drenador (`app.channels.whatsapp.queue`) procesa
los trabajos pendientes fuera del request, así que un reinicio del proceso
no pierde mensajes (a diferencia de BackgroundTasks in-process).

Estados: pending -> processing -> done | failed.
Un trabajo failed con attempts < MAX_ATTEMPTS puede reprocesarse con el
endpoint POST /webhook/whatsapp/jobs/{id}/reprocess.
"""
import uuid
from datetime import datetime

from sqlalchemy import Integer, String, Text, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, TenantMixin

MAX_ATTEMPTS = 5


class WebhookJob(Base, TenantMixin):
    __tablename__ = "webhook_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v4()")
    )
    # {"phone_number_id": str, "change": <value.* de Meta>}
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default="pending", nullable=False, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        server_default=text("now()"), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column()
