"""Términos de privacidad del tenant (Fase 7b).

Un solo texto vigente por tenant (tenant_id es PK): el contacto los acepta
desde el primer mensaje y la versión aceptada queda en
`contacts.privacy_terms_version` (NULL = no aceptada). Si el tenant publica
una versión nueva, los contactos que ya aceptaron una versión anterior deben
re-aceptar: la comparación es por versión, no por booleano.
"""
import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base


class TenantPrivacyTerms(Base):
    """Texto vigente de términos de privacidad/aviso de privacidad."""

    __tablename__ = "tenant_privacy_terms"

    # PK a la vez que FK: un tenant tiene como máximo UN texto vigente.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        primary_key=True,
    )
    version: Mapped[str] = mapped_column(String(20), nullable=False)
    titulo: Mapped[str] = mapped_column(String(160), nullable=False)
    texto: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        server_default=text("now()"), nullable=False
    )
