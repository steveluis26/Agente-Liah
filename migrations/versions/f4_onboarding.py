"""Revision Fase 4: alta por perfil declarativo + idempotencia por cita.

Revision ID: f4_onboarding
Revises: f3_panel
Create Date: 2026-09-21

- reminder_log.appointment_id (UUID nullable, FK a appointments con
  ON DELETE SET NULL): la idempotencia de recordatorios pasa a ser
  (rule, contact, appointment, scheduled_for). Sin esta columna, dos citas
  del mismo contacto que calculen el mismo `scheduled_for` se pisan entre
  sí (el segundo recordatorio se suprime como duplicado).

Nota: los tests usan Base.metadata.create_all(), no Alembic; esta migración
existe para deploys reales. No hay tablas nuevas en Fase 4: tenant,
tenant_configs, automation_rules, templates, knowledge_* y platform_users
ya existían y el onboarding los reutiliza.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision: str = "f4_onboarding"
down_revision: str | None = "f3_panel"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "reminder_log",
        sa.Column("appointment_id", postgresql.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_reminder_log_appointment_id",
        "reminder_log",
        "appointments",
        ["appointment_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_reminder_log_appointment_id", "reminder_log", type_="foreignkey"
    )
    op.drop_column("reminder_log", "appointment_id")
