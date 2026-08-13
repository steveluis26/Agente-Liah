"""Autenticación del panel por API Key (X-Tenant-API-Key).

Al crear un tenant se genera una clave (liah_live_sk_...) que se muestra una
sola vez; en BD solo se guarda su hash SHA-256. La dependencia resuelve el
tenant por el hash del header y lo inyecta en tenant_ctx.
"""
import hashlib
import hmac
import os
import secrets
import uuid

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base import Base, TenantMixin
from app.core.db import get_session
from app.core.tenant_ctx import set_tenant_id
from app.models import Tenant


def generate_api_key() -> str:
    """Genera una API key legible y única (se muestra una sola vez)."""
    rand = secrets.token_urlsafe(24)
    return f"liah_live_sk_{rand}"


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


async def get_current_tenant_by_api_key(
    x_tenant_api_key: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> uuid.UUID:
    """Valida X-Tenant-API-Key y devuelve el tenant_id autenticado."""
    if not x_tenant_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Tenant-API-Key header",
        )
    key_hash = hash_api_key(x_tenant_api_key)
    tenant = (
        await session.execute(
            select(Tenant).where(Tenant.api_key_hash == key_hash)
        )
    ).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )
    set_tenant_id(tenant.id)
    return tenant.id
