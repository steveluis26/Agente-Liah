# Dockerfile — Esqueleto Liah (Fase 8: empaque comercial)
#
# Una sola imagen para API y worker; docker-compose.prod.yml define el
# comando de cada servicio:
#   api:    uvicorn app.main:app (+ migraciones en servicio "migrate")
#   worker: python -m app.worker
#
# Build:  docker compose -f infra/docker-compose.prod.yml build
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv/liah

# libpq para asyncpg (psycopg no se usa; asyncpg trae sus propias libs,
# pero libpq evita sorpresas con herramientas como pg_isready si se agregan).
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock

# Código y artefactos de runtime. No se copia: tests/, docs/, logs/, .git.
COPY app/ app/
COPY migrations/ migrations/
COPY templates/ templates/
COPY scripts/ scripts/

# Usuario no-root para producción.
RUN useradd --create-home --shell /bin/bash liah \
    && chown -R liah:liah /srv/liah
USER liah

EXPOSE 8000

# Por defecto corre la API. Las migraciones las aplica el servicio
# "migrate" del compose ANTES de levantar api/worker.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
