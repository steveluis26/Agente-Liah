"""Declarative base + mixin multi-tenant.

TenantMixin impone tenant_id en cada tabla de negocio. El CRUD (app/crud)
siempre filtra/inserta por el tenant_id del contexto (tenant_ctx).
"""
import uuid

from sqlalchemy import ForeignKey, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TenantMixin:
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
