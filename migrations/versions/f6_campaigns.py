"""Revision Fase 6: módulo de campañas y avisos.

Revision ID: f6_campaigns
Revises: f4_onboarding
Create Date: 2026-09-21

- Tablas nuevas: contact_tags, campaigns, campaign_sends.
- contacts: contact_type (default 'prospect'), marketing_opt_in (+_at, +_source).
- usage_records: kind (default 'llm'), campaign_id (FK nullable).
- templates: CHECK status IN (pending, approved, rejected) para que el launch
  de campañas solo acepte plantillas aprobadas por Meta.

Nota: los tests usan Base.metadata.create_all(), no Alembic; esta migración
existe para deploys reales.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision: str = "f6_campaigns"
down_revision: str | None = "f4_onboarding"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ── contact_tags ────────────────────────────────────────────────
    op.create_table(
        "contact_tags",
        sa.Column("id", postgresql.UUID(), server_default=sa.text("uuid_generate_v4()"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(), nullable=False),
        sa.Column("contact_id", postgresql.UUID(), nullable=False),
        sa.Column("tag", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "contact_id", "tag", name="uq_contact_tags_tenant_contact_tag"),
    )
    op.create_index("ix_contact_tags_tenant_id", "contact_tags", ["tenant_id"])

    # ── campaigns ───────────────────────────────────────────────────
    op.create_table(
        "campaigns",
        sa.Column("id", postgresql.UUID(), server_default=sa.text("uuid_generate_v4()"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("type", sa.String(10), nullable=False),
        sa.Column("template_id", postgresql.UUID(), nullable=True),
        sa.Column("template_name", sa.String(80), nullable=False),
        sa.Column("params", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("segment", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("status", sa.String(20), server_default="draft", nullable=False),
        sa.Column("scheduled_at", sa.DateTime(), nullable=True),
        sa.Column("launched_at", sa.DateTime(), nullable=True),
        sa.Column("total_targets", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(120), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["template_id"], ["templates.id"], ondelete="SET NULL"),
        sa.CheckConstraint("type IN ('promo', 'notice')", name="ck_campaigns_type"),
        sa.CheckConstraint(
            "status IN ('draft', 'scheduled', 'sending', 'done', 'cancelled')",
            name="ck_campaigns_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_campaigns_tenant_id", "campaigns", ["tenant_id"])

    # ── campaign_sends ──────────────────────────────────────────────
    op.create_table(
        "campaign_sends",
        sa.Column("id", postgresql.UUID(), server_default=sa.text("uuid_generate_v4()"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(), nullable=False),
        sa.Column("contact_id", postgresql.UUID(), nullable=False),
        sa.Column("status", sa.String(20), server_default="queued", nullable=False),
        sa.Column("wamid", sa.String(80), nullable=True),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("campaign_id", "contact_id", name="uq_campaign_sends_campaign_contact"),
        sa.UniqueConstraint("wamid", name="uq_campaign_sends_wamid"),
        sa.CheckConstraint(
            "status IN ('queued', 'sent', 'delivered', 'read', 'failed')",
            name="ck_campaign_sends_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_campaign_sends_tenant_id", "campaign_sends", ["tenant_id"])
    op.create_index("ix_campaign_sends_campaign_id", "campaign_sends", ["campaign_id"])

    # ── contacts: segmentación + opt-in ─────────────────────────────
    op.add_column("contacts", sa.Column("contact_type", sa.String(10), server_default="prospect", nullable=False))
    op.add_column("contacts", sa.Column("marketing_opt_in", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("contacts", sa.Column("marketing_opt_in_at", sa.DateTime(), nullable=True))
    op.add_column("contacts", sa.Column("marketing_opt_in_source", sa.String(20), nullable=True))
    op.create_check_constraint(
        "ck_contacts_contact_type", "contacts", "contact_type IN ('prospect', 'client')"
    )

    # ── usage_records: kind + enlace a campaña ──────────────────────
    op.add_column("usage_records", sa.Column("kind", sa.String(20), server_default="llm", nullable=False))
    op.add_column("usage_records", sa.Column("campaign_id", postgresql.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_usage_records_campaign_id", "usage_records", "campaigns",
        ["campaign_id"], ["id"], ondelete="SET NULL",
    )

    # ── templates: enum de estado de aprobación Meta ────────────────
    op.create_check_constraint(
        "ck_templates_status", "templates", "status IN ('pending', 'approved', 'rejected')"
    )


def downgrade() -> None:
    op.drop_constraint("ck_templates_status", "templates", type_="check")
    op.drop_constraint("fk_usage_records_campaign_id", "usage_records", type_="foreignkey")
    op.drop_column("usage_records", "campaign_id")
    op.drop_column("usage_records", "kind")
    op.drop_constraint("ck_contacts_contact_type", "contacts", type_="check")
    op.drop_column("contacts", "marketing_opt_in_source")
    op.drop_column("contacts", "marketing_opt_in_at")
    op.drop_column("contacts", "marketing_opt_in")
    op.drop_column("contacts", "contact_type")
    op.drop_index("ix_campaign_sends_campaign_id", table_name="campaign_sends")
    op.drop_index("ix_campaign_sends_tenant_id", table_name="campaign_sends")
    op.drop_table("campaign_sends")
    op.drop_index("ix_campaigns_tenant_id", table_name="campaigns")
    op.drop_table("campaigns")
    op.drop_index("ix_contact_tags_tenant_id", table_name="contact_tags")
    op.drop_table("contact_tags")
