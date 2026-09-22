"""Usuarios humanos del panel (Fase 3).

Roles:
- `platform_admin`: opera la plataforma (todos los tenants). Único que puede
  crear tenants y operadores.
- `tenant_admin`: administra UN tenant (config, bandeja, métricas).
- `tenant_agent`: opera la bandeja de UN tenant (ver/tomar/resolver/devolver).

`tenant_id` es NULL solo para `platform_admin`. El password se guarda como
hash PBKDF2-HMAC-SHA256 con salt por usuario (stdlib, sin dependencias nuevas):

    formato: "pbkdf2_sha256$<iteraciones>$<salt_hex>$<hash_hex>"

Decisión documentada en docs/DECISIONES_FASE3.md (por qué no bcrypt/argon2
como dependencia: el esqueleto minimiza dependencias; PBKDF2 con 600k
iteraciones es el mínimo aceptado por OWASP para este uso).
"""
import hashlib
import secrets
import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base

ROLE_PLATFORM_ADMIN = "platform_admin"
ROLE_TENANT_ADMIN = "tenant_admin"
ROLE_TENANT_AGENT = "tenant_agent"
VALID_ROLES = (ROLE_PLATFORM_ADMIN, ROLE_TENANT_ADMIN, ROLE_TENANT_AGENT)

TENANT_ROLES = (ROLE_TENANT_ADMIN, ROLE_TENANT_AGENT)

_PBKDF2_ALGO = "pbkdf2_sha256"
_PBKDF2_ITERATIONS = 600_000  # recomendación OWASP 2023 para PBKDF2-HMAC-SHA256
_SALT_BYTES = 16


def hash_password(password: str) -> str:
    """Hash PBKDF2-HMAC-SHA256 con salt aleatorio por usuario."""
    salt = secrets.token_bytes(_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS
    )
    return (
        f"{_PBKDF2_ALGO}${_PBKDF2_ITERATIONS}"
        f"${salt.hex()}${dk.hex()}"
    )


def verify_password(password: str, password_hash: str | None) -> bool:
    """Verifica contra el formato de `hash_password`. Falla cerrado."""
    try:
        if not password_hash:
            return False
        algo, iters, salt_hex, hash_hex = password_hash.split("$")
        if algo != _PBKDF2_ALGO:
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iters),
        )
        return secrets.compare_digest(dk.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


class PlatformUser(Base):
    __tablename__ = "platform_users"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v4()")
    )
    email: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    password_hash: Mapped[str | None] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(
        String(20), default=ROLE_TENANT_AGENT, nullable=False
    )  # Fase 3: default de menor privilegio ('owner' legacy migrado a platform_admin)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        server_default=text("now()"), nullable=False
    )
