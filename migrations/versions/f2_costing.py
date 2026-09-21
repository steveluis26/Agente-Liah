"""Revision Fase 2: costeo por turno (tablas de uso).

Revision ID: f2_costing
Revises: f1_hardening
Create Date: 2026-09-21

- usage_records: una fila por llamada al LLM (tenant, contacto, modelo,
  tokens in/out, cost_usd).
- usage_monthly: agregado mensual por tenant (lo recalcula
  aggregate_monthly_usage(); lo lee el panel de costos de Fase 3).

Nota: los tests usan Base.metadata.create_all(), no Alembic; esta migración
existe para deploys reales.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision: str = "f2_costing"
down_revision: str | None = "f1_hardening"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "usage_records",
        sa.Column("id", postgresql.UUID(), server_default=sa.text("uuid_generate_v4()"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(), nullable=False),
        sa.Column("contact_id", postgresql.UUID(), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(), nullable=True),
        sa.Column("model", sa.String(60), nullable=False, server_default="unknown"),
        sa.Column("tokens_in", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_out", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_usage_records_tenant_created", "usage_records", ["tenant_id", "created_at"])

    op.create_table(
        "usage_monthly",
        sa.Column("id", postgresql.UUID(), server_default=sa.text("uuid_generate_v4()"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("month", sa.Integer(), nullable=False),
        sa.Column("tokens_in", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_out", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "year", "month", name="uq_usage_monthly_tenant_ym"),
    )


def downgrade() -> None:
    op.drop_table("usage_monthly")
    op.drop_index("ix_usage_records_tenant_created", table_name="usage_records")
    op.drop_table("usage_records")
