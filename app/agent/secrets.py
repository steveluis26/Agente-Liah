"""Resolución de secretos por tenant (Fase 2).

REGLA DURA: ningún secreto vive en el repo ni en la BD en claro. Las tablas
solo guardan *referencias* (`WhatsappChannel.token_secret_ref`); el valor
real se resuelve aquí, en runtime, vía un `SecretProvider`.

Convención de nombres (EnvSecretProvider):
- OpenAI por tenant: `OPENAI_API_KEY_TENANT_<SLUG>` donde <SLUG> es el slug
  del tenant en mayúsculas y todo lo no alfanumérico -> '_'.
  Ej: tenant `clinica-san-angel` -> `OPENAI_API_KEY_TENANT_CLINICA_SAN_ANGEL`.
  Fallback: `OPENAI_API_KEY` global (útil en dev / instancia por cliente).
- WhatsApp por tenant: `WA_TOKEN_<secret_ref>` (el secret_ref tal cual se
  guardó en `whatsapp_channels.token_secret_ref`).
- Embeddings OpenAI: usan la misma key del tenant (misma cuenta que chat),
  salvo que exista `OPENAI_EMBED_API_KEY_TENANT_<SLUG>`.

Para producción real, enchufa un secret manager implementando
`SecretProvider` (ver `VaultSecretProvider`, stub documentado) y pásalo al
factory del engine / al sender.
"""
import os
import re
from typing import Protocol, runtime_checkable


class SecretNotFoundError(RuntimeError):
    """Un secreto requerido no se resolvió en ningún proveedor."""


@runtime_checkable
class SecretProvider(Protocol):
    """Contrato mínimo de un almacén de secretos."""

    def get_secret(self, name: str) -> str | None:
        """Devuelve el valor o None si no existe."""
        ...

    def require_secret(self, name: str) -> str:
        """Devuelve el valor o lanza SecretNotFoundError con mensaje claro."""
        ...


class EnvSecretProvider:
    """Secretos desde variables de entorno (dev / instancia por cliente).

    Convención documentada arriba. Nunca loguea valores.
    """

    def get_secret(self, name: str) -> str | None:
        return os.environ.get(name) or None

    def require_secret(self, name: str) -> str:
        value = self.get_secret(name)
        if not value:
            raise SecretNotFoundError(
                f"Secreto '{name}' no configurado. Defínelo como variable de "
                "entorno o conecta un secret manager (ver app/agent/secrets.py)."
            )
        return value


class VaultSecretProvider:
    """STUB: conector a un secret manager real (Vault / AWS Secrets Manager).

    NO implementado en el esqueleto: es el punto de extensión documentado.
    Para producción con varios tenants, implementa esta clase (o una propia
    que cumpla `SecretProvider`) leyendo p.ej.:

      - HashiCorp Vault:  hvac.Client(...).secrets.kv.v2.read_secret_version(path)
      - AWS:              boto3.client("secretsmanager").get_secret_value(SecretId=...)

    y pásala a `build_llm_for_tenant(..., secrets=...)` y al sender. Mantén
    el mismo esquema de nombres (OPENAI_API_KEY_TENANT_<SLUG>, WA_TOKEN_<ref>)
    para que el cambio sea solo de backend, no de convención.
    """

    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "VaultSecretProvider es un stub documentado: implementa "
            "get_secret()/require_secret() contra tu secret manager real "
            "(ver docstring en app/agent/secrets.py)."
        )

    def get_secret(self, name: str) -> str | None:  # pragma: no cover
        raise NotImplementedError

    def require_secret(self, name: str) -> str:  # pragma: no cover
        raise NotImplementedError


def _slug_env(slug: str) -> str:
    """Normaliza un slug de tenant a sufijo de variable de entorno."""
    return re.sub(r"[^A-Z0-9]", "_", (slug or "").upper())


def tenant_openai_key_names(tenant_slug: str) -> list[str]:
    """Nombres candidatos (en orden) para la API key de OpenAI del tenant."""
    return [
        f"OPENAI_API_KEY_TENANT_{_slug_env(tenant_slug)}",
        "OPENAI_API_KEY",  # fallback global
    ]


def resolve_tenant_openai_key(
    provider: SecretProvider, tenant_slug: str
) -> str | None:
    """Resuelve la API key de OpenAI del tenant (específica, luego global)."""
    for name in tenant_openai_key_names(tenant_slug):
        value = provider.get_secret(name)
        if value:
            return value
    return None


def require_tenant_openai_key(
    provider: SecretProvider, tenant_slug: str
) -> str:
    """Como `resolve_...`, pero falla con mensaje accionable si no hay key."""
    key = resolve_tenant_openai_key(provider, tenant_slug)
    if not key:
        names = " o ".join(tenant_openai_key_names(tenant_slug))
        raise SecretNotFoundError(
            f"El tenant '{tenant_slug}' requiere OpenAI pero no se resolvió "
            f"su API key. Define {names} o conecta un secret manager."
        )
    return key


def resolve_whatsapp_token(
    provider: SecretProvider, secret_ref: str | None
) -> str:
    """Resuelve el token de envío del canal de WhatsApp del tenant.

    Falla explícitamente si el ref está vacío/PENDING o el secreto no existe:
    jamás usa el ref como token.
    """
    if not secret_ref or secret_ref == "PENDING":
        raise SecretNotFoundError(
            "El canal no tiene token configurado (token_secret_ref vacío o "
            "'PENDING'). Configura el secreto del tenant antes de enviar."
        )
    name = f"WA_TOKEN_{secret_ref}"
    token = provider.get_secret(name)
    if not token:
        raise SecretNotFoundError(
            f"No se resolvió el secreto {name}. Defínelo o conecta el "
            "secret manager."
        )
    return token
