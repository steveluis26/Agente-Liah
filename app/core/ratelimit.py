"""Fase 8 — rate limiting mínimo anti-abuso (en memoria, por proceso).

Reglas (path-prefijo → límite por ventana):
- POST /api/v1/admin/auth/login : 10 intentos/min por IP (anti fuerza bruta).
- /webhook/whatsapp            : 240 req/min por IP (Meta reintenta; amplio).
- resto                        : 600 req/min por IP.

Se responde 429 con Retry-After. Es por proceso (no distribuido): suficiente
para un VPS con 1 réplica de API; si se escala a N réplicas, mover a Redis.

Se activa con LIAH_RATE_LIMIT_ENABLED=true (default true).
"""
import logging
import time
from collections import defaultdict, deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

logger = logging.getLogger("liah.ratelimit")

_RULES = [
    ("/api/v1/admin/auth/login", 10, 60),
    ("/webhook/whatsapp", 240, 60),
    ("", 600, 60),  # default
]


def _client_ip(request) -> str:
    # Detrás de un proxy inverso (nginx/traefik) el despliegue debe fijar
    # X-Forwarded-For; en VPS directo se usa el peer.
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)
        self._hits: dict[tuple[str, str], deque] = defaultdict(deque)

    def _rule_for(self, path: str) -> tuple[str, int, int]:
        for prefix, limit, window in _RULES:
            if path.startswith(prefix):
                return prefix, limit, window
        return "", 600, 60

    async def dispatch(self, request, call_next):
        # /health nunca se limita (orquestadores y uptime checks).
        if request.url.path == "/health":
            return await call_next(request)
        prefix, limit, window = self._rule_for(request.url.path)
        key = (_client_ip(request), prefix or "default")
        now = time.monotonic()
        dq = self._hits[key]
        while dq and dq[0] <= now - window:
            dq.popleft()
        if len(dq) >= limit:
            retry = int(dq[0] + window - now) + 1
            logger.warning("rate limit 429 ip=%s path=%s", key[0], request.url.path)
            return JSONResponse(
                {"detail": "demasiadas solicitudes, intenta de nuevo en un momento"},
                status_code=429,
                headers={"Retry-After": str(retry)},
            )
        dq.append(now)
        # Poda oportunista para no crecer sin cota.
        if len(self._hits) > 20000:
            self._hits.clear()
        return await call_next(request)
