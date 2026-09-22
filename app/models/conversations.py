"""Conversaciones por contacto (Fase 3: panel mínimo).

Una conversación agrupa el intercambio bot↔contacto entre "episodios": se crea
(o reutiliza) por contacto en el drenador, el handoff la pone en `human`,
"devolver al bot" la regresa a `ai`, y resolverla la marca `resolved`
(cierra el episodio para métricas).

Semántica de `mode`:
- `ai`: el bot responde con normalidad.
- `human`: hay un handoff abierto; el drenador silencia al bot.
- `resolved`: episodio cerrado (para métricas); el próximo mensaje inbound
  abre una conversación nueva en `ai`.

Las transiciones las hacen el engine (`_create_handoff`), el drenador y los
endpoints del panel; nunca se editan a mano.
"""
import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, Uuid, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, TenantMixin

# Modos válidos. No usar Enum de PG: el esquema usa Strings cortos en todas
# las tablas y los tests hacen drop_all/create_all.
MODE_AI = "ai"
MODE_HUMAN = "human"
MODE_RESOLVED = "resolved"
VALID_MODES = (MODE_AI, MODE_HUMAN, MODE_RESOLVED)


class Conversation(Base, TenantMixin):
    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_contact", "tenant_id", "contact_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v4()")
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[str] = mapped_column(
        String(20), default="whatsapp", nullable=False
    )  # whatsapp | instagram | facebook (futuro, vía ChannelAdapter)
    mode: Mapped[str] = mapped_column(String(10), default=MODE_AI, nullable=False)
    opened_at: Mapped[datetime] = mapped_column(
        server_default=text("now()"), nullable=False
    )
    closed_at: Mapped[datetime | None] = mapped_column()


async def get_or_create_conversation(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    contact_id: uuid.UUID,
    channel: str = "whatsapp",
) -> Conversation:
    """Devuelve la conversación abierta del contacto o crea una nueva.

    Un episodio `resolved` no se reutiliza: el próximo mensaje abre uno nuevo
    (así las métricas de resolución automática cuadran por episodio).
    """
    from sqlalchemy import select  # import tardío: evita ciclos

    conv = (
        await session.execute(
            select(Conversation)
            .where(
                Conversation.tenant_id == tenant_id,
                Conversation.contact_id == contact_id,
                Conversation.mode != MODE_RESOLVED,
            )
            .order_by(Conversation.opened_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if conv is None:
        conv = Conversation(
            tenant_id=tenant_id,
            contact_id=contact_id,
            channel=channel,
            mode=MODE_AI,
        )
        session.add(conv)
        await session.flush()
    return conv


async def set_conversation_mode(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    contact_id: uuid.UUID,
    mode: str,
    channel: str = "whatsapp",
) -> Conversation:
    """Transición de modo (valida el valor). No hace commit."""
    if mode not in VALID_MODES:
        raise ValueError(f"mode inválido: {mode!r} (válidos: {VALID_MODES})")
    conv = await get_or_create_conversation(session, tenant_id, contact_id, channel)
    conv.mode = mode
    if mode == MODE_RESOLVED:
        from datetime import timezone

        conv.closed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    else:
        conv.closed_at = None
    await session.flush()
    return conv
