# Decisiones Fase 1 — Endurecer motor (2026-09-21)

Motor/orquestador genérico, sin literales de ningún vertical. WhatsApp es un
canal más detrás del contrato `ChannelAdapter`.

## Cola persistente del webhook
El POST del webhook valida firma HMAC, resuelve el tenant por
`phone_number_id` y hace `enqueue_job()` (INSERT en `webhook_jobs`), luego
responde 200 en milisegundos. Nada del agente corre en el request:
`drain_jobs()` procesa fuera del request (worker, cron o endpoint
`POST /webhook/whatsapp/jobs/drain`). Un reinicio del proceso no pierde
mensajes (a diferencia de BackgroundTasks in-process). `statuses` de Meta
solo se auditan (`webhook.statuses`), nunca generan trabajo ni respuesta;
`phone_number_id` desconocido se descarta con 200 (sin reintentos de Meta).

## RLS diferido
`require_tenant()` se fuerza en el drenador de la cola (todo acceso a datos
pasa por ahí en el path del webhook) y `log_event`/`audit` reciben
`tenant_id` explícito. RLS a nivel Postgres queda diferido a Fase 2: hoy el
aislamiento se garantiza por filtros `tenant_id` en cada query + tests de
aislamiento por tenant (RAG, dispatch, webhook).

## Aislamiento
Índices únicos por tenant (`contacts(tenant_id, wa_id)`,
`appointments(tenant_id, start_at)`, `meta_message_id` parcial), RAG filtrado
duro por `tenant_id`, idempotency keys deterministas (`webhook:<wamid>:*`,
`book:<tenant>:<contacto>:<fecha>:<hora>`), y dedupe por `wamid` con
`INSERT ... ON CONFLICT DO NOTHING`. El `contact_id` que proponga el LLM se
ignora siempre: se usa el del contexto autenticado.

## Contrato ChannelAdapter
`app/channels/adapter.py` define `InboundEvent` (evento agnóstico) y el
protocolo `ChannelAdapter` con registro por nombre. `WhatsappAdapter`
implementa el parseo de Meta; el drenador resuelve el adapter por
`payload["channel"]`. Añadir Instagram/Facebook = implementar el protocolo y
registrarlo, sin tocar el motor.

## Callback de Embedded Signup
El alta de tenants y la rotación de API key están implementados (salt
aleatorio + pepper, compatibilidad temporal con hashes legacy). El callback
autenticado del Embedded Signup queda para Fase 3: el endpoint existe pero
la verificación firmada del callback se documenta como pendiente.

## usage_records
La tabla `usage_records` mencionada en el CHANGE_MAP se difiere al worker de
Fase 2 (costeo por tenant/tier): Fase 1 solo deja el `usage` en `LLMResponse`
para que el worker lo consuma. No se crea la tabla en esta fase.

## Convenciones de tiempo
Las columnas `DateTime` del esquema son naive; internamente se usa
`datetime.now(timezone.utc).replace(tzinfo=None)` (UTC naive). La zona del
negocio (`Tenant.timezone`) solo se usa para interpretar horarios de citas.
