#!/usr/bin/env python3
"""Demo end-to-end — NEGOCIO MÓVIL con traslados (Fase 7e).

Da de alta un negocio ficticio de espejo mágico para eventos con la
plantilla `templates/espejo_magico.yaml` (onboarding real) y demuestra el
motor de disponibilidad con recursos MÓVILES, buffers y traslados:

  1. Evento 1: renta-espejo-3h el día D de 16:00 a 20:00, sede "Xalapa"
     -> OK. Muestra buffers (setup 45 / teardown 30) y el intervalo
     realmente ocupado del recurso.
  2. Evento 2: mismo día 19:00, sede "Veracruz" -> RECHAZADO (el espejo
     sigue ocupado: con buffers queda libre hasta las 19:30).
  3. Evento 2b: mismo día 20:30, sede "Veracruz" -> RECHAZADO por
     TRASLADO imposible: solo hay 15 min de hueco y la política
     per_zone exige 60 min entre sedes distintas (veredicto explícito).
  4. Evento 2: al día siguiente (D+1) 16:00, sede "Veracruz" -> OK.

BD: usa `pyme_agent_demo_espejo` (se crea si no existe), NUNCA la BD de
test de pytest. Sin `--reset` no borra nada: reusa el tenant y limpia
solo sus propios artefactos del run anterior.

Uso:
    python scripts/demo_espejo_magico.py            # corrida normal
    python scripts/demo_espejo_magico.py --reset    # borra y empieza de cero

Veredicto: imprime `DEMO OK (4/4)` o la lista de fallos; el exit code es
!= 0 si algo falla.
"""
import os
import sys
import time
from datetime import timedelta
from zoneinfo import ZoneInfo

DB_DEFAULT = "postgresql+asyncpg://pyme:pyme@127.0.0.1:5433/pyme_agent_demo_espejo"
DB_URL = os.getenv("LIAH_DEMO_DATABASE_URL", DB_DEFAULT)
os.environ["DATABASE_URL"] = DB_URL
os.environ.setdefault("LIAH_SEND_DRY_RUN", "1")  # jamás llamar a Meta en la demo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import demo_lib  # noqa: E402

TEMPLATE = "espejo_magico"
SLUG = "espejo-demo"
SERVICE = "renta-espejo-3h"
TENANT_TZ = demo_lib.TENANT_TZ


def _future_date(days_ahead: int) -> str:
    return (
        __import__("datetime").datetime.now(ZoneInfo(TENANT_TZ))
        + timedelta(days=days_ahead)
    ).date().isoformat()


async def _run(reset: bool) -> int:
    print("=" * 64)
    print("DEMO LIAH — Espejo mágico móvil: recursos, buffers y traslados")
    print("=" * 64)
    t0 = time.time()
    results: list = []

    print("[0/5] Base de datos demo")
    await demo_lib.ensure_database(DB_URL)
    demo_lib.wire_db(DB_URL)
    await demo_lib.prepare_schema(reset)

    # Imports de la app: DESPUÉS de wire_db.
    from sqlalchemy import select  # noqa: E402

    from app.agent.calendar import MemoryCalendarAdapter  # noqa: E402
    from app.core import db as db_mod  # noqa: E402
    from app.models import ServiceType  # noqa: E402

    print("[1/5] Alta del negocio (onboarding real)")
    async with db_mod.async_session_maker() as s:
        tenant_id = await demo_lib.get_or_create_tenant(
            s, SLUG, TEMPLATE, "Espejo Demo Liah",
            "demo@espejo-demo.example",
        )
        st = (
            await s.execute(
                select(ServiceType).where(
                    ServiceType.tenant_id == tenant_id,
                    ServiceType.slug == SERVICE,
                )
            )
        ).scalar_one()
        setup = (st.buffers or {}).get("setup")
        teardown = (st.buffers or {}).get("teardown")
        traslado = st.traslado or {}
        print(f"  [cfg] {st.nombre}: duración {st.duracion_min} min, "
              f"setup {setup} min, teardown {teardown} min, "
              f"traslado modo={traslado.get('modo')} "
              f"default={traslado.get('default_min')} min")
        await s.commit()

    day = _future_date(7)
    day2 = _future_date(8)
    wa_ids = ["521550021001", "521550021002", "521550021003", "521550021004"]

    print(f"[2/5] Evento 1: {day} 16:00-20:00, sede 'Xalapa'")
    try:
        async with db_mod.async_session_maker() as s:
            c1 = await demo_lib.get_or_create_contact(
                s, tenant_id, wa_ids[0], name="Cliente Evento 1")
            await demo_lib.cleanup_demo_appointments(
                s, tenant_id, [c1.id])
            cal = MemoryCalendarAdapter(s, tenant_id, tz=TENANT_TZ)
            r1 = await cal.book(
                str(c1.id), day, "16:00", "renta",
                service_type_slug=SERVICE, venue="Xalapa",
                idempotency_key=f"demo-espejo-e1-{day}",
            )
            await s.commit()
        if r1["ok"]:
            detail = ("agendado; el espejo queda ocupado [15:15, 19:30] "
                      "(16:00-20:00 + setup 45 + teardown 30)")
            results.append(("evento-1 xalapa 16:00", True, detail))
        else:
            results.append(("evento-1 xalapa 16:00", False,
                            f"no se pudo agendar: {r1.get('error')}"))
    except Exception as e:  # noqa: BLE001
        results.append(("evento-1 xalapa 16:00", False,
                        f"{type(e).__name__}: {e}"))

    print("[3/5] Evento 2: mismo día 19:00, sede 'Veracruz' (traslape)")
    try:
        async with db_mod.async_session_maker() as s:
            c2 = await demo_lib.get_or_create_contact(
                s, tenant_id, wa_ids[1], name="Cliente Evento 2")
            await demo_lib.cleanup_demo_appointments(
                s, tenant_id, [c2.id])
            cal = MemoryCalendarAdapter(s, tenant_id, tz=TENANT_TZ)
            r2 = await cal.book(
                str(c2.id), day, "19:00", "renta",
                service_type_slug=SERVICE, venue="Veracruz",
                idempotency_key=f"demo-espejo-e2a-{day}",
            )
            await s.commit()
        if not r2["ok"] and r2.get("error"):
            results.append((
                "evento-2 19:00 rechazado",
                True,
                f"RECHAZADO como se esperaba: {r2['error']}",
            ))
        else:
            results.append((
                "evento-2 19:00 rechazado",
                False,
                f"se esperaba rechazo y el book devolvió ok={r2['ok']}",
            ))
    except Exception as e:  # noqa: BLE001
        results.append(("evento-2 19:00 rechazado", False,
                        f"{type(e).__name__}: {e}"))

    print("[4/5] Evento 2b: mismo día 20:30, sede 'Veracruz' (traslado)")
    try:
        from app.agent import availability as availmod  # noqa: E402

        async with db_mod.async_session_maker() as s:
            chk = await availmod.check_resource_availability(
                s, tenant_id, SERVICE, day, "20:30", venue="Veracruz",
            )
        travel = [c for c in chk["conflicts"] if c["type"] == "travel"]
        cap = [c for c in chk["conflicts"] if c["type"] == "resource_capacity"]
        if not chk["available"] and travel and not cap:
            results.append((
                "evento-2b 20:30 traslado imposible",
                True,
                f"RECHAZADO por traslado (veredicto explícito): "
                f"{travel[0]['detail']}",
            ))
        else:
            results.append((
                "evento-2b 20:30 traslado imposible",
                False,
                f"se esperaba SOLO conflicto de traslado; "
                f"available={chk['available']} conflictos={chk['conflicts']}",
            ))
    except Exception as e:  # noqa: BLE001
        results.append(("evento-2b 20:30 traslado imposible", False,
                        f"{type(e).__name__}: {e}"))

    print(f"[5/5] Evento 2: día siguiente ({day2}) 16:00, sede 'Veracruz'")
    try:
        async with db_mod.async_session_maker() as s:
            c3 = await demo_lib.get_or_create_contact(
                s, tenant_id, wa_ids[2], name="Cliente Evento 3")
            await demo_lib.cleanup_demo_appointments(
                s, tenant_id, [c3.id])
            cal = MemoryCalendarAdapter(s, tenant_id, tz=TENANT_TZ)
            r3 = await cal.book(
                str(c3.id), day2, "16:00", "renta",
                service_type_slug=SERVICE, venue="Veracruz",
                idempotency_key=f"demo-espejo-e3-{day2}",
            )
            await s.commit()
        if r3["ok"]:
            results.append((
                "evento-3 día siguiente veracruz 16:00",
                True,
                "agendado: con hueco suficiente (día distinto) el traslado "
                "ya no bloquea",
            ))
        else:
            results.append((
                "evento-3 día siguiente veracruz 16:00",
                False,
                f"no se pudo agendar: {r3.get('error')}",
            ))
    except Exception as e:  # noqa: BLE001
        results.append(("evento-3 día siguiente veracruz 16:00", False,
                        f"{type(e).__name__}: {e}"))

    print(f"\nTiempo total: {time.time() - t0:.1f}s")
    return demo_lib.report(results, "espejo mágico móvil")


def main() -> int:
    reset, _ = demo_lib.parse_args("Demo: espejo mágico móvil.", DB_DEFAULT)
    if reset:
        print(f"ATENCIÓN: --reset borra el esquema de la BD demo "
              f"({DB_URL.rsplit('/', 1)[-1]}).")
    return demo_lib.run(lambda: _run(reset), None)


if __name__ == "__main__":
    sys.exit(main())
