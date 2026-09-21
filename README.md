# Agente Liah

> El asistente de WhatsApp que atiende, agenda y da seguimiento a los clientes de tu pyme, 24/7 y con tu tono.

Scaffold de grado producción multi-tenant (FastAPI async + PostgreSQL/pgvector): un solo
motor de agente parametrizado por negocio, para academias de danza, escuelas, barberías y
consultorios. Fase 0 = tubería de entrada (Webhook → Seguridad → Identificación de Tenant →
Persistencia). Fase 1+ = motor de agente (RAG + tool-calling) y calendario como fuente de verdad.

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

# 7. Tests (usa pyme_agent_test, con proxies desactivados)
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy \
    -u all_proxy -u NO_PROXY -u no_proxy python -m pytest -q
```

## Configuración por tenant (Fase 2)

El LLM y el embedder se eligen **por tenant** en `tenant_configs.model_routing`
(JSONB), no por flags del cliente:

```json
{"llm_provider": "openai", "llm_model": "gpt-4o-mini",
 "llm_max_tokens": 800, "llm_temperature": 0.2, "embedder": "openai"}
```

- `llm_provider`: `openai` (default comercial) u `ollama` (local/dev, costo 0).
- La API key de OpenAI se resuelve **por tenant** vía `SecretProvider`
  (ver `app/agent/secrets.py`): `OPENAI_API_KEY_TENANT_<SLUG>` (slug en
  mayúsculas, no alfanuméricos → `_`), con fallback a `OPENAI_API_KEY`.
  En producción se enchufa un secret manager real (stub `VaultSecretProvider`).
- El token de WhatsApp se resuelve igual: `WA_TOKEN_<secret_ref>`.
- Cada llamada al LLM deja su `usage` en `usage_records` (tokens in/out,
  `cost_usd` según tabla de precios referencial en `app/agent/costing.py`);
  `aggregate_monthly_usage()` genera el agregado `usage_monthly` que leerá el
  panel de costos (Fase 3).
- `EMBED_DIM` (default 1536, OpenAI) es la única fuente de verdad de la
  dimensión de embeddings y se valida contra la columna pgvector al arrancar
  (falla rápido ante mismatch 1536 vs 768).

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

Fase 3: panel mínimo (bandeja de handoff, config por tenant, métricas y costo
visible). Ver `docs/DECISIONES_FASE2.md` para las decisiones de esta fase.
