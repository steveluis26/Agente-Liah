#!/usr/bin/env python3
"""Demo breve — MODO STAFF por WhatsApp (Fase 7e).

Da de alta una clínica con la plantilla `templates/consultorio_medico.yaml`
(onboarding real), registra dos miembros del staff (owner + especialista) y
demuestra con veredictos sobre resultados REALES:

  1. "mi agenda de hoy" (owner) -> lista las citas confirmadas de hoy,
     con hora y paciente.
  2. "¿quién es el de las 10:30?" -> nombre del paciente de esa cita.
  3. "¿qué huecos hay mañana?" -> huecos libres reales del motor de
     disponibilidad.
  4. Resumen matutino: con `staff_briefing` habilitado, el scheduler envía
     el briefing del día al staff (verificado en los mensajes outbound
     persistidos).
  5. Scoping por rol: la especialista solo ve su propia agenda.

BD: usa `pyme_agent_demo_staff` (se crea si no existe), NUNCA la BD de
test de pytest. Sin `--reset` no borra nada: reusa el tenant y limpia
solo sus propios artefactos del run anterior.

Uso:
    python scripts/demo_staff.py
    python scripts/demo_staff.py --reset

Veredicto: imprime `DEMO OK (5/5)` o la lista de fallos; el exit code es
!= 0 si algo falla.
"""
import os
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

DB_DEFAULT = "postgresql+asyncpg://pyme:pyme@127.0.0.1:5433/pyme_agent_demo_staff"
DB_URL = os.getenv("LIAH_DEMO_DATABASE_URL", DB_DEFAULT)
os.environ["DATABASE_URL"] = DB_URL
os.environ.setdefault("LIAH_SEND_DRY_RUN", "1")  # jamás llamar a Meta en la demo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import demo_lib  # noqa: E402

TEMPLATE = "consultorio_medico"
SLUG = "clinica-staff-demo"
TENANT_TZ = demo_lib.TENANT_TZ

OWNER_WA = "521550041001"
SPEC_WA = "521550041002"


async def _run(reset: bool) -> int:
    print("=" * 64)
    print("DEMO LIAH — Modo staff por WhatsApp + resumen matutino")
    print("=" * 64)
    t0 = time.time()
    results: list = []

    print("[0/5] Base de datos demo")
    await demo_lib.ensure_database(DB_URL)
    demo_lib.wire_db(DB_URL)
    await demo_lib.prepare_schema(reset)

    # Imports de la app: DESPUÉS de wire_db.
    from sqlalchemy import delete, select  # noqa: E402

    from app.agent.calendar import MemoryCalendarAdapter  # noqa: E402
    from app.agent.staff import handle_staff_message  # noqa: E402
    from app.core import db as db_mod  # noqa: E402
    from app.models import (  # noqa: E402
        ActionLog,
        Message,
        Resource,
        StaffMember,
        TenantConfig,
    )
    from app.reminders.staff_briefing import (  # noqa: E402
        send_due_staff_briefings,
    )

    now_local = datetime.now(ZoneInfo(TENANT_TZ))
    today = now_local.date().isoformat()
    briefing_hour = (now_local - timedelta(minutes=5)).strftime("%H:%M")

    print("[1/5] Alta de la clínica + staff (onboarding real)")
    try:
        async with db_mod.async_session_maker() as s:
            tenant_id = await demo_lib.get_or_create_tenant(
                s, SLUG, TEMPLATE, "Clínica Staff Demo",
                "demo@clinica-staff.example",
            )
            # Staff: owner (todo) + especialista (solo pediatría).
            for wa_id, nombre, role, res_slug in [
                (OWNER_WA, "Dueña Demo", "owner", None),
                (SPEC_WA, "Dra. Pediatra Demo", "specialist", "pediatra"),
            ]:
                m = (
                    await s.execute(
                        select(StaffMember).where(
                            StaffMember.tenant_id == tenant_id,
                            StaffMember.wa_id == wa_id,
                        )
                    )
                ).scalar_one_or_none()
                resource_id = None
                if res_slug:
                    resource_id = (
                        await s.execute(
                            select(Resource.id).where(
                                Resource.tenant_id == tenant_id,
                                Resource.slug == res_slug,
                            )
                        )
                    ).scalar_one()
                if m is None:
                    s.add(StaffMember(
                        tenant_id=tenant_id, wa_id=wa_id, nombre=nombre,
                        role=role, resource_id=resource_id))
                elif m.resource_id != resource_id or m.role != role:
                    m.resource_id = resource_id
                    m.role = role
            # Briefing matutino habilitado a una hora ya pasada hoy (due).
            cfg = (
                await s.execute(
                    select(TenantConfig).where(
                        TenantConfig.tenant_id == tenant_id)
                )
            ).scalar_one_or_none()
            briefing_cfg = {
                "staff_briefing": {
                    "enabled": True,
                    "hour": briefing_hour,
                    "roles": ["owner", "specialist"],
                }
            }
            if cfg is None:
                s.add(TenantConfig(
                    tenant_id=tenant_id, system_prompt="demo",
                    extra=briefing_cfg))
            else:
                cfg.extra = {**(cfg.extra or {}), **briefing_cfg}
            await s.commit()

            # Citas de hoy: 10:30 medicina general, 12:00 pediatría.
            pa = await demo_lib.get_or_create_contact(
                s, tenant_id, "521550041101", name="Paciente Demo A")
            pb = await demo_lib.get_or_create_contact(
                s, tenant_id, "521550041102", name="Paciente Demo B")
            await demo_lib.cleanup_demo_appointments(
                s, tenant_id, [pa.id, pb.id])
            cal = MemoryCalendarAdapter(s, tenant_id, tz=TENANT_TZ)
            r1 = await cal.book(
                str(pa.id), today, "10:30", "consulta",
                service_type_slug="consulta-medicina-general",
                idempotency_key=f"demo-staff-hoy-{today}-1030")
            r2 = await cal.book(
                str(pb.id), today, "12:00", "consulta",
                service_type_slug="consulta-pediatria",
                idempotency_key=f"demo-staff-hoy-{today}-1200")
            await s.commit()
        ok = bool(r1["ok"]) and bool(r2["ok"])
        results.append((
            "alta + citas de hoy",
            ok,
            "staff registrado y 2 citas de hoy creadas (10:30 y 12:00)"
            if ok else f"book 10:30={r1} 12:00={r2}",
        ))
        seeded = ok
    except Exception as e:  # noqa: BLE001
        results.append(("alta + citas de hoy", False,
                        f"{type(e).__name__}: {e}"))
        seeded = False

    async def _staff(s, wa):
        return (
            await s.execute(
                select(StaffMember).where(
                    StaffMember.tenant_id == tenant_id,
                    StaffMember.wa_id == wa,
                )
            )
        ).scalar_one()

    print("[2/5] Comando: 'mi agenda de hoy' (owner)")
    try:
        async with db_mod.async_session_maker() as s:
            m = await _staff(s, OWNER_WA)
            reply = await handle_staff_message(s, tenant_id, m,
                                               "mi agenda de hoy")
        ok = "10:30" in reply and "Paciente Demo A" in reply \
            and "12:00" in reply
        results.append((
            "agenda de hoy",
            ok,
            f"la agenda lista las 2 citas de hoy: {reply[:120]!r}"
            if ok else f"respuesta inesperada: {reply!r}",
        ))
    except Exception as e:  # noqa: BLE001
        results.append(("agenda de hoy", False, f"{type(e).__name__}: {e}"))

    print("[3/5] Comando: '¿quién es el de las 10:30?'")
    try:
        async with db_mod.async_session_maker() as s:
            m = await _staff(s, OWNER_WA)
            reply = await handle_staff_message(
                s, tenant_id, m, "¿quién es el de las 10:30?")
        ok = "Paciente Demo A" in reply and "10:30" in reply
        results.append((
            "quién a las 10:30",
            ok,
            f"identifica al paciente: {reply!r}"
            if ok else f"respuesta inesperada: {reply!r}",
        ))
    except Exception as e:  # noqa: BLE001
        results.append(("quién a las 10:30", False,
                        f"{type(e).__name__}: {e}"))

    print("[4/5] Comando: '¿qué huecos hay mañana?' + scoping especialista")
    try:
        async with db_mod.async_session_maker() as s:
            m = await _staff(s, OWNER_WA)
            reply = await handle_staff_message(
                s, tenant_id, m, "¿qué huecos hay mañana?")
            me = await _staff(s, SPEC_WA)
            reply_spec = await handle_staff_message(
                s, tenant_id, me, "mi agenda de hoy")
        ok_owner = "Huecos libres mañana" in reply
        # La especialista solo ve su agenda: la cita de pediatría (12:00),
        # no la de medicina general (10:30).
        ok_spec = "12:00" in reply_spec and "10:30" not in reply_spec
        ok = ok_owner and ok_spec
        results.append((
            "huecos mañana + scoping",
            ok,
            ("owner ve huecos reales; la especialista solo ve su cita "
             "de las 12:00")
            if ok else
            f"huecos={reply[:100]!r} agenda_spec={reply_spec[:100]!r}",
        ))
    except Exception as e:  # noqa: BLE001
        results.append(("huecos mañana + scoping", False,
                        f"{type(e).__name__}: {e}"))

    print("[5/5] Resumen matutino (briefing due ahora)")
    try:
        async with db_mod.async_session_maker() as s:
            # Limpia el idempotency del briefing de hoy (artefacto propio)
            # para que la demo sea repetible.
            day_key = today
            await s.execute(
                delete(ActionLog).where(
                    ActionLog.tenant_id == tenant_id,
                    ActionLog.idempotency_key.like(
                        f"staff-briefing:{tenant_id}:{day_key}%"),
                )
            )
            await s.commit()
        out = await send_due_staff_briefings(
            db_mod.async_session_maker, dry_run=True,
            now=now_local,
        )
        sent = out.get(str(tenant_id), {}).get("sent", 0)
        async with db_mod.async_session_maker() as s:
            texts = (
                await s.execute(
                    select(Message.content).where(
                        Message.tenant_id == tenant_id,
                        Message.direction == "outbound",
                    ).order_by(Message.created_at.desc())
                )
            ).scalars().all()
        briefings = [t for t in texts if "Buenos días" in (t or "")]
        # La owner recibe su agenda completa (10:30 y 12:00); la
        # especialista solo la suya (12:00).
        ok = (sent >= 2
              and len(briefings) >= 2
              and any("10:30" in b and "12:00" in b for b in briefings)
              and any("12:00" in b and "10:30" not in b for b in briefings))
        results.append((
            "briefing matutino",
            ok,
            (f"{sent} briefing(s) enviados con scoping por rol: la owner "
             f"recibe las 2 citas, la especialista solo la suya")
            if ok else
            f"sent={sent} stats={out.get(str(tenant_id))} "
            f"n_briefings={len(briefings)}",
        ))
    except Exception as e:  # noqa: BLE001
        results.append(("briefing matutino", False,
                        f"{type(e).__name__}: {e}"))

    print(f"\nTiempo total: {time.time() - t0:.1f}s")
    return demo_lib.report(results, "modo staff")


def main() -> int:
    reset, _ = demo_lib.parse_args("Demo: modo staff por WhatsApp.", DB_DEFAULT)
    if reset:
        print(f"ATENCIÓN: --reset borra el esquema de la BD demo "
              f"({DB_URL.rsplit('/', 1)[-1]}).")
    return demo_lib.run(lambda: _run(reset), None)


if __name__ == "__main__":
    sys.exit(main())
