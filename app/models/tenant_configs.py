import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, String, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, TenantMixin


class TenantConfig(Base, TenantMixin):
    __tablename__ = "tenant_configs"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    tone: Mapped[str | None] = mapped_column(String(40))
    business_hours: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    handoff_phone: Mapped[str | None] = mapped_column(String(20))
    lfpdp_consent_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    privacy_notice: Mapped[str | None] = mapped_column(Text)
    model_routing: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    extra: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
