# Makefile — Esqueleto Liah (Fase 5)
#
# Uso típico (instalación local desde cero):
#   make up                      # postgres -> BD -> migraciones -> api + worker
#   make seed-admin               # primer platform_admin del panel
#   make onboard TENANT=mi-clinica TEMPLATE=consultorio_medico
#   make demo                     # demo end-to-end (4 rutas)
#   make down                     # detiene api + worker
#
# Variables:
#   VENV=...            venv a usar/crear (default: ../.venv si existe, si no .venv)
#   DATABASE_URL=...    default: postgres local 127.0.0.1:5433, BD pyme_agent
#   TENANT=slug         requerido por `make onboard`
#   TEMPLATE=giro       plantilla de templates/ (default: consultorio_medico)

# ── Venv: usa ../.venv si existe (layout de este workspace), si no .venv ──
VENV := $(firstword $(wildcard ../.venv .venv) .venv)
PY := $(VENV)/bin/python

# ── Postgres ───────────────────────────────────────────────────────────────
PGHOST ?= 127.0.0.1
PGPORT ?= 5433
PGUSER ?= pyme
PGPASSWORD ?= pyme
PGDB ?= pyme_agent
export PGPASSWORD

DATABASE_URL ?= postgresql+asyncpg://$(PGUSER):$(PGPASSWORD)@$(PGHOST):$(PGPORT)/$(PGDB)
export DATABASE_URL

TEMPLATE ?= consultorio_medico
ADMIN_EMAIL ?= admin@$(TENANT).example.com
# Overrides JSON del perfil (p.ej. '{"model_routing":{"embedder":"fake"}}' en
# dev sin OPENAI_API_KEY). Los arrays se reemplazan completos.
OVERRIDES_JSON ?= {}

LOGS := logs

.PHONY: help venv up down migrate test onboard demo seed-admin check-db

help:
	@echo "Targets:"
	@echo "  make up         postgres -> crea BD -> alembic upgrade head -> api + worker (logs/)"
	@echo "  make down       detiene api + worker"
	@echo "  make migrate    alembic upgrade head"
	@echo "  make test       pytest (BD pyme_agent_test, sin proxies)"
	@echo "  make onboard TENANT=slug [TEMPLATE=consultorio_medico] [ADMIN_EMAIL=...]"
	@echo "  make demo       demo end-to-end del consultorio (4 rutas)"
	@echo "  make seed-admin primer platform_admin (LIAH_ADMIN_PASSWORD o prompt)"

venv:
	@if [ ! -x "$(PY)" ]; then \
		echo "==> creando venv en $(VENV)"; \
		python3 -m venv "$(VENV)" || { echo "ERROR: no se pudo crear el venv (¿python3 instalado?)"; exit 1; }; \
		"$(PY)" -m pip install -e ".[test]" || { echo "ERROR: falló pip install -e \".[test]\""; exit 1; }; \
	else \
		echo "==> venv ok: $(VENV)"; \
	fi

check-db:
	@pg_isready -h $(PGHOST) -p $(PGPORT) >/dev/null 2>&1 || { \
		echo "ERROR: Postgres no responde en $(PGHOST):$(PGPORT)."; \
		echo "  Instálalo y levántalo, p.ej.:"; \
		echo "    sudo apt install postgresql-16 postgresql-16-pgvector"; \
		echo "    sudo pg_ctlcluster 16 main start   # o crea tu cluster en el puerto $(PGPORT)"; \
		echo "  Luego crea el rol/BD: CREATE ROLE $(PGUSER) SUPERUSER LOGIN PASSWORD '$(PGPASSWORD)'; CREATE DATABASE $(PGDB);"; \
		exit 1; }
	@echo "==> postgres responde en $(PGHOST):$(PGPORT)"
	@psql -h $(PGHOST) -p $(PGPORT) -U $(PGUSER) -d postgres -tc \
		"SELECT 1 FROM pg_database WHERE datname='$(PGDB)'" | grep -q 1 || { \
		echo "==> creando base de datos $(PGDB)"; \
		createdb -h $(PGHOST) -p $(PGPORT) -U $(PGUSER) $(PGDB) || { \
			echo "ERROR: no se pudo crear la BD $(PGDB) (¿existe el rol $(PGUSER)?)"; exit 1; }; }

migrate: venv check-db
	@echo "==> alembic upgrade head"
	@"$(PY)" -m alembic -c migrations/alembic.ini upgrade head || { \
		echo "ERROR: falló la migración. Revisa DATABASE_URL y que la extensión pgvector esté instalada."; exit 1; }

up: migrate
	@mkdir -p $(LOGS)
	@if [ -f $(LOGS)/api.pid ] && kill -0 $$(cat $(LOGS)/api.pid) 2>/dev/null; then \
		echo "ERROR: la api ya corre (pid $$(cat $(LOGS)/api.pid)). Usa 'make down' primero."; exit 1; fi
	@echo "==> levantando api (uvicorn :8000, log en $(LOGS)/api.log)"
	@nohup "$(PY)" -m uvicorn app.main:app --host 0.0.0.0 --port 8000 >$(LOGS)/api.log 2>&1 & echo $$! > $(LOGS)/api.pid
	@echo "==> levantando worker (python -m app.worker, log en $(LOGS)/worker.log)"
	@nohup "$(PY)" -m app.worker >$(LOGS)/worker.log 2>&1 & echo $$! > $(LOGS)/worker.pid
	@sleep 2
	@curl -sf http://127.0.0.1:8000/health >/dev/null && echo "==> api OK: http://127.0.0.1:8000/health" || \
		{ echo "ERROR: la api no responde en :8000; revisa $(LOGS)/api.log"; exit 1; }
	@echo "==> worker pid $$(cat $(LOGS)/worker.pid) (revisa $(LOGS)/worker.log)"

down:
	@for svc in api worker; do \
		if [ -f $(LOGS)/$$svc.pid ]; then \
			pid=$$(cat $(LOGS)/$$svc.pid); \
			if kill -0 $$pid 2>/dev/null; then kill $$pid && echo "==> $$svc detenido (pid $$pid)"; \
			else echo "==> $$svc ya no corría (pid $$pid huérfano)"; fi; \
			rm -f $(LOGS)/$$svc.pid; \
		else echo "==> $$svc no estaba levantado por make"; fi; \
	done

test: venv
	@echo "==> pytest (usa pyme_agent_test)"
	@env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy \
		-u all_proxy -u NO_PROXY -u no_proxy \
		TEST_DATABASE_URL="postgresql+asyncpg://$(PGUSER):$(PGPASSWORD)@$(PGHOST):$(PGPORT)/pyme_agent_test" \
		"$(PY)" -m pytest -q

onboard: venv check-db
	@if [ -z "$(TENANT)" ]; then \
		echo "ERROR: falta TENANT. Uso: make onboard TENANT=mi-clinica [TEMPLATE=consultorio_medico]"; exit 1; fi
	@if [ ! -f "templates/$(TEMPLATE).yaml" ]; then \
		echo "ERROR: no existe templates/$(TEMPLATE).yaml. Plantillas disponibles:"; ls templates/*.yaml; exit 1; fi
	@echo "==> onboard: plantilla $(TEMPLATE), slug $(TENANT), admin $(ADMIN_EMAIL)"
	@LIAH_TENANT_ADMIN_PASSWORD="$${LIAH_TENANT_ADMIN_PASSWORD:-}" \
		"$(PY)" scripts/onboard_tenant.py "templates/$(TEMPLATE).yaml" \
		--slug "$(TENANT)" --admin-email "$(ADMIN_EMAIL)" \
		--overrides-json '$(OVERRIDES_JSON)' || { \
		echo "ERROR: falló el onboarding (¿slug duplicado? ¿password <12 caracteres?"; \
		echo "  ¿falta OPENAI_API_KEY? En dev sin key usa OVERRIDES_JSON='{\"model_routing\":{\"embedder\":\"fake\"}}')"; exit 1; }

demo: venv
	@echo "==> demo end-to-end (BD pyme_agent_demo; --reset borra)"
	@"$(PY)" scripts/demo_consultorio.py $(DEMO_ARGS)

seed-admin: venv check-db
	@echo "==> seed del primer platform_admin (LIAH_ADMIN_PASSWORD o prompt)"
	@"$(PY)" scripts/seed_platform_admin.py
