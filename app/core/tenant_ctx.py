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


def clear_tenant_id() -> None:    _tenant_id_ctx.set(None)


def peek_tenant_id() -> Optional[uuid.UUID]:
    """Versión no exigente de get_tenant_id(): None si no hay contexto.

    Para logging y diagnóstico (Fase 8): nunca debe romper el flujo.
    """
    return _tenant_id_ctx.get()


def require_tenant() -> uuid.UUID:
    """Atajo explícito: exige que el contexto de tenant esté configurado.

    Úsalo al inicio de cualquier ruta de acceso a datos que reciba el
    tenant_id por otro canal (payload, job encolado), para que el aislamiento
    no dependa de que alguien "se acuerde" de llamar a get_tenant_id().
    """
    return get_tenant_id()


# ── RLS (decisión Fase 1) ─────────────────────────────────────────────
# Se evaluó Row-Level Security de Postgres como defensa en profundidad.
# DECISIÓN: se difiere. Motivos: (1) RLS exige cambiar de rol por conexión
# (SET ROLE / SET app.tenant_id) en cada checkout del pool, lo que complica
# el pooling y el debugging; (2) el aislamiento hoy es lógico y se refuerza
# con TenantMixin + require_tenant() + tests de aislamiento por tenant en
# cada fase. RLS se reconsidera antes de vender a terceros (ver
# docs/DECISIONES_FASE1.md).
