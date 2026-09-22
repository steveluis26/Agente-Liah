"""Worker de fondo Liah (Fase 5).

Entrypoint: `python -m app.worker`

Un solo proceso que, en loop asyncio:
  (a) drena `webhook_jobs` pendientes (reusa el drenador de Fase 1:
      `app/channels/whatsapp/queue.py::drain_jobs`),
  (b) ejecuta el scheduler de recordatorios (reusa
      `app/reminders/scheduler.py::run_once`), y
  (c) despacha campañas/aviso de marketing (Fase 6:
      `app/marketing/campaigns.py::dispatch_campaigns`, con pacing
      anti-baneo).

En producción este worker corre SEPARADO de la API. En dev, `app/main.py`
sigue arrancando el scheduler in-process en el lifespan (ver su docstring):
útil para desarrollo, pero si API y worker corren a la vez, los
recordatorios se evaluarían dos veces (la idempotencia por `reminder_log`
evita duplicados, pero es desperdicio).

Intervalos por env:
  WORKER_DRAIN_INTERVAL_S     (default 10): cada cuánto drena la cola.
  WORKER_REMINDER_INTERVAL_S  (default 3600): cada cuánto corre recordatorios.
  WORKER_DRAIN_LIMIT          (default 50): tope de jobs por drenado.
  LIAH_SEND_DRY_RUN           (default "1"): "1" = no llama a Meta (dev).

Apagado limpio: SIGTERM/SIGINT detienen el loop al terminar el ciclo en
curso (nunca a mitad de un job: cada job corre en su propia transacción).
"""
import asyncio
import logging
import os
import signal
import time

logger = logging.getLogger("liah.worker")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        logger.warning("Env %s inválido; uso default %d", name, default)
        return default


async def run_cycle(
    session_maker,
    *,
    drain_limit: int = 50,
    llm_factory=None,
    dry_run: bool | None = None,
    run_reminders: bool = True,
    reminder_dry_run: bool = False,
    run_campaigns: bool = True,
) -> dict:
    """UN ciclo del worker: drena la cola, corre recordatorios y campañas.

    Diseñada para ser testeable sin loop infinito: los tests llaman a esta
    función directamente. Devuelve estadísticas del drenado, de campañas y
    cualquier error del scheduler (que nunca debe tumbar el ciclo).
    """
    from app.channels.whatsapp.queue import _dry_run_default, drain_jobs
    from app.marketing.campaigns import dispatch_campaigns
    from app.reminders.scheduler import run_once

    stats = await drain_jobs(
        session_maker,
        limit=drain_limit,
        llm_factory=llm_factory,
        dry_run=dry_run,
    )
    reminder_error = None
    if run_reminders:
        try:
            await run_once(dry_run=reminder_dry_run)
        except Exception as e:  # noqa: BLE001 - el worker no debe morir
            reminder_error = f"{type(e).__name__}: {e}"
            logger.exception("Error en ciclo de recordatorios")
    campaign_stats: dict | None = None
    campaign_error = None
    if run_campaigns:
        try:
            campaign_stats = await dispatch_campaigns(
                session_maker,
                dry_run=bool(dry_run) if dry_run is not None else _dry_run_default(),
            )
        except Exception as e:  # noqa: BLE001 - el worker no debe morir
            campaign_error = f"{type(e).__name__}: {e}"
            logger.exception("Error en dispatch de campañas")
    return {
        "drain": stats,
        "reminder_error": reminder_error,
        "campaigns": campaign_stats,
        "campaign_error": campaign_error,
    }


async def amain() -> None:
    """Loop principal del worker. Termina limpio con SIGTERM/SIGINT."""
    from app.core import db as db_mod

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    drain_interval = _env_int("WORKER_DRAIN_INTERVAL_S", 10)
    reminder_interval = _env_int("WORKER_REMINDER_INTERVAL_S", 3600)
    drain_limit = _env_int("WORKER_DRAIN_LIMIT", 50)
    # Envíos: None = respeta LIAH_SEND_DRY_RUN (default "1": dev seguro).
    dry_run = None if os.getenv("LIAH_SEND_DRY_RUN", "1") == "1" else False
    reminder_dry_run = dry_run is None

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows / entornos sin signal handlers en asyncio.
            pass

    logger.info(
        "Worker Liah iniciado (drain cada %ds, recordatorios cada %ds, "
        "dry_run=%s). SIGTERM para detener.",
        drain_interval,
        reminder_interval,
        dry_run if dry_run is not None else True,
    )
    last_reminder = 0.0
    cycle = 0
    while not stop.is_set():
        cycle += 1
        do_reminders = (time.monotonic() - last_reminder) >= reminder_interval
        try:
            result = await run_cycle(
                db_mod.async_session_maker,
                drain_limit=drain_limit,
                dry_run=dry_run,
                run_reminders=do_reminders,
                reminder_dry_run=reminder_dry_run,
            )
        except Exception:  # noqa: BLE001 - el loop no debe morir
            logger.exception("Error inesperado en ciclo %d del worker", cycle)
        else:
            d = result["drain"]
            if d["processed"]:
                logger.info(
                    "Ciclo %d: jobs procesados=%d done=%d failed=%d skipped=%d "
                    "reminder_error=%s",
                    cycle,
                    d["processed"],
                    d["done"],
                    d["failed"],
                    d["skipped"],
                    result["reminder_error"],
                )
            if do_reminders:
                last_reminder = time.monotonic()
        try:
            await asyncio.wait_for(stop.wait(), timeout=drain_interval)
        except asyncio.TimeoutError:
            pass
    logger.info("Worker Liah detenido limpio (señal recibida).")


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
