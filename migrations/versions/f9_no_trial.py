"""Revision Fase 8b: sin fase de prueba + producto contratado.

Revision ID: f9_no_trial
Revises: f8_billing
Create Date: 2026-09-22

- Elimina 'trial' del status de tenants: se compra o se renta, sin periodo
  de prueba. Filas legacy en 'trial' (si las hubiera) pasan a 'suspended'.
- Agrega tenants.product: 'chatbot' (solo el chatbot) o 'paquete_completo'
  (chatbot + CRM administrado). Default 'paquete_completo'.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision: str = "f9_no_trial"
down_revision: str | None = "f8_billing"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Seguridad: ningún trial queda huérfano antes de apretar el constraint.
    op.execute("UPDATE tenants SET status = 'suspended' WHERE status = 'trial'")
    op.drop_constraint("ck_tenants_status", "tenants", type_="check")
    op.create_check_constraint(
        "ck_tenants_status",
        "tenants",
        "status IN ('active', 'suspended')",
    )
    op.add_column(
        "tenants",
        sa.Column(
            "product", sa.String(40), nullable=False,
            server_default="paquete_completo",
        ),
    )
    op.create_check_constraint(
        "ck_tenants_product",
        "tenants",
        "product IN ('chatbot', 'paquete_completo')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_tenants_product", "tenants", type_="check")
    op.drop_column("tenants", "product")
    op.drop_constraint("ck_tenants_status", "tenants", type_="check")
    op.create_check_constraint(
        "ck_tenants_status",
        "tenants",
        "status IN ('active', 'suspended', 'trial')",
    )
