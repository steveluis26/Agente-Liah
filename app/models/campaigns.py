"""Modelos del módulo de campañas y avisos (Fase 6).

- `ContactTag`: tags libres por contacto para segmentación de campañas.
- `Campaign`: campaña (promo|notice) con segmento, plantilla y estado de
  ciclo de vida draft → scheduled → sending → done|cancelled.
- `CampaignSend`: un envío por (campaña, contacto). `wamid` enlaza con los
  `statuses` del webhook de Meta para delivered/read.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, TenantMixin

CAMPAIGN_TYPES = ("promo", "notice")
CAMPAIGN_STATUSES = ("draft", "scheduled", "sending", "done", "cancelled")
CAMPAIGN_SEND_STATUSES = ("queued", "sent", "delivered", "read", "failed")

# Orden parcial de progreso de un envío (para no degradar por statuses
# fuera de orden de Meta): read > delivered > sent. queued/failed son
# estados gestionados por el dispatch, no por el webhook.
_SEND_PROGRESS = {"sent": 1, "delivered": 2, "read": 3}


class ContactTag(Base, TenantMixin):
    """Tag de segmentación por contacto (p.ej. "vip", "moroso", "pediatria").

    Único por (tenant, contacto, tag): re-etiquetar no duplica.
    """

    __tablename__ = "contact_tags"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "contact_id", "tag", name="uq_contact_tags_tenant_contact_tag"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v4()")
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    tag: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        server_default=text("now()"), nullable=False
    )


class Campaign(Base, TenantMixin):
    """Campaña de marketing (promo) o aviso institucional (notice).

    El segmento es un dict JSONB: {"contact_type": "client"|"prospect",
    "tags": ["vip", ...]}. Los tags se evalúan con OR (el contacto califica
    si tiene AL MENOS UNO; ver docs/DECISIONES_FASE6.md).

    REGLA DURA (anti-spam/anti-baneo): el dispatch solo incluye contactos
    con marketing_opt_in=true, SIN excepciones ni siquiera para avisos.
    """

    __tablename__ = "campaigns"
    __table_args__ = (
        CheckConstraint(
            "type IN ('promo', 'notice')", name="ck_campaigns_type"
        ),
        CheckConstraint(
            "status IN ('draft', 'scheduled', 'sending', 'done', 'cancelled')",
            name="ck_campaigns_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v4()")
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    type: Mapped[str] = mapped_column(String(10), nullable=False)  # promo|notice
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("templates.id", ondelete="SET NULL"), nullable=True
    )
    # Nombre de la plantilla tal como se lanzó (snapshot: la plantilla
    # puede renombrarse o borrarse después sin perder la auditoría).
    template_name: Mapped[str] = mapped_column(String(80), nullable=False)
    params: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    segment: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default="draft", nullable=False
    )  # draft|scheduled|sending|done|cancelled
    scheduled_at: Mapped[datetime | None] = mapped_column()
    launched_at: Mapped[datetime | None] = mapped_column()
    total_targets: Mapped[int] = mapped_column(default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(
        server_default=text("now()"), nullable=False
    )


class CampaignSend(Base, TenantMixin):
    """Un envío de campaña a un contacto.

    `unique(campaign_id, contact_id)`: jamás duplicar un envío aunque el
    dispatch se re-ejecute. `wamid` (unique, parcial donde no nulo) enlaza
    con los statuses del webhook → delivered/read.
    """

    __tablename__ = "campaign_sends"
    __table_args__ = (
        UniqueConstraint(
            "campaign_id", "contact_id", name="uq_campaign_sends_campaign_contact"
        ),
        UniqueConstraint("wamid", name="uq_campaign_sends_wamid"),
        CheckConstraint(
            "status IN ('queued', 'sent', 'delivered', 'read', 'failed')",
            name="ck_campaign_sends_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v4()")
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(20), default="queued", nullable=False
    )  # queued|sent|delivered|read|failed
    wamid: Mapped[str | None] = mapped_column(String(80))
    sent_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(
        server_default=text("now()"), nullable=False
    )


def send_progress(status: str) -> int:
    """Nivel de progreso de un estado de envío (para no degradar)."""
    return _SEND_PROGRESS.get(status, 0)
