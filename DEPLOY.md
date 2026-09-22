# DEPLOY — Esqueleto Liah (Fase 8: empaque comercial)

Guía para levantar Liah en un VPS limpio (Ubuntu 22.04/24.04) con Docker.
Tiempo estimado: 30–45 min la primera vez.

## 1. Requisitos

- VPS con ≥2 vCPU, 4 GB RAM, 30 GB disco.
- Docker Engine + plugin compose (`docker compose version`).
- Un dominio apuntando al VPS (para el webhook de Meta con HTTPS).
- `git`.

## 2. Instalación

```bash
git clone https://github.com/steveluis26/Agente-Liah.git
cd Agente-Liah
git checkout skeleton

cp infra/.env.example infra/.env
nano infra/.env   # ← EDITA TODOS los valores CAMBIA_*

docker compose -f infra/docker-compose.prod.yml up -d --build
docker compose -f infra/docker-compose.prod.yml logs -f migrate  # debe terminar OK
curl http://127.0.0.1:8000/health   # {"status":"ok",...}
```

### Primer platform_admin (tu cuenta de operador)

```bash
docker compose -f infra/docker-compose.prod.yml exec api \
  python scripts/seed_platform_admin.py
# te pide email y password (12+ caracteres). Guárdala en tu gestor.
```

Entra a `http://TU_VPS:8000/admin` con esa cuenta.

### Alta de un cliente (renta o compra única)

```bash
docker compose -f infra/docker-compose.prod.yml exec api \
  python scripts/onboard_tenant.py templates/consultorio_medico.yaml \
    --slug clinica-ejemplo --admin-email admin@clinica-ejemplo.com
# OVERRIDES_JSON='{"model_routing":{"embedder":"fake"}}' si aún no hay OPENAI_API_KEY
```

Plantillas en `templates/`: `consultorio_medico`, `estetica`, `escuela_privada`,
`academia_danza`, `snacks_eventos`, `espejo_magico`.

## 3. WhatsApp / Meta (lo hace el operador, una vez por cliente)

1. En Meta Developers: app con producto WhatsApp, número de WhatsApp Business.
2. Webhook: `https://TU_DOMINIO/webhook/whatsapp` con el `WHATSAPP_VERIFY_TOKEN`
   del `.env`. Suscribe `messages`.
3. En el panel del tenant: registrar el `phone_number_id` del número
   (canal de WhatsApp del tenant).
4. **Plantillas HSM**: aprueba en Meta las plantillas de recordatorios/avisos
   antes de poner `LIAH_SEND_DRY_RUN=0`. Sin plantillas aprobadas no hay
   mensajes proactivos (regla de Meta, no del sistema).
5. Cuando todo esté verde: `LIAH_SEND_DRY_RUN=0` en `infra/.env` y
   `docker compose ... up -d api worker`.

> El trámite de WhatsApp Business/Embedded Signup lo hace el dueño del número
> (el cliente o tú como operador). Sin esto no hay mensajes reales.

## 4. Operación diaria (runbook de soporte)

### Ver como cliente (diagnóstico)
Entra al panel como `platform_admin`. Cada página (handoffs, métricas,
campañas, config, contactos, recursos) tiene un selector de tenant arriba:
elige el negocio del cliente y ves exactamente lo que él ve.

### Suspender / reactivar por falta de pago
```bash
# suspender (corta mensajes y campañas; el webhook responde 200 sin procesar)
curl -X PATCH http://127.0.0.1:8000/api/v1/admin/tenants/<TENANT_ID>/plan \
  -H "Authorization: Bearer <TOKEN_PLATFORM_ADMIN>" \
  -H "Content-Type: application/json" \
  -d '{"status":"suspended"}'
# reactivar: '{"status":"active"}'
```
El token lo obtienes con `POST /api/v1/admin/auth/login`.

### Cambiar plan y referencia de cobro
```bash
-d '{"plan":"renta","billing_ref":"MP-SUB-12345"}'
# planes válidos: compra_unica | renta
```

### Logs por tenant (con LIAH_LOG_JSON=true)
```bash
docker compose -f infra/docker-compose.prod.yml logs -f api | grep '"tenant_id": "<UUID>"'
```

### Respaldos
```bash
docker compose -f infra/docker-compose.prod.yml exec postgres \
  pg_dump -U pyme pyme_agent > respaldo-$(date +%F).sql
```
Automatízalo con cron en el VPS. Prueba restaurar una vez al mes.

### Actualizar a una versión nueva
```bash
git pull && git checkout skeleton
docker compose -f infra/docker-compose.prod.yml up -d --build
# "migrate" aplica solo las migraciones pendientes (alembic upgrade head)
```

## 5. Seguridad mínima del VPS

- UFW: permite 22, 80, 443. **No expongas 5432 ni 8000** al público
  (el compose no publica postgres; la API va detrás de nginx/traefik con TLS).
- `APP_ENV=production` + secretos largos en `infra/.env` (la app se niega a
  arrancar en producción con secretos default).
- Rate limiting activo por defecto (`LIAH_RATE_LIMIT_ENABLED=true`):
  login 10/min/IP, webhook 240/min/IP. `/health` exento.
- Nunca subas `infra/.env` a git.

## 6. Costos que tú absorbes (modo renta)

El panel registra uso de OpenAI por tenant (`/admin/metrics`). Calibra tus
precios de renta con datos reales de 2–4 semanas antes de prometer
"mensualidad fija" a muchos clientes. En modo **compra única** el cliente
pone su propia `OPENAI_API_KEY` y su cuenta de Meta: tú no absorbes nada.

## 7. Limitaciones honestas de esta versión

- Sin RLS en Postgres (aislamiento lógico por `tenant_ctx` + tests).
  RLS antes de vender a terceros que compartan la misma BD.
- Rate limit en memoria por proceso (1 worker de uvicorn). Con N réplicas,
  mover a Redis.
- Transcripción de voz: stub (Whisper real pendiente).
- Sin panel web para altas/bajas de staff (hoy es por API/WhatsApp).
