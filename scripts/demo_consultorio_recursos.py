#!/usr/bin/env python3
"""Demo end-to-end — CONSULTORIO con recursos fijos + consentimiento (Fase 7e).

Da de alta una clínica con la plantilla `templates/consultorio_medico.yaml`
(onboarding real) y demuestra, con veredictos sobre resultados REALES:

  1. Cita 1: paciente A, consulta-medicina-general, mañana 10:00 -> OK
     (usa médico general + consultorio 1).
  2. Cita 2: paciente B, mismo servicio, misma hora -> RECHAZADA: el
     consultorio y el especialista no pueden traslaparse.
  3. Cita 3: paciente B, consulta-pediatría, mañana 10:30 -> OK (pediatra
     y consultorio 2 libres).
  4. Buffers: electrocardiograma (setup 5 / teardown 5) a las 11:00 -> OK;
     otro electro a las 11:20 -> RECHAZADO (traslape con el buffer).
  5. Consentimiento: contacto nuevo SIN consentimiento no puede agendar
     (la puerta de privacidad pide el aviso en el primer contacto);
     tras responder "sí", el consentimiento queda granted con la versión
     vigente y la agenda procede.

BD: usa `pyme_agent_demo_recursos` (se crea si no existe), NUNCA la BD de
test de pytest. Sin `--reset` no borra nada: reusa el tenant y limpia
solo sus propios artefactos del run anterior.

Uso:
    python scripts/demo_consultorio_recursos.py
    python scripts/demo_consultorio_recursos.py --reset

Veredicto: imprime `DEMO OK (5/5)` o la lista de fallos; el exit code es
!= 0 si algo falla.
"""
import os
import sys
import time
from datetime import timedelta
from zoneinfo import ZoneInfo

DB_DEFAULT = "postgresql+asyncpg://pyme:pyme@127.0.0.1:5433/pyme_agent_demo_recursos"
DB_URL = os.getenv("LIAH_DEMO_DATABASE_URL", DB_DEFAULT)
os.environ["DATABASE_URL"] = DB_URL
os.environ.setdefault("LIAH_SEND_DRY_RUN", "1")  # jamás llamar a Meta en la demo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import demo_lib  # noqa: E402

TEMPLATE = "consultorio_medico"
SLUG = "clinica-recursos-demo"
TENANT_TZ = demo_lib.TENANT_TZ

MG = "consulta-medicina-general"
PED = "consulta-pediatria"
ECG = "electrocardiograma"


def _tomorrow() -> str:
    return (
        __import__("datetime").datetime.now(ZoneInfo(TENANT_TZ))
        + timedelta(days=1)
    ).date().isoformat()


async def _book(s, cal, contact_id, date, hhmm, service, day_key, tag):
    return await cal.book(
        str(contact_id), date, hhmm, "consulta",
        service_type_slug=service,
        idempotency_key=f"demo-rec-{tag}-{day_key}-{hhmm}",
    )


async def _run(reset: bool) -> int:
    print("=" * 64)
    print("DEMO LIAH — Consultorio: recursos fijos + consentimiento")
    print("=" * 64)
    t0 = time.time()
    results: list = []

    print("[0/6] Base de datos demo")
    await demo_lib.ensure_database(DB_URL)
    demo_lib.wire_db(DB_URL)
    await demo_lib.prepare_schema(reset)

    # Imports de la app: DESPUÉS de wire_db.
    from app.agent.calendar import MemoryCalendarAdapter  # noqa: E402
    from app.agent.consent import (  # noqa: E402
        apply_privacy_gate,
        privacy_gate_error,
    )
    from app.core import db as db_mod  # noqa: E402

    print("[1/6] Alta de la clínica (onboarding real)")
    async with db_mod.async_session_maker() as s:
        tenant_id = await demo_lib.get_or_create_tenant(
            s, SLUG, TEMPLATE, "Clínica Recursos Demo",
            "demo@clinica-recursos.example",
        )
        await s.commit()

    day = _tomorrow()
    wa = {
        "a": "521550031001",
        "b": "521550031002",
        "c": "521550031003",
        "nuevo": "521550031009",
    }

    print(f"[2/6] Cita 1: paciente A, medicina general, {day} 10:00")
    try:
        async with db_mod.async_session_maker() as s:
            ca = await demo_lib.get_or_create_contact(
                s, tenant_id, wa["a"], name="Paciente A")
            await demo_lib.cleanup_demo_appointments(
                s, tenant_id, [ca.id])
            cal = MemoryCalendarAdapter(s, tenant_id, tz=TENANT_TZ)
            r = await _book(s, cal, ca.id, day, "10:00", MG, day, "c1")
            await s.commit()
        results.append((
            "cita-1 medicina general 10:00",
            bool(r["ok"]),
            "agendada (médico general + consultorio 1)"
            if r["ok"] else f"no se pudo agendar: {r.get('error')}",
        ))
    except Exception as e:  # noqa: BLE001
        results.append(("cita-1 medicina general 10:00", False,
                        f"{type(e).__name__}: {e}"))

    print("[3/6] Cita 2: paciente B, mismo servicio, misma hora (traslape)")
    try:
        async with db_mod.async_session_maker() as s:
            cb = await demo_lib.get_or_create_contact(
                s, tenant_id, wa["b"], name="Paciente B")
            await demo_lib.cleanup_demo_appointments(
                s, tenant_id, [cb.id])
            cal = MemoryCalendarAdapter(s, tenant_id, tz=TENANT_TZ)
            r = await _book(s, cal, cb.id, day, "10:00", MG, day, "c2")
            await s.commit()
        if not r["ok"] and r.get("error"):
            results.append((
                "cita-2 traslape rechazado",
                True,
                f"RECHAZADA como se esperaba: {r['error']}",
            ))
        else:
            results.append((
                "cita-2 traslape rechazado",
                False,
                f"se esperaba rechazo y el book devolvió ok={r['ok']}",
            ))
    except Exception as e:  # noqa: BLE001
        results.append(("cita-2 traslape rechazado", False,
                        f"{type(e).__name__}: {e}"))

    print(f"[4/6] Cita 3: paciente B, pediatría, {day} 10:30 (recurso libre)")
    try:
        async with db_mod.async_session_maker() as s:
            cb = await demo_lib.get_or_create_contact(
                s, tenant_id, wa["b"], name="Paciente B")
            cal = MemoryCalendarAdapter(s, tenant_id, tz=TENANT_TZ)
            r = await _book(s, cal, cb.id, day, "10:30", PED, day, "c3")
            await s.commit()
        results.append((
            "cita-3 pediatría 10:30",
            bool(r["ok"]),
            "agendada (pediatra + consultorio libres en ese horario)"
            if r["ok"] else f"no se pudo agendar: {r.get('error')}",
        ))
    except Exception as e:  # noqa: BLE001
        results.append(("cita-3 pediatría 10:30", False,
                        f"{type(e).__name__}: {e}"))

    print(f"[5/6] Buffers: electrocardiograma {day} 11:00 y 11:20")
    try:
        async with db_mod.async_session_maker() as s:
            ca = await demo_lib.get_or_create_contact(
                s, tenant_id, wa["a"], name="Paciente A")
            cc = await demo_lib.get_or_create_contact(
                s, tenant_id, wa["c"], name="Paciente C")
            await demo_lib.cleanup_demo_appointments(
                s, tenant_id, [cc.id])
            cal = MemoryCalendarAdapter(s, tenant_id, tz=TENANT_TZ)
            r1 = await _book(s, cal, ca.id, day, "11:00", ECG, day, "e1")
            r2 = await _book(s, cal, cc.id, day, "11:20", ECG, day, "e2")
            await s.commit()
        ok = bool(r1["ok"]) and not r2["ok"] and bool(r2.get("error"))
        results.append((
            "buffers electrocardiograma",
            ok,
            ("11:00 agendado (ocupa [10:55, 11:25] con setup/teardown); "
             f"11:20 RECHAZADO: {r2.get('error')}")
            if ok else
            f"inesperado: 11:00 ok={r1['ok']} ({r1.get('error')}), "
            f"11:20 ok={r2['ok']} ({r2.get('error')})",
        ))
    except Exception as e:  # noqa: BLE001
        results.append(("buffers electrocardiograma", False,
                        f"{type(e).__name__}: {e}"))

    print("[6/6] Consentimiento: primer contacto antes de agendar")
    try:
        async with db_mod.async_session_maker() as s:
            cn = await demo_lib.get_or_create_contact(
                s, tenant_id, wa["nuevo"], name=None, consent="none")
            await demo_lib.cleanup_demo_appointments(
                s, tenant_id, [cn.id])

            gate1 = await privacy_gate_error(s, tenant_id, cn)
            paso1 = gate1 is not None and "aviso de privacidad" in gate1

            gate2 = await apply_privacy_gate(
                s, tenant_id, cn, "hola, quiero una cita")
            paso2 = (gate2["handled"] is True
                     and cn.consent_status == "pending"
                     and bool(gate2["reply"]))

            gate3 = await apply_privacy_gate(s, tenant_id, cn, "sí, acepto")
            paso3 = (gate3["handled"] is False
                     and cn.consent_status == "granted"
                     and cn.privacy_terms_version == "1.0")

            gate4 = await privacy_gate_error(s, tenant_id, cn)
            cal = MemoryCalendarAdapter(s, tenant_id, tz=TENANT_TZ)
            r = await _book(s, cal, cn.id, day, "12:00", MG, day, "c4")
            paso4 = gate4 is None and bool(r["ok"])
            await s.commit()

        ok = paso1 and paso2 and paso3 and paso4
        results.append((
            "consentimiento primer contacto",
            ok,
            ("sin consent no agenda; el aviso se pide al primer mensaje; "
             "'sí' otorga (v1.0) y la cita de las 12:00 queda agendada")
            if ok else
            f"pasos: puerta_cerrada={paso1} aviso_enviado={paso2} "
            f"otorgado={paso3} agenda_tras_si={paso4}",
        ))
    except Exception as e:  # noqa: BLE001
        results.append(("consentimiento primer contacto", False,
                        f"{type(e).__name__}: {e}"))

    print(f"\nTiempo total: {time.time() - t0:.1f}s")
    return demo_lib.report(results, "consultorio con recursos")


def main() -> int:
    reset, _ = demo_lib.parse_args("Demo: consultorio con recursos.", DB_DEFAULT)
    if reset:
        print(f"ATENCIÓN: --reset borra el esquema de la BD demo "
              f"({DB_URL.rsplit('/', 1)[-1]}).")
    return demo_lib.run(lambda: _run(reset), None)


if __name__ == "__main__":
    sys.exit(main())
