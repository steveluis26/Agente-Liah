import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, TenantMixin


class Message(Base, TenantMixin):
    __tablename__ = "messages"
    # Deduplicación de webhooks: Meta reintenta entregas; el mismo wamid no
    # debe insertarse dos veces. Índice único parcial: los mensajes sin
    # meta_message_id (p.ej. generados localmente) no participan.
    __table_args__ = (
        Index(
            "uq_messages_meta_message_id",
            "meta_message_id",
            unique=True,
            postgresql_where=text("meta_message_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v4()")
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    direction: Mapped[str] = mapped_column(String(10), nullable=False)  # inbound|outbound
    content: Mapped[str] = mapped_column(Text, nullable=False)
    meta_message_id: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(
        server_default=text("now()"), nullable=False
    )
