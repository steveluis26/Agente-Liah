"""Revision Fase 8 (empaque/soporte): plan de renta y referencia de cobro por tenant.

Revision ID: f8_billing
Revises: f7f_staff
Create Date: 2026-09-21

- tenants.plan: 'compra_unica' | 'renta' (default 'renta'). Define el modelo
  comercial del cliente.
- tenants.billing_ref: referencia externa de cobro (p.ej. ID de suscripción
  de MercadoPago). NULL = cobro manual (SPEI).
- tenants.status ya existía ('active' por defecto); la convención Fase 8 es
  'active' | 'suspended' | 'trial'. El webhook y el emisor de campañas
  ignoran tenants no 'active' (suspensión por falta de pago).

Nota: los tests usan Base.metadata.create_all(), no Alembic; esta migración
existe para deploys reales.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision: str = "f8_billing"
down_revision: str | None = "f7f_staff"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("plan", sa.String(40), nullable=False, server_default="renta"),
    )
    op.add_column(
        "tenants",
        sa.Column("billing_ref", sa.String(120), nullable=True),
    )
    op.create_check_constraint(
        "ck_tenants_plan",
        "tenants",
        "plan IN ('compra_unica', 'renta')",
    )
    op.create_check_constraint(
        "ck_tenants_status",
        "tenants",
        "status IN ('active', 'suspended', 'trial')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_tenants_status", "tenants", type_="check")
    op.drop_constraint("ck_tenants_plan", "tenants", type_="check")
    op.drop_column("tenants", "billing_ref")
    op.drop_column("tenants", "plan")
