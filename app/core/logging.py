"""Fase 8 — logging estructurado para soporte multi-tenant.

- `setup_logging(json_format)`: configura el root logger. En producción
  (LIAH_LOG_JSON=true) emite una línea JSON por registro con campos
  estables: ts, level, logger, msg, tenant_id.
- `TenantIdFilter`: inyecta el tenant_id del ContextVar en cada record.
  Nunca rompe el flujo (usa peek_tenant_id()).

Uso para soporte: filtrar por tenant en los logs del compose:
    docker compose -f infra/docker-compose.prod.yml logs api | \
      grep '"tenant_id": "<uuid>"'
  o con jq si el formato es JSON.
"""
import json
import logging
from datetime import datetime, timezone

from app.core.tenant_ctx import peek_tenant_id


class TenantIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        tid = peek_tenant_id()
        record.tenant_id = str(tid) if tid else "-"
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "tenant_id": getattr(record, "tenant_id", "-"),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(json_format: bool = False, level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        JsonFormatter() if json_format
        else logging.Formatter(
            "%(asctime)s %(levelname)s [%(name)s] [tenant=%(tenant_id)s] %(message)s"
        )
    )
    handler.addFilter(TenantIdFilter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    # Baja el ruido de librerías en producción.
    for noisy in ("httpx", "httpcore", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
