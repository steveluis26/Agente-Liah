"""ContextVar que garantiza el aislamiento multi-tenant.

Todo acceso a datos (SELECT/INSERT/UPDATE) en el CRUD debe ejecutarse
DENTRO de un contexto con tenant_id resuelto. Si no está seteado, el
acceso falla explícitamente: es imposible tocar datos sin filtrar por tenant.
"""
import uuid
from contextvars import ContextVar
from typing import Optional

_tenant_id_ctx: ContextVar[Optional[uuid.UUID]] = ContextVar(
    "tenant_id", default=None
)


def set_tenant_id(tenant_id: uuid.UUID) -> None:
    _tenant_id_ctx.set(tenant_id)


def get_tenant_id() -> uuid.UUID:
    tenant_id = _tenant_id_ctx.get()
    if tenant_id is None:
        raise RuntimeError(
            "tenant_id no está configurado en el contexto. "
            "Todo acceso a datos debe resolverse desde un tenant."
        )
    return tenant_id


def clear_tenant_id() -> None:
    _tenant_id_ctx.set(None)
