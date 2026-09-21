"""Revision Fase 1: endurecimiento (idempotencia, auditoría, cola).

Revision ID: f1_hardening
Revises: f0_initial
Create Date: 2026-09-21

- tenants.api_key_hash/api_key_salt (f0 no los creaba; el modelo los exige).
- messages: unique parcial en meta_message_id (dedupe de webhooks).
- contacts: unique (tenant_id, wa_id).
- appointments: índice único (tenant_id, start_at) anti-doble-agenda.
- Tablas nuevas: event_log (auditoría append-only), action_log
  (idempotencia), webhook_jobs (cola persistente del webhook).

Nota: los tests usan Base.metadata.create_all(), no Alembic; esta migración
existe para deploys reales.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision: str = "f1_hardening"
down_revision: str | None = "f0_initial"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # 1) Columnas de API key con salt (f0 no las incluía).
    op.add_column("tenants", sa.Column("api_key_hash", sa.String(64), nullable=True))
    op.add_column("tenants", sa.Column("api_key_salt", sa.String(64), nullable=True))

    # 2) Dedupe de webhooks: wamid único donde no sea nulo.
    op.create_index(
        "uq_messages_meta_message_id",
        "messages",
        ["meta_message_id"],
        unique=True,
        postgresql_where=sa.text("meta_message_id IS NOT NULL"),
    )

    # 3) Un contacto por wa_id dentro de cada tenant.
    op.create_unique_constraint(
        "uq_contacts_tenant_wa", "contacts", ["tenant_id", "wa_id"]
    )

    # 4) Anti-doble-agenda a nivel BD.
    op.create_index(
        "uq_appointments_tenant_start",
        "appointments",
        ["tenant_id", "start_at"],
        unique=True,
    )

    # 5) Auditoría append-only.
    op.create_table(
        "event_log",
        sa.Column("id", postgresql.UUID(), server_default=sa.text("uuid_generate_v4()"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(), nullable=False),
        sa.Column("type", sa.String(80), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_event_log_tenant_id", "event_log", ["tenant_id"])
    op.create_index("ix_event_log_type", "event_log", ["type"])

    # 6) Idempotencia de acciones de negocio.
    op.create_table(
        "action_log",
        sa.Column("id", postgresql.UUID(), server_default=sa.text("uuid_generate_v4()"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(), nullable=False),
        sa.Column("contact_id", postgresql.UUID(), nullable=True),
        sa.Column("action", sa.String(60), nullable=False),
        sa.Column("idempotency_key", sa.String(120), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="ok"),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index("ix_action_log_tenant_id", "action_log", ["tenant_id"])

    # 7) Cola persistente del webhook.
    op.create_table(
        "webhook_jobs",
        sa.Column("id", postgresql.UUID(), server_default=sa.text("uuid_generate_v4()"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_webhook_jobs_tenant_id", "webhook_jobs", ["tenant_id"])
    op.create_index("ix_webhook_jobs_status", "webhook_jobs", ["status"])


def downgrade() -> None:
    op.drop_index("ix_webhook_jobs_status", table_name="webhook_jobs")
    op.drop_index("ix_webhook_jobs_tenant_id", table_name="webhook_jobs")
    op.drop_table("webhook_jobs")
    op.drop_index("ix_action_log_tenant_id", table_name="action_log")
    op.drop_table("action_log")
    op.drop_index("ix_event_log_type", table_name="event_log")
    op.drop_index("ix_event_log_tenant_id", table_name="event_log")
    op.drop_table("event_log")
    op.drop_index("uq_appointments_tenant_start", table_name="appointments")
    op.drop_constraint("uq_contacts_tenant_wa", "contacts", type_="unique")
    op.drop_index("uq_messages_meta_message_id", table_name="messages")
    op.drop_column("tenants", "api_key_salt")
    op.drop_column("tenants", "api_key_hash")
