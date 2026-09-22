"""Tipos de servicio del tenant (Fase 7b).

Un ServiceType es el "producto" que se agenda: duración, qué recursos
requiere (snapshot JSONB del `resources` del perfil: referencias por slug
o por tipo+cantidad+especialidad), buffers de setup/teardown y política
de traslado. El JSONB es un snapshot intencional: si el operador cambia
el perfil después, las citas ya hechas conservan lo que se cotizó.
"""
import uuid
from datetime import datetime

from sqlalchemy import Integer, String, UniqueConstraint, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, TenantMixin


class ServiceType(Base, TenantMixin):
    """Tipo de servicio agendable con sus requerimientos de recursos."""

    __tablename__ = "service_types"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "slug", name="uq_service_types_tenant_slug"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v4()")
    )
    slug: Mapped[str] = mapped_column(String(60), nullable=False)
    nombre: Mapped[str] = mapped_column(String(120), nullable=False)
    duracion_min: Mapped[int] = mapped_column(Integer, nullable=False)
    # Snapshot del `resources` del perfil: lista de dicts {"recurso": slug}
    # o {"tipo": ..., "cantidad": N, "especialidad": ...}.
    recursos_requeridos: Mapped[list] = mapped_column(
        JSONB, default=list, nullable=False
    )
    # {"setup": min, "teardown": min}
    buffers: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    # {"modo": "none"|"fixed"|"per_zone", "fixed_min": ..., "default_min": ...,
    #  "zonas": {...}}
    traslado: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        server_default=text("now()"), nullable=False
    )
