"""Autenticación del panel por API Key (X-Tenant-API-Key).

Al crear un tenant se genera una clave (liah_live_sk_...) que se muestra una
sola vez; en BD solo se guarda su hash con **salt por clave + pepper global**:

    hash = SHA-256(salt + "." + pepper + "." + key)

- salt: aleatorio por clave, guardado en `tenants.api_key_salt` (no es secreto).
- pepper: `LIAH_API_KEY_PEPPER` (secreto, env/secret manager; obligatorio en prod).

Claves creadas antes de la Fase 1 (salt NULL) se verifican con el esquema
legacy SHA-256(key) para no romperlas; la rotación las migra al esquema nuevo.
"""
import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_session
from app.core.tenant_ctx import set_tenant_id
from app.models import PlatformUser, Tenant
from app.models.platform_users import (
    ROLE_PLATFORM_ADMIN,
    TENANT_ROLES,
    VALID_ROLES,
    verify_password,
)

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


# ── Login humano del panel: JWT (Fase 3) ───────────────────────────────────
#
# Implementación con stdlib (hmac + base64url), sin dependencias nuevas.
# Decisión documentada en docs/DECISIONES_FASE3.md: se acepta SOLO HS256, el
# secreto viene de LIAH_JWT_SECRET (con rechazo de defaults en producción) y
# se validan firma, alg y exp. Nada de `alg: none`.
#
# El token viaja por header `Authorization: Bearer` (clientes API) o por la
# cookie httpOnly `liah_admin_token` (UI server-rendered /admin). Ambos pasan
# por la misma verificación en `get_current_user`.

ADMIN_JWT_COOKIE = "liah_admin_token"
_JWT_ALG = "HS256"


def _jwt_secret() -> str:
    return get_settings().liah_jwt_secret


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data.encode("ascii") + b"=" * (-len(data) % 4))


def create_access_token(
    user_id: uuid.UUID,
    role: str,
    tenant_id: uuid.UUID | None,
    expires_minutes: int | None = None,
) -> str:
    """Emite un JWT HS256 con claims sub/role/tenant_id/iat/exp."""
    if expires_minutes is None:
        expires_minutes = get_settings().liah_jwt_expire_minutes
    now = int(time.time())
    header = {"alg": _JWT_ALG, "typ": "JWT"}
    payload = {
        "sub": str(user_id),
        "role": role,
        "tenant_id": str(tenant_id) if tenant_id else None,
        "iat": now,
        "exp": now + expires_minutes * 60,
    }
    signing_input = (
        f"{_b64url_encode(json.dumps(header, separators=(',', ':')).encode())}."
        f"{_b64url_encode(json.dumps(payload, separators=(',', ':')).encode())}"
    )
    sig = hmac.new(
        _jwt_secret().encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{signing_input}.{_b64url_encode(sig)}"


class JWTError(Exception):
    """Token ausente, malformado, manipulado o expirado."""


def verify_access_token(token: str) -> dict:
    """Verifica firma HS256 + alg + exp. Devuelve el payload o lanza JWTError."""
    try:
        signing_input, sig_b64 = token.rsplit(".", 1)
        header_b64, payload_b64 = signing_input.split(".")
    except ValueError:
        raise JWTError("token malformado")
    try:
        header = json.loads(_b64url_decode(header_b64))
        payload = json.loads(_b64url_decode(payload_b64))
        sig = _b64url_decode(sig_b64)
    except Exception:
        raise JWTError("token malformado")
    if header.get("alg") != _JWT_ALG:
        # Falla cerrado ante alg-confusion: solo aceptamos HS256.
        raise JWTError("algoritmo no soportado")
    expected = hmac.new(
        _jwt_secret().encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(expected, sig):
        raise JWTError("firma inválida")
    now = int(time.time())
    if not isinstance(payload.get("exp"), int) or payload["exp"] <= now:
        raise JWTError("token expirado")
    if payload.get("role") not in VALID_ROLES:
        raise JWTError("rol inválido")
    return payload


@dataclass
class CurrentUser:
    """Usuario humano autenticado por JWT (panel)."""

    id: uuid.UUID
    email: str
    role: str
    tenant_id: uuid.UUID | None

    @property
    def is_platform_admin(self) -> bool:
        return self.role == ROLE_PLATFORM_ADMIN


async def _load_user(session: AsyncSession, payload: dict) -> CurrentUser:
    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError, AttributeError, TypeError):
        raise JWTError("sub inválido")
    user = await session.get(PlatformUser, user_id)
    if user is None or user.role != payload.get("role"):
        # El usuario fue borrado o cambió de rol: el token viejo muere.
        raise JWTError("usuario inválido")
    token_tenant = payload.get("tenant_id")
    user_tenant = str(user.tenant_id) if user.tenant_id else None
    if token_tenant != user_tenant:
        raise JWTError("tenant del token no coincide")
    return CurrentUser(
        id=user.id, email=user.email, role=user.role, tenant_id=user.tenant_id
    )


def _token_from_request(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return request.cookies.get(ADMIN_JWT_COOKIE)


async def get_user_from_token(
    session: AsyncSession, token: str | None
) -> CurrentUser | None:
    """Resuelve el usuario de un JWT sin lanzar (para la UI server-rendered).

    Devuelve None si el token falta, está manipulado o expiró.
    """
    if not token:
        return None
    try:
        return await _load_user(session, verify_access_token(token))
    except JWTError:
        return None


async def get_current_user(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> CurrentUser:
    """Autentica al operador por JWT (header Bearer o cookie httpOnly)."""
    token = _token_from_request(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = verify_access_token(token)
        return await _load_user(session, payload)
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        )


def require_platform_admin(
    user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    if not user.is_platform_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requiere rol platform_admin",
        )
    return user


def require_tenant_role(*roles: str):
    """Dependencia parametrizada: el usuario debe tener uno de los roles."""

    async def _dep(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requiere uno de los roles: {', '.join(roles)}",
            )
        return user

    return _dep


def require_tenant_access(tenant_id: uuid.UUID, user: CurrentUser) -> uuid.UUID:
    """Aplica el scope del usuario sobre un tenant_id de la ruta.

    - platform_admin: acceso a cualquier tenant.
    - tenant_admin/tenant_agent: solo a su propio tenant (mismatch → 403).

    Devuelve el tenant_id autorizado (para encadenar en queries).
    """
    if user.is_platform_admin:
        return tenant_id
    if user.tenant_id != tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Sin acceso a este tenant",
        )
    return tenant_id


async def authenticate_platform_user(
    session: AsyncSession, email: str, password: str
) -> PlatformUser | None:
    """Valida credenciales del panel. Devuelve el usuario o None.

    El tiempo es deliberadamente no-constante en el lookup por email (no es
    secreto); la comparación del hash sí usa compare_digest.
    """
    user = (
        await session.execute(
            select(PlatformUser).where(
                PlatformUser.email == email.strip().lower()
            )
        )
    ).scalar_one_or_none()
    if user is None or not verify_password(password, user.password_hash):
        return None
    return user


async def authorize_embedded_signup(
    request: Request, session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    """Autoriza el callback de Meta Embedded Signup (Fase 3).

    Antes: el endpoint tomaba `tenant_id` del body sin autenticar (vector de
    hijack: cualquiera podía enlazar su WABA al tenant de otro).

    Mecanismo elegido (documentado en docs/DECISIONES_FASE3.md): se acepta
    cualquiera de las dos credenciales, en este orden:

    1. JWT de `platform_admin` (el operador white-label completa el signup
       desde el panel).
    2. `X-Tenant-API-Key` cuya clave corresponde EXACTAMENTE al `tenant_id`
       del body (el tenant enlaza su propio WhatsApp, self-service).

    Cualquier otra combinación → 401/403.
    """
    # 1) JWT de plataforma.
    token = _token_from_request(request)
    if token:
        try:
            user = await _load_user(session, verify_access_token(token))
            if user.is_platform_admin:
                return
        except JWTError:
            pass
    # 2) API key del propio tenant.
    api_key = request.headers.get("x-tenant-api-key")
    if api_key:
        tenant = await session.get(Tenant, tenant_id)
        if tenant is not None and _verify_candidate(api_key, tenant):
            return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Callback de signup no autorizado: requiere JWT de platform_admin "
        "o X-Tenant-API-Key del tenant",
    )
