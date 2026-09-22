"""Infra común de las demos de Fase 7e.

Cada demo usa su propia BD (`pyme_agent_demo_*`, se crea si no existe),
NUNCA la BD de test de pytest. Cualquier `drop_all`/reset exige `--reset`
o `LIAH_DEMO_RESET=1`: por default no se borra nada (se reusa el tenant si
el slug ya existe y cada demo limpia solo sus propios artefactos).

El alta usa el onboarding REAL (`app/api/onboarding.py::onboard_tenant`,
la misma función que el CLI y el endpoint).
"""
import argparse
import asyncio
import os
import secrets
import sys
import time
from urllib.parse import urlparse

TENANT_TZ = "America/Mexico_City"


def parse_args(desc: str, db_default: str):
    p = argparse.ArgumentParser(description=desc)
    p.add_argument("--reset", action="store_true",
                   help="Borra el esquema de la BD demo y empieza de cero.")
    args = p.parse_args()
    reset = args.reset or os.getenv("LIAH_DEMO_RESET") == "1"
    db_url = os.getenv("LIAH_DEMO_DATABASE_URL", db_default)
    return reset, db_url


async def ensure_database(db_url: str) -> None:
    parsed = urlparse(db_url.replace("+asyncpg", ""))
    dbname = parsed.path.lstrip("/")
    admin_url = (f"postgresql://{parsed.username}:{parsed.password}@"
                 f"{parsed.hostname}:{parsed.port}/postgres")
    import asyncpg  # import tardío: solo se necesita aquí

    conn = await asyncpg.connect(admin_url)
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", dbname
        )
        if not exists:
            await conn.execute(f'CREATE DATABASE "{dbname}"')
            print(f"  [db] creada base de datos '{dbname}'")
        else:
            print(f"  [db] base de datos '{dbname}' ya existe (se reusa)")
    finally:
        await conn.close()


def wire_db(db_url: str) -> None:
    """Reapunta el engine de la app a la BD de la demo (antes de importarla)."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.core import db as db_mod
    from app.core.config import get_settings

    engine = create_async_engine(db_url, echo=False, poolclass=NullPool)
    db_mod.engine = engine
    db_mod.async_session_maker = async_sessionmaker(engine,
                                                    expire_on_commit=False)
    get_settings.cache_clear()


async def prepare_schema(reset: bool) -> None:
    import asyncpg

    from app.core import db as db_mod
    from app.core.base import Base
    import app.models  # noqa: F401 - registra los modelos en Base.metadata

    url = os.environ["DATABASE_URL"].replace("+asyncpg", "")
    conn = await asyncpg.connect(url)
    try:
        await conn.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp";')
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    finally:
        await conn.close()
    async with db_mod.engine.begin() as conn:
        if reset:
            print("  [db] --reset: drop_all + create_all")
            await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)


async def get_or_create_tenant(session, slug: str, template: str,
                               nombre: str, admin_email: str):
    """Onboarding real si el slug no existe; reusa si ya existe."""
    from sqlalchemy import select

    from app.agent.embedder import FakeEmbedder
    from app.api.onboarding import onboard_tenant
    from app.models import Tenant
    import uuid

    existing = (
        await session.execute(select(Tenant).where(Tenant.slug == slug))
    ).scalar_one_or_none()
    if existing is not None:
        print(f"  [onboard] tenant '{slug}' ya existe: se reusa "
              f"(sin --reset no se recrea)")
        return existing.id
    print(f"  [onboard] dando de alta '{slug}' con plantilla '{template}' "
          f"(onboarding real, transaccional)...")
    result = await onboard_tenant(
        session,
        template_name=template,
        slug=slug,
        nombre=nombre,
        overrides=None,
        admin_email=admin_email,
        admin_password=secrets.token_urlsafe(16),  # solo para el alta
        embedder=FakeEmbedder(),
    )
    print(f"  [onboard] ok: tenant_id={result['tenant_id']} "
          f"giro={result['giro']} resumen={result['summary']}")
    return uuid.UUID(result["tenant_id"])


async def get_or_create_contact(session, tenant_id, wa_id, name=None,
                                consent="granted", privacy_terms_version=None):
    from sqlalchemy import select

    from app.models import Contact

    contact = (
        await session.execute(
            select(Contact).where(
                Contact.tenant_id == tenant_id, Contact.wa_id == wa_id
            )
        )
    ).scalar_one_or_none()
    if contact is None:
        contact = Contact(tenant_id=tenant_id, wa_id=wa_id, name=name,
                          consent_status=consent,
                          privacy_terms_version=privacy_terms_version)
        session.add(contact)
        await session.flush()
    elif contact.consent_status != consent:
        contact.consent_status = consent
        await session.flush()
    return contact


async def cleanup_demo_appointments(session, tenant_id, contact_ids) -> None:
    """Borra las citas de los contactos demo + sus filas de recursos y
    idempotency keys de booking (artefactos propios de la demo)."""
    from sqlalchemy import delete, select

    from app.models import (
        ActionLog,
        Appointment,
        AppointmentResource,
    )

    appt_ids = (
        await session.execute(
            select(Appointment.id).where(
                Appointment.tenant_id == tenant_id,
                Appointment.contact_id.in_(contact_ids),
            )
        )
    ).scalars().all()
    if appt_ids:
        await session.execute(
            delete(AppointmentResource).where(
                AppointmentResource.appointment_id.in_(appt_ids)
            )
        )
        await session.execute(
            delete(Appointment).where(Appointment.id.in_(appt_ids))
        )
    await session.execute(
        delete(ActionLog).where(
            ActionLog.tenant_id == tenant_id,
            ActionLog.contact_id.in_(contact_ids),
            ActionLog.action == "book_appointment",
        )
    )
    await session.commit()


def report(results: list, title: str) -> int:
    print()
    print("=" * 64)
    ok = sum(1 for _, passed, _ in results if passed)
    for name, passed, detail in results:
        mark = "PASS" if passed else "FAIL"
        print(f"  [{mark}] {name}: {detail}")
    print("=" * 64)
    if ok == len(results):
        print(f"DEMO OK ({ok}/{len(results)}) — {title}")
        return 0
    print(f"DEMO CON FALLOS ({ok}/{len(results)}) — {title}")
    return 1


def run(coro_factory, results_sink):
    """Ejecuta la demo y maneja excepciones no controladas."""
    try:
        import asyncio

        return asyncio.run(coro_factory())
    except Exception as e:  # noqa: BLE001
        print(f"\nDEMO FALLIDA (excepción no controlada): "
              f"{type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return 2
