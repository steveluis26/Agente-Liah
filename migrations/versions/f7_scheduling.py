"""Revision Fase 7b: esquema de configuración (recursos, tipos de servicio, privacidad).

Revision ID: f7_scheduling
Revises: f6_campaigns
Create Date: 2026-09-21

- Tablas nuevas: resources, service_types, tenant_privacy_terms,
  appointment_resources, waitlist_entries.
- appointments: service_type_slug (nullable), venue (nullable).
- contacts: privacy_terms_version (nullable).

Nota: los tests usan Base.metadata.create_all(), no Alembic; esta migración
existe para deploys reales.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision: str = "f7_scheduling"
down_revision: str | None = "f6_campaigns"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ── resources ─────────────────────────────────────────────────────
    op.create_table(
        "resources",
        sa.Column("id", postgresql.UUID(), server_default=sa.text("uuid_generate_v4()"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(), nullable=False),
        sa.Column("slug", sa.String(60), nullable=False),
        sa.Column("nombre", sa.String(120), nullable=False),
        sa.Column("tipo", sa.String(20), nullable=False),
        sa.Column("movilidad", sa.String(10), server_default="fixed", nullable=False),
        sa.Column("capacidad", sa.Integer(), server_default="1", nullable=False),
        sa.Column("especialidad", sa.String(60), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "slug", name="uq_resources_tenant_slug"),
        sa.CheckConstraint("tipo IN ('room', 'specialist', 'equipment', 'staff')", name="ck_resources_tipo"),
        sa.CheckConstraint("movilidad IN ('fixed', 'mobile')", name="ck_resources_movilidad"),
    )
    op.create_index("ix_resources_tenant_id", "resources", ["tenant_id"])

    # ── service_types ─────────────────────────────────────────────────
    op.create_table(
        "service_types",
        sa.Column("id", postgresql.UUID(), server_default=sa.text("uuid_generate_v4()"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(), nullable=False),
        sa.Column("slug", sa.String(60), nullable=False),
        sa.Column("nombre", sa.String(120), nullable=False),
        sa.Column("duracion_min", sa.Integer(), nullable=False),
        sa.Column("recursos_requeridos", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("buffers", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("traslado", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "slug", name="uq_service_types_tenant_slug"),
    )
    op.create_index("ix_service_types_tenant_id", "service_types", ["tenant_id"])

    # ── tenant_privacy_terms ──────────────────────────────────────────
    op.create_table(
        "tenant_privacy_terms",
        sa.Column("tenant_id", postgresql.UUID(), nullable=False),
        sa.Column("version", sa.String(20), nullable=False),
        sa.Column("titulo", sa.String(160), nullable=False),
        sa.Column("texto", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tenant_id"),
    )

    # ── appointment_resources ─────────────────────────────────────────
    op.create_table(
        "appointment_resources",
        sa.Column("id", postgresql.UUID(), server_default=sa.text("uuid_generate_v4()"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(), nullable=False),
        sa.Column("appointment_id", postgresql.UUID(), nullable=False),
        sa.Column("resource_id", postgresql.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["appointment_id"], ["appointments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["resource_id"], ["resources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("appointment_id", "resource_id", name="uq_appointment_resources_appt_resource"),
    )
    op.create_index("ix_appointment_resources_tenant_id", "appointment_resources", ["tenant_id"])

    # ── waitlist_entries ──────────────────────────────────────────────
    op.create_table(
        "waitlist_entries",
        sa.Column("id", postgresql.UUID(), server_default=sa.text("uuid_generate_v4()"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(), nullable=False),
        sa.Column("contact_id", postgresql.UUID(), nullable=False),
        sa.Column("service_type_slug", sa.String(60), nullable=False),
        sa.Column("current_appointment_id", postgresql.UUID(), nullable=True),
        sa.Column("status", sa.String(20), server_default="waiting", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["current_appointment_id"], ["appointments.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("status IN ('waiting', 'offered', 'converted', 'cancelled')", name="ck_waitlist_entries_status"),
    )
    op.create_index("ix_waitlist_entries_tenant_id", "waitlist_entries", ["tenant_id"])

    # ── columnas en tablas existentes ─────────────────────────────────
    op.add_column(
        "appointments",
        sa.Column("service_type_slug", sa.String(60), nullable=True),
    )
    op.add_column(
        "appointments",
        sa.Column("venue", sa.String(120), nullable=True),
    )
    op.add_column(
        "contacts",
        sa.Column("privacy_terms_version", sa.String(20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("contacts", "privacy_terms_version")
    op.drop_column("appointments", "venue")
    op.drop_column("appointments", "service_type_slug")

    op.drop_index("ix_waitlist_entries_tenant_id", table_name="waitlist_entries")
    op.drop_table("waitlist_entries")
    op.drop_index("ix_appointment_resources_tenant_id", table_name="appointment_resources")
    op.drop_table("appointment_resources")
    op.drop_table("tenant_privacy_terms")
    op.drop_index("ix_service_types_tenant_id", table_name="service_types")
    op.drop_table("service_types")
    op.drop_index("ix_resources_tenant_id", table_name="resources")
    op.drop_table("resources")
