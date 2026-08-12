# PYME Agent — Fase 0 (Multi-tenant WhatsApp scaffold)

Scaffold de grado producción: FastAPI async + PostgreSQL/pgvector, aislación
multi-tenant por `tenant_id`, webhook de Meta WhatsApp con verificación de
token (GET) y firma HMAC `X-Hub-Signature-256` (POST), resolución de tenant
vía `phone_number_id`, y persistencia inmutable de mensajes.

## Levantar en local (Mac, con Docker)

```bash
# 1. Crea .env a partir del ejemplo
cp .env.example .env

# 2. Levanta Postgres (pgvector) + Redis
docker compose -f infra/docker-compose.yml up -d

# 3. Instala dependencias
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[test]"

# 4. Corre la migración inicial (crea esquema + extensiones)
alembic upgrade head

# 5. (Opcional) Seed demo: academia-danza-demo + canal de prueba
python scripts/seed_fase0.py

# 6. Arranca la API
uvicorn app.main:app --reload --port 8000

# 7. Tests (usa pyme_agent_test)
TEST_DATABASE_URL=postgresql+asyncpg://pyme:pyme@localhost:5432/pyme_agent_test \
  pytest -q
```

## Endpoints (Fase 0)

- `GET  /health`
- `GET  /webhook/whatsapp?hub.mode=subscribe&hub.verify_token=...&hub.challenge=...`
  → verifica el token y devuelve `hub.challenge`. 403 si no coincide.
- `POST /webhook/whatsapp` (Meta)
  → exige firma `X-Hub-Signature-256`; resuelve tenant por `phone_number_id`,
    crea/actualiza `contacts` e inserta `messages` inbound. 403 si firma inválida.

## Verificación ejecutada (Fase 0)

- 4/4 tests verdes contra Postgres 16 + pgvector real.
- Migración Alembic: 15 tablas creadas (incl. `knowledge_chunks` con índice HNSW).
- Seed: tenant `academia-danza-demo` + canal `phone_number_id=123456789`.
- App arranca: `GET /health` → 200.

## Notas de seguridad / producción

- El `tenant_id` se resuelve en el contexto (`app/core/tenant_ctx.py`) y es
  obligatorio en todo acceso a datos (Fase 1+).
- En producción los tokens de canal NO viven en `.env`; se resuelven vía
  `token_secret_ref` desde un secret manager.
- Recordatorios proactivos (colegiatura, seguimiento a 30 días) REQUIEREN
  plantillas aprobadas por Meta fuera de la ventana de 24h (Fase 2).
- Consultorios/escuelas: habilitar `lfpdp_consent_required` y aviso de
  privacidad en el primer contacto (LFPDPPP, México).

## Siguiente fase

Fase 1: motor de agente (LLM + tool-calling), RAG con pgvector, y conexión
a agenda (Cal.com / Google Calendar) como fuente de verdad de disponibilidad.
