"""Clock that drives the WaitlistAgent's proactive behaviour (no user action needed)."""
import asyncio
import logging

from .config import settings
from .database import SessionLocal

log = logging.getLogger("eventsphere.scheduler")


def sweep_once(bus, now=None) -> dict:
    with SessionLocal() as db:
        try:
            result = bus.agent("WaitlistAgent").on_tick(db, now=now)
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise


async def run_scheduler(bus) -> None:
    while True:
        await asyncio.sleep(settings.sweep_interval_seconds)
        try:
            result = await asyncio.to_thread(sweep_once, bus)
            if result.get("expired"):
                log.info("Waitlist sweep: %s", result)
        except Exception:  # keep the timer alive
            log.exception("Waitlist sweep failed")
