"""Costeo por turno/conversación (Fase 2).

Convierte tokens × precio del modelo -> `cost_usd` y persiste el registro.
Los precios son REFERENCIALES (pueden cambiar; el operador los ajusta vía
env `LIAH_MODEL_PRICES_JSON` o editando `MODEL_PRICES_USD_PER_1M`). Ollama /
modelos locales cuestan 0.

`record_turn_usage()` escribe el turno en `usage_records`.
`aggregate_monthly_usage()` suma al agregado mensual `usage_monthly` (el cron
real que la invoque llega en Fase 6; mientras tanto es invocable a mano o
desde el panel de Fase 3).
"""
import json
import logging
import os
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.usage_records import UsageMonthly, UsageRecord

logger = logging.getLogger("liah.costing")

# USD por 1M tokens. Referenciales (docs/DECISIONES_FASE2.md): el operador
# los mantiene; no son promesa de precio de OpenAI.
MODEL_PRICES_USD_PER_1M: dict[str, dict[str, float]] = {
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4o": {"input": 2.50, "output": 10.00},
}


def _load_price_overrides() -> None:
    raw = os.getenv("LIAH_MODEL_PRICES_JSON")
    if not raw:
        return
    try:
        overrides = json.loads(raw)
        for model, prices in overrides.items():
            MODEL_PRICES_USD_PER_1M[str(model)] = {
                "input": float(prices.get("input", 0.0)),
                "output": float(prices.get("output", 0.0)),
            }
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        logger.warning("LIAH_MODEL_PRICES_JSON inválido (%s); se ignoran", e)


_load_price_overrides()


def _base_model(model: str | None) -> str:
    """Normaliza variantes con fecha/sufijo al precio conocido más cercano."""
    m = (model or "").strip().lower()
    if m.startswith("gpt-4o-mini"):
        return "gpt-4o-mini"
    if m.startswith("gpt-4o"):
        return "gpt-4o"
    return m


def cost_usd(model: str | None, usage: dict | None) -> float:
    """Costo USD del turno a partir de tokens_in/out y la tabla de precios.

    Modelos desconocidos o locales (ollama/llama) -> 0.0 (se loguea para
    que el operador agregue el precio si hace falta).
    """
    usage = usage or {}
    tokens_in = int(usage.get("prompt_tokens") or 0)
    tokens_out = int(usage.get("completion_tokens") or 0)
    if tokens_in == 0 and tokens_out == 0:
        return 0.0
    prices = MODEL_PRICES_USD_PER_1M.get(_base_model(model))
    if prices is None:
        logger.debug("Modelo '%s' sin precio configurado; costo 0.0", model)
        return 0.0
    return (
        tokens_in / 1_000_000 * prices["input"]
        + tokens_out / 1_000_000 * prices["output"]
    )


async def record_turn_usage(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    contact_id: uuid.UUID,
    conversation_id: uuid.UUID | None,
    model: str | None,
    usage: dict | None,
) -> UsageRecord:
    """Persiste el usage de un turno del agente (hace flush, no commit).

    El commit lo decide el flujo que llama (el engine no debe cambiar su
    semántica transaccional por el costeo).
    """
    usage = usage or {}
    record = UsageRecord(
        tenant_id=tenant_id,
        contact_id=contact_id,
        conversation_id=conversation_id,
        model=(model or "unknown")[:60],
        tokens_in=int(usage.get("prompt_tokens") or 0),
        tokens_out=int(usage.get("completion_tokens") or 0),
        cost_usd=cost_usd(model, usage),
    )
    session.add(record)
    await session.flush()
    return record


async def aggregate_monthly_usage(
    session: AsyncSession,
    *,
    year: int | None = None,
    month: int | None = None,
    tenant_id: uuid.UUID | None = None,
) -> int:
    """Suma `usage_records` al agregado mensual `usage_monthly` (upsert).

    Sin `tenant_id` agrega todos los tenants del mes. Devuelve cuántas filas
    de `usage_monthly` se insertaron/actualizaron. Idempotente: re-ejecutar
    el mismo mes recalcula desde los registros, no duplica.
    """
    now = datetime.now(timezone.utc)
    year = year or now.year
    month = month or now.month

    q = (
        select(
            UsageRecord.tenant_id,
            func.coalesce(func.sum(UsageRecord.tokens_in), 0).label("tin"),
            func.coalesce(func.sum(UsageRecord.tokens_out), 0).label("tout"),
            func.coalesce(func.sum(UsageRecord.cost_usd), 0).label("cost"),
        )
        .where(
            func.extract("year", UsageRecord.created_at) == year,
            func.extract("month", UsageRecord.created_at) == month,
        )
        .group_by(UsageRecord.tenant_id)
    )
    if tenant_id is not None:
        q = q.where(UsageRecord.tenant_id == tenant_id)
    rows = (await session.execute(q)).all()

    updated = 0
    for tenant, tin, tout, cost in rows:
        stmt = (
            pg_insert(UsageMonthly)
            .values(
                tenant_id=tenant,
                year=year,
                month=month,
                tokens_in=int(tin),
                tokens_out=int(tout),
                cost_usd=float(cost),
            )
            .on_conflict_do_update(
                index_elements=["tenant_id", "year", "month"],
                set_={
                    "tokens_in": int(tin),
                    "tokens_out": int(tout),
                    "cost_usd": float(cost),
                    "updated_at": func.now(),
                },
            )
        )
        await session.execute(stmt)
        updated += 1
    await session.flush()
    return updated
