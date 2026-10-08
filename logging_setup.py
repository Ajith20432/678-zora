"""
logging_setup.py — one place that configures logging for the whole bot.

Writes to both the console (so Termux still shows live status the way it
always did) and a rotating log file (so you have a real record to scroll
back through after the phone's terminal buffer is long gone). The file
handler is size-capped and rotates, so it won't slowly eat phone storage
the way an ever-growing log file would.

Call configure_logging() once, at the very start of bot.py's main(). Every
other module just does `logger = logging.getLogger(__name__)` and uses it
normally — no other module needs to know about handlers or file paths.
"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

LOG_FILE = os.getenv("LOG_FILE", "zora.log")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
MAX_BYTES = 2 * 1024 * 1024   # 2 MB per file
BACKUP_COUNT = 3              # keep 3 rotated files -> ~8 MB ceiling total

_configured = False


def configure_logging() -> None:
    global _configured
    if _configured:
        return

    root = logging.getLogger()
    root.setLevel(LOG_LEVEL)

    fmt = logging.Formatter(
        fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    try:
        file_handler = RotatingFileHandler(LOG_FILE, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT)
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError:
        # e.g. read-only filesystem in some sandboxed environment — console logging still works
        root.warning("Could not open log file %s; continuing with console logging only.", LOG_FILE)

    _configured = True
