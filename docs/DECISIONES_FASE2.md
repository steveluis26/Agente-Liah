# Decisiones Fase 2 — Conector OpenAI comercial + costeo (2026-09-21)

## Conector OpenAI (`app/agent/llm.py`)
- `OpenAILLM` terminado: captura `usage` real (`prompt_tokens`/`completion_tokens`).
  **Bug corregido:** `usage` se leía de `choices[0].message` (siempre daba 0);
  en la API real vive en el nivel raíz de la respuesta.
- `max_tokens` / `temperature` configurables por constructor; el factory los
  toma de `tenant_configs.model_routing` (`llm_max_tokens`, `llm_temperature`).
- Retry con backoff exponencial ante 429/5xx y errores de transporte
  (mismo patrón que `sender.py`); `trust_env=False` en el cliente httpx por el
  problema de proxies de la VM.
- La key **no** sale del env global directo: el factory la resuelve por tenant
  vía `SecretProvider` y la pasa ya resuelta al constructor.

## Secretos (`app/agent/secrets.py`)
- Contrato `SecretProvider` (`get_secret` / `require_secret`); `EnvSecretProvider`
  como implementación dev/instancia-por-cliente; `VaultSecretProvider` como
  **stub documentado** (punto de extensión para Vault/AWS en producción).
- Convención de nombres: `OPENAI_API_KEY_TENANT_<SLUG>` (slug normalizado a
  `A-Z0-9_`); fallback global `OPENAI_API_KEY`. WhatsApp: `WA_TOKEN_<secret_ref>`.
- El token de WhatsApp (`sender._resolve_token`) ahora también se resuelve por
  aquí. `SecretNotFoundError` hereda de `RuntimeError` (compatibilidad con
  tests/handlers que capturan `RuntimeError`).
- Nada de secretos en el repo ni en la BD en claro: solo `secret_ref`.

## Factory por tenant (`app/agent/engine.py`)
- `build_llm_for_tenant(session, tenant_id, secrets)`: lee
  `tenant_configs.model_routing` (`llm_provider`, `llm_model`, `llm_max_tokens`,
  `llm_temperature`, `ollama_*`). **Default comercial: `openai`** (fail fast con
  mensaje accionable si falta la key del tenant); `ollama` = opción local/dev.
- `build_embedder_for_tenant(routing, secrets)`: `openai` | `ollama` | `fake`.
  Default `fake` (cero costo en dev/tests); el tenant comercial lo fija a
  `openai` en el onboarding. Overridable por env `LIAH_DEFAULT_*`.
- El drenador (`queue._default_llm`) usa el factory; si el tenant no tiene key,
  cae al `_DevStubLLM` (dev) en vez de tumbar el job. Acepta `llm_factory`
  sync o async (compatibilidad con tests).

## Costeo (`app/agent/costing.py`, `app/models/usage_records.py`)
- Tabla de precios `MODEL_PRICES_USD_PER_1M` (gpt-4o-mini, gpt-4o):
  **referenciales y configurables** (env `LIAH_MODEL_PRICES_JSON` o editar el
  dict). Modelos locales/desconocidos → costo 0.0 (se loguea).
- `record_turn_usage()`: una fila en `usage_records` por llamada al LLM
  (tenant, contacto, modelo, tokens in/out, `cost_usd`). Hace flush, no commit:
  no cambia la semántica transaccional del flujo que llama.
- `aggregate_monthly_usage()`: upsert idempotente en `usage_monthly`
  (tenant, año, mes); re-ejecutar recalcula, no duplica. El cron programado
  llega en Fase 6; mientras tanto es invocable/manual o desde el panel (Fase 3).
- El engine registra el usage **después de cada `llm.chat`**; si el registro
  falla, se loguea y el loop **sigue** (el costeo jamás rompe una conversación).
- Migración `f2_costing` para deploys reales (los tests usan create_all).

## `EMBED_DIM`: una sola fuente de verdad (`app/agent/embedder.py`)
- `EMBED_DIM` vive en `app/agent/embedder.py`; `app/models/knowledge.py` la
  importa (antes cada uno leía el env por su lado: 1536 vs 768 podían
  mezclarse y romper pgvector en silencio).
- `validate_embed_dim(session)` coteja contra la columna real en BD y lanza
  `RuntimeError` con mensaje accionable ante mismatch. Se llama en el
  `lifespan` de `app/main.py`: si la BD no está disponible se loguea warning
  (no se bloquea el arranque por eso); si hay mismatch real, no arranca.
- `OpenAIEmbedder` falla claro si `EMBED_DIM != 1536`; `FakeEmbedder.dimension`
  es siempre `EMBED_DIM`.

## Knowledge API (`app/api/knowledge.py`)
- Eliminado el flag `use_openai` del body: era un vector de abuso (el cliente
  elegía consumir API de pago). El embedder lo decide `model_routing` del
  tenant; sin config válida → 400 con mensaje claro.

## Dependencias (`pyproject.toml`)
- Declaradas las que Fase 0 instaló sueltas: `openai`, `pypdf`,
  `beautifulsoup4`, `python-multipart`, `apscheduler[sqlalchemy]`.
  `respx` ya estaba en extras `test` (verificado).

## RLS: por qué queda pendiente (stretch no tomado)
RLS como defensa en profundidad sigue diferido, a propósito:
1. El aislamiento hoy está **garantizado y testeado** en la capa de queries
   (`require_tenant()` forzado en el drenador + filtros `tenant_id` + tests
   de aislamiento por tenant de Fase 1).
2. RLS real exige plumbing por conexión (rol de BD por tenant o
   `SET app.tenant_id` en cada checkout del pool async + políticas por tabla);
   es un cambio de riesgo medio que toca todo el acceso a datos, y el modelo
   de despliegue supuesto (instancia por cliente para los primeros 5–15)
   ya da aislamiento físico.
3. Se retoma **antes de vender a terceros sobre multi-tenant compartido**,
   como ya exige el CHANGE_MAP. Hacerlo ahora, sin un deploy que lo necesite,
   suma riesgo sin beneficio operativo.
