import uuid
from datetime import datetime

from sqlalchemy import String, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, TenantMixin


class WhatsappChannel(Base, TenantMixin):
    __tablename__ = "whatsapp_channels"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v4()")
    )
    bsp: Mapped[str] = mapped_column(String(20), default="meta", nullable=False)
    waba_id: Mapped[str | None] = mapped_column(String(40))
    phone_number_id: Mapped[str | None] = mapped_column(String(40), unique=True)
    phone_number: Mapped[str | None] = mapped_column(String(20))
    verify_token: Mapped[str | None] = mapped_column(String(80))
    token_secret_ref: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        server_default=text("now()"), nullable=False
    )
