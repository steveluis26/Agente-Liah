#!/usr/bin/env python3
"""Crea el primer operador `platform_admin` del panel (Fase 3).

- SOLO actúa si NO existe ningún usuario en `platform_users`: nunca duplica
  ni resetea passwords de usuarios existentes.
- El password viene de la variable `LIAH_ADMIN_PASSWORD` o de un prompt
  interactivo (getpass). JAMÁS hay un default: en producción un default
  sería una puerta trasera.
- Uso:  LIAH_ADMIN_PASSWORD='...' python scripts/seed_platform_admin.py
        # o interactivo:
        python scripts/seed_platform_admin.py
        # con email distinto:
        LIAH_ADMIN_EMAIL=admin@mi-dominio.mx python scripts/seed_platform_admin.py

La conexión usa DATABASE_URL (ver .env / app/core/config.py).
"""
import asyncio
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func, select  # noqa: E402

from app.core import db as db_mod  # noqa: E402  (acceso tardío: respeta la reconfiguración de tests)
from app.models import PlatformUser  # noqa: E402
from app.models.platform_users import (  # noqa: E402
    ROLE_PLATFORM_ADMIN,
    hash_password,
)


def _read_password() -> str:
    pw = os.getenv("LIAH_ADMIN_PASSWORD")
    if pw:
        return pw
    if not sys.stdin.isatty():
        raise SystemExit(
            "ERROR: no hay LIAH_ADMIN_PASSWORD y la terminal no es interactiva. "
            "Define LIAH_ADMIN_PASSWORD o corre el script en una terminal."
        )
    pw = getpass.getpass("Password para el primer platform_admin: ")
    pw2 = getpass.getpass("Confirma el password: ")
    if pw != pw2:
        raise SystemExit("ERROR: los passwords no coinciden.")
    if len(pw) < 12:
        raise SystemExit("ERROR: el password debe tener al menos 12 caracteres.")
    return pw


async def main() -> int:
    email = os.getenv("LIAH_ADMIN_EMAIL", "admin@liah.local").strip().lower()
    async with db_mod.async_session_maker() as session:
        existing = (
            await session.execute(select(func.count(PlatformUser.id)))
        ).scalar_one()
        if existing:
            print(
                f"Ya existen {existing} usuario(s) en platform_users: "
                "no se crea ni se modifica nada (seguridad: el seed nunca "
                "resetea passwords)."
            )
            return 0
        password = _read_password()
        user = PlatformUser(
            email=email,
            password_hash=hash_password(password),
            role=ROLE_PLATFORM_ADMIN,
            tenant_id=None,
        )
        session.add(user)
        await session.commit()
        print(f"platform_admin creado: {email}")
        print("Entra al panel en /admin con ese correo y password.")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
