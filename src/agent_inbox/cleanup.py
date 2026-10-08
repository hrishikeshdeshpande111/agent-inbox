"""Background retention enforcement.

Runs forever on the configured interval and purges messages older than
AGENT_INBOX_RETENTION_DAYS. Inboxes themselves are never auto-deleted —
an empty inbox costs nothing and its URL may be printed in someone's docs.
"""

import asyncio
import logging

from .config import settings
from .db import db

log = logging.getLogger("agent-inbox.cleanup")


async def purge_once() -> int:
    purged = await db.purge_expired(settings.retention_days)
    if purged:
        log.info("retention purge removed %d expired message(s)", purged)
    return purged


async def loop() -> None:
    while True:
        try:
            await purge_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("retention purge failed; will retry next interval")
        await asyncio.sleep(settings.cleanup_interval_sec)
