"""Fase 8 — modelo comercial por tenant (soporte/renta).

- plan: 'compra_unica' (el cliente asume su consumo de OpenAI/Meta) o
  'renta' (Steve hospeda y absorbe costos dentro de la mensualidad).
- status: 'active' | 'suspended' | 'trial'. Solo 'active' procesa mensajes
  y campañas: suspender = cortar el servicio por falta de pago.
"""
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

PLAN_COMPRA_UNICA = "compra_unica"
PLAN_RENTA = "renta"
VALID_PLANS = (PLAN_COMPRA_UNICA, PLAN_RENTA)

STATUS_ACTIVE = "active"
STATUS_SUSPENDED = "suspended"
STATUS_TRIAL = "trial"
VALID_STATUSES = (STATUS_ACTIVE, STATUS_SUSPENDED, STATUS_TRIAL)


async def get_tenant_status(session: AsyncSession, tenant_id: uuid.UUID) -> str | None:
    from app.models.tenants import Tenant

    tenant = await session.get(Tenant, tenant_id)
    return tenant.status if tenant else None


async def is_tenant_active(session: AsyncSession, tenant_id: uuid.UUID) -> bool:
    """True solo si el tenant existe y su status es 'active'."""
    return await get_tenant_status(session, tenant_id) == STATUS_ACTIVE
