"""Revision Fase 3: panel mínimo (conversaciones + auth de operadores).

Revision ID: f3_panel
Revises: f2_costing
Create Date: 2026-09-21

- conversations: episodio por contacto (mode ai|human|resolved); el drenador
  la crea/reutiliza, el handoff la pone en human, "devolver al bot" en ai.
- platform_users: password_hash (PBKDF2, nullable para no romper filas
  existentes), tenant_id (NULL = platform_admin), y migración de datos del
  rol legacy 'owner' -> 'platform_admin'.
- handoffs.resolution_note: nota del operador al resolver desde la bandeja.

Nota: los tests usan Base.metadata.create_all(), no Alembic; esta migración
existe para deploys reales.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision: str = "f3_panel"
down_revision: str | None = "f2_costing"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(), server_default=sa.text("uuid_generate_v4()"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(), nullable=False),
        sa.Column("contact_id", postgresql.UUID(), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False, server_default="whatsapp"),
        sa.Column("mode", sa.String(10), nullable=False, server_default="ai"),
        sa.Column("opened_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_conversations_tenant_id", "conversations", ["tenant_id"])
    op.create_index("ix_conversations_contact", "conversations", ["tenant_id", "contact_id"])

    op.add_column("platform_users", sa.Column("password_hash", sa.String(256), nullable=True))
    op.add_column("platform_users", sa.Column("tenant_id", postgresql.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_platform_users_tenant_id", "platform_users", "tenants",
        ["tenant_id"], ["id"], ondelete="SET NULL",
    )
    # El default legacy 'owner' (Fase 0) pasa a ser 'platform_admin'.
    op.execute(
        sa.text("UPDATE platform_users SET role = 'platform_admin' WHERE role = 'owner'")
    )

    op.add_column("handoffs", sa.Column("resolution_note", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("handoffs", "resolution_note")
    op.execute(
        sa.text("UPDATE platform_users SET role = 'owner' WHERE role = 'platform_admin'")
    )
    op.drop_constraint("fk_platform_users_tenant_id", "platform_users", type_="foreignkey")
    op.drop_column("platform_users", "tenant_id")
    op.drop_column("platform_users", "password_hash")
    op.drop_index("ix_conversations_contact", table_name="conversations")
    op.drop_index("ix_conversations_tenant_id", table_name="conversations")
    op.drop_table("conversations")
