"""Hintergrund-Takt für fällige Aufträge."""

from __future__ import annotations

import logging
import threading
import time

from app.runner import Runner

log = logging.getLogger("app.scheduler")


def start_scheduler(runner: Runner, interval: float = 15.0) -> None:
    def loop() -> None:
        while True:
            try:
                runner.schedule_tick()
            except Exception:
                log.exception("Zeitplan")
            time.sleep(interval)

    threading.Thread(target=loop, name="scheduler", daemon=True).start()
