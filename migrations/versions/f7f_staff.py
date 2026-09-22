"""Revision Fase 7f: tabla de personal (staff_members).

Revision ID: f7f_staff
Revises: f7_scheduling
Create Date: 2026-09-21

- Tabla nueva: staff_members (tenant_id, wa_id, nombre, role, resource_id
  nullable -> resources.id). Un wa_id staff jamás entra al flujo de cliente.

Nota: los tests usan Base.metadata.create_all(), no Alembic; esta migración
existe para deploys reales.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision: str = "f7f_staff"
down_revision: str | None = "f7_scheduling"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "staff_members",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuid_generate_v4()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("wa_id", sa.String(40), nullable=False),
        sa.Column("nombre", sa.String(120), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["resource_id"], ["resources.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "wa_id", name="uq_staff_members_tenant_wa_id"),
        sa.CheckConstraint("role IN ('owner', 'specialist', 'receptionist')", name="ck_staff_members_role"),
    )
    op.create_index("ix_staff_members_tenant_id", "staff_members", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_staff_members_tenant_id", table_name="staff_members")
    op.drop_table("staff_members")
