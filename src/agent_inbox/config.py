"""Runtime configuration. Everything is overridable via environment variables."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # Filesystem path for the SQLite database. Parent dirs are created on boot.
    db_path: str = os.environ.get("AGENT_INBOX_DB_PATH", "data/agent-inbox.db")
    # Public base URL used when constructing inbox URLs handed to users.
    # Set this to your real domain in production, e.g. https://inbox.example.com
    base_url: str = os.environ.get("AGENT_INBOX_BASE_URL", "http://localhost:8000").rstrip("/")
    # Messages older than this are purged by the background cleanup task.
    retention_days: int = int(os.environ.get("AGENT_INBOX_RETENTION_DAYS", "7"))
    # Max requests per minute, enforced per (client IP) and per (inbox, IP).
    rate_limit_per_min: int = int(os.environ.get("AGENT_INBOX_RATE_LIMIT_PER_MIN", "60"))
    # Hard cap on a single delivered payload.
    max_body_bytes: int = int(os.environ.get("AGENT_INBOX_MAX_BODY_BYTES", str(256 * 1024)))
    # How often the cleanup task runs.
    cleanup_interval_sec: int = int(os.environ.get("AGENT_INBOX_CLEANUP_INTERVAL_SEC", "3600"))
    # Max length of the optional human-readable inbox label.
    max_label_len: int = 120


settings = Settings()
