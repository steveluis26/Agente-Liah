"""Auditoría: helper para registrar eventos append-only en event_log.

Regla: los eventos se registran, nunca se modifican. Úsalo para trazar el
ciclo de vida de un mensaje/acción (recibido → procesado → respondido) sin
meter lógica de negocio en los modelos.
"""
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event_log import EventLog


async def log_event(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    type: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Inserta un evento de auditoría (sin commit: lo hace el llamador)."""
    session.add(EventLog(tenant_id=tenant_id, type=type, payload=payload or {}))
