"""Логирование + кольцевой буфер последних ошибок (виден в админке)."""
from __future__ import annotations

import logging
import sys
from collections import deque
from datetime import datetime, timezone

LOG_BUFFER: deque[str] = deque(maxlen=200)


class RingBufferHandler(logging.Handler):
    """Хранит последние WARNING+ записи в памяти."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            ts = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%d.%m %H:%M:%S")
            line = f"{ts} {record.levelname} {record.name}: {record.getMessage()}"
            if record.exc_info:
                exc = record.exc_info[1]
                line += f" | {type(exc).__name__}: {exc}"
            LOG_BUFFER.append(line[:600])
        except Exception:  # noqa: BLE001
            pass


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level.upper())

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
    root.addHandler(stream)

    ring = RingBufferHandler(level=logging.WARNING)
    root.addHandler(ring)

    # шумные библиотеки
    for noisy in ("httpx", "httpcore", "aiohttp.access", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
