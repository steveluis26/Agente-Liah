#!/usr/bin/env python3
"""Alta de un cliente desde una plantilla de giro (Fase 4, CLI de operador).

Uso:
    python scripts/onboard_tenant.py templates/consultorio_medico.yaml \
        --slug clinica-norte --nombre "Clínica Norte" \
        --admin-email admin@clinica-norte.mx

    # con overrides (JSON) para personalizar sin editar el YAML:
    python scripts/onboard_tenant.py templates/consultorio_medico.yaml \
        --slug clinica-norte --admin-email admin@clinica-norte.mx \
        --overrides-json '{"tono": "formal", "model_routing": {"embedder": "fake"}}'

El password del tenant_admin sale de la variable LIAH_TENANT_ADMIN_PASSWORD
o de un prompt interactivo (getpass). JAMÁS hay un default (mínimo 12
caracteres). La conexión usa DATABASE_URL (ver .env / app/core/config.py).

Equivale a llamar POST /api/v1/admin/tenants/onboard con credenciales de
platform_admin, pero directo contra la BD para el operador local.
"""
import argparse
import asyncio
import getpass
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.api.onboarding import (  # noqa: E402
    MIN_ADMIN_PASSWORD_LEN,
    OnboardingError,
    onboard_tenant,
)
from app.core import db as db_mod  # noqa: E402


def _read_password() -> str:
    pw = os.getenv("LIAH_TENANT_ADMIN_PASSWORD")
    if pw:
        return pw
    if not sys.stdin.isatty():
        raise SystemExit(
            "ERROR: no hay LIAH_TENANT_ADMIN_PASSWORD y la terminal no es "
            "interactiva. Define la variable o corre el script en una terminal."
        )
    pw = getpass.getpass("Password para el tenant_admin del nuevo cliente: ")
    pw2 = getpass.getpass("Confirma el password: ")
    if pw != pw2:
        raise SystemExit("ERROR: los passwords no coinciden.")
    return pw


def _parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Da de alta un cliente Liah desde una plantilla de giro."
    )
    p.add_argument("template", help="Ruta al YAML (templates/<giro>.yaml)")
    p.add_argument("--slug", required=True, help="Slug único del tenant")
    p.add_argument("--nombre", default=None, help="Nombre comercial (default: el de la plantilla)")
    p.add_argument("--admin-email", required=True, help="Email del tenant_admin")
    p.add_argument(
        "--overrides-json",
        default="{}",
        help="JSON con overrides del perfil (ej. tono, horarios, model_routing)",
    )
    return p.parse_args(argv)


async def _run(args) -> dict:
    try:
        overrides = json.loads(args.overrides_json)
    except json.JSONDecodeError as e:
        raise SystemExit(f"ERROR: --overrides-json no es JSON válido: {e}")
    if not isinstance(overrides, dict):
        raise SystemExit("ERROR: --overrides-json debe ser un objeto JSON")

    template_name = os.path.splitext(os.path.basename(args.template))[0]
    async with db_mod.async_session_maker() as session:
        return await onboard_tenant(
            session,
            template_name=template_name,
            slug=args.slug,
            nombre=args.nombre,
            overrides=overrides,
            admin_email=args.admin_email,
            admin_password=_read_password(),
        )


def main(argv=None) -> int:
    """Punto de entrada (también invocable desde tests). Devuelve exit code."""
    args = _parse_args(argv)
    password = os.getenv("LIAH_TENANT_ADMIN_PASSWORD") or ""
    if password and len(password) < MIN_ADMIN_PASSWORD_LEN:
        print(
            f"ERROR: LIAH_TENANT_ADMIN_PASSWORD debe tener al menos "
            f"{MIN_ADMIN_PASSWORD_LEN} caracteres.",
            file=sys.stderr,
        )
        return 2
    try:
        result = asyncio.run(_run(args))
    except OnboardingError as e:
        print(f"ERROR [{e.code}]: {e}", file=sys.stderr)
        return 2
    except SystemExit as e:
        print(e, file=sys.stderr)
        return 2
    except Exception:
        traceback.print_exc()
        return 1

    print("Cliente dado de alta:")
    print(f"  tenant_id : {result['tenant_id']}")
    print(f"  slug      : {result['slug']}")
    print(f"  nombre    : {result['nombre']}")
    print(f"  giro      : {result['giro']} (plantilla: {result['template']})")
    print(f"  admin     : {result['admin_email']} (rol tenant_admin)")
    print(f"  resumen   : {json.dumps(result['summary'], ensure_ascii=False)}")
    print()
    print("  api_key   : " + result["api_key"])
    print("  ^^^ GUÁRDALA AHORA: no se vuelve a mostrar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
