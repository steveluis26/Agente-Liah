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

## Panel de operador (Fase 3)

UI mínima server-rendered en `/admin` (Jinja2, sin build step) + API JSON en
`/api/v1/admin`: bandeja de handoffs (ver/tomar/resolver/devolver al bot),
editor de config por tenant y tablero de métricas/costo.

**Cómo entrar:**

```bash
# 1. Migra (crea `conversations`, columnas de auth en `platform_users`)
alembic upgrade head

# 2. Crea el primer operador (solo si no existe ninguno; password por env o
#    prompt interactivo; jamás hay default)
LIAH_ADMIN_PASSWORD='tu-password-seguro' python scripts/seed_platform_admin.py

# 3. Arranca y abre http://localhost:8000/admin
uvicorn app.main:app --reload --port 8000
```

**Roles:** `platform_admin` (opera todos los tenants; único que puede crear
tenants y operadores), `tenant_admin` (config + bandeja + métricas de su
tenant), `tenant_agent` (bandeja de su tenant). Login con email+password →
JWT (header `Authorization: Bearer` o cookie httpOnly `liah_admin_token`,
que fija el login para la UI). En producción configura `LIAH_JWT_SECRET`
(el arranque falla si sigue siendo el default) y `LIAH_JWT_EXPIRE_MINUTES`
(default 480).

**Endpoints del panel** (`/api/v1/admin`): `POST /auth/login`, `GET /tenants`,
`GET /handoffs` (+ `POST /handoffs/{id}/take|resolve|return-to-bot`),
`GET|PUT /tenants/{id}/config` (valida `model_routing` contra el factory de
Fase 2; nunca expone secretos), `GET /tenants/{id}/metrics?days=30`
(resolución automática, transferencias, 1ª respuesta media, acciones
exitosas, costo por conversación).

**Seguridad cerrada en esta fase:** `POST /tenants` exige JWT de
`platform_admin` (antes abierto); el callback de Embedded Signup exige JWT
de plataforma o la `X-Tenant-API-Key` del propio tenant (antes aceptaba
`tenant_id` del body sin auth). Ver `docs/DECISIONES_FASE3.md`.

## Siguiente fase

Fase 4: alta por perfil declarativo (`templates/<giro>.yaml` versionados +
"nuevo cliente desde plantilla" en el panel; primera plantilla:
`consultorio_medico`). Ver `docs/DECISIONES_FASE3.md` para el contexto.
