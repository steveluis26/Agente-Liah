"""Autenticación del panel por API Key (X-Tenant-API-Key).

Al crear un tenant se genera una clave (liah_live_sk_...) que se muestra una
sola vez; en BD solo se guarda su hash con **salt por clave + pepper global**:

    hash = SHA-256(salt + "." + pepper + "." + key)

- salt: aleatorio por clave, guardado en `tenants.api_key_salt` (no es secreto).
- pepper: `LIAH_API_KEY_PEPPER` (secreto, env/secret manager; obligatorio en prod).

Claves creadas antes de la Fase 1 (salt NULL) se verifican con el esquema
legacy SHA-256(key) para no romperlas; la rotación las migra al esquema nuevo.
"""
import hashlib
import hmac
import secrets
import uuid

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_session
from app.core.tenant_ctx import set_tenant_id
from app.models import Tenant

_LEGACY_SALT = None  # marcador: fila sin salt -> esquema legacy


def _pepper() -> str:
    return get_settings().liah_api_key_pepper or ""


def generate_api_key() -> tuple[str, str]:
    """Genera (api_key, salt). La key se muestra una sola vez."""
    rand = secrets.token_urlsafe(24)
    salt = secrets.token_hex(16)
    return f"liah_live_sk_{rand}", salt


def hash_api_key(key: str, salt: str | None = _LEGACY_SALT) -> str:
    """Hash con salt+pepper; salt=None reproduce el esquema legacy (Fase 0)."""
    if salt is None:
        return hashlib.sha256(key.encode("utf-8")).hexdigest()
    material = f"{salt}.{_pepper()}.{key}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _verify_candidate(key: str, tenant: Tenant) -> bool:
    expected = hash_api_key(key, tenant.api_key_salt)
    return hmac.compare_digest(expected, tenant.api_key_hash or "")


async def get_current_tenant_by_api_key(
    x_tenant_api_key: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> uuid.UUID:
    """Valida X-Tenant-API-Key y devuelve el tenant_id autenticado.

    Nota de rendimiento: con salt por clave no se puede buscar por hash
    directo; se itera sobre tenants con api_key_hash no nulo. Aceptable para
    el volumen de tenants del esqueleto; si crece, migrar a prefijo indexado.
    """
    if not x_tenant_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Tenant-API-Key header",
        )
    tenants = (
        await session.execute(
            select(Tenant).where(Tenant.api_key_hash.is_not(None))
        )
    ).scalars().all()
    for tenant in tenants:
        if _verify_candidate(x_tenant_api_key, tenant):
            set_tenant_id(tenant.id)
            return tenant.id
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid API key",
    )
