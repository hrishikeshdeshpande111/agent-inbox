"""SQLite persistence layer (aiosqlite, WAL mode, busy timeout).

Single-writer friendly by design: inboxes are independent rows, messages are
append-only, and the cleanup task runs off-peak. For multi-instance scale-out,
point AGENT_INBOX_DB_PATH at a shared volume or migrate the schema to
Postgres — the access functions are the only thing that would change.
"""

import json
import os
from datetime import datetime, timezone

import aiosqlite

from .config import settings
from .security import hash_secret, new_id

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS inboxes (
    id               TEXT PRIMARY KEY,
    read_secret_hash TEXT NOT NULL,
    write_secret_hash TEXT NOT NULL,
    conversation_token_hash TEXT,
    label            TEXT,
    created_at       TEXT NOT NULL,
    last_activity_at TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id              TEXT PRIMARY KEY,
    inbox_id        TEXT NOT NULL REFERENCES inboxes(id) ON DELETE CASCADE,
    received_at     TEXT NOT NULL,
    content_type    TEXT,
    body            TEXT NOT NULL,
    headers         TEXT NOT NULL DEFAULT '{}',
    signature_valid INTEGER
);

CREATE INDEX IF NOT EXISTS idx_messages_inbox_time
    ON messages(inbox_id, received_at DESC, id DESC);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: str | None = None):
        self.path = path or settings.db_path
        self._conn: aiosqlite.Connection | None = None

    async def init(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        await self._conn.executescript(SCHEMA)
        # Lightweight migration for databases created before the
        # conversation_token_hash column existed.
        async with self._conn.execute("PRAGMA table_info(inboxes)") as cur:
            cols = {row[1] for row in await cur.fetchall()}
        if "conversation_token_hash" not in cols:
            await self._conn.execute(
                "ALTER TABLE inboxes ADD COLUMN conversation_token_hash TEXT"
            )
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database.init() was not called"
        return self._conn

    # ---- inboxes ------------------------------------------------------

    async def create_inbox(
        self,
        read_hash: str,
        write_hash: str,
        label: str | None,
        conversation_token_hash: str | None = None,
    ) -> dict:
        inbox_id = new_id()
        now = _now()
        await self.conn.execute(
            "INSERT INTO inboxes (id, read_secret_hash, write_secret_hash,"
            "                     conversation_token_hash, label, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (inbox_id, read_hash, write_hash, conversation_token_hash, label, now),
        )
        await self.conn.commit()
        return {"id": inbox_id, "created_at": now, "label": label}

    async def get_inbox(self, inbox_id: str) -> dict | None:
        async with self.conn.execute(
            "SELECT id, read_secret_hash, write_secret_hash, conversation_token_hash,"
            "       label, created_at, last_activity_at FROM inboxes WHERE id = ?",
            (inbox_id,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "read_secret_hash": row[1],
            "write_secret_hash": row[2],
            "conversation_token_hash": row[3],
            "label": row[4],
            "created_at": row[5],
            "last_activity_at": row[6],
        }

    async def touch_inbox(self, inbox_id: str) -> None:
        await self.conn.execute(
            "UPDATE inboxes SET last_activity_at = ? WHERE id = ?", (_now(), inbox_id)
        )
        await self.conn.commit()

    async def delete_inbox(self, inbox_id: str) -> None:
        await self.conn.execute("DELETE FROM messages WHERE inbox_id = ?", (inbox_id,))
        await self.conn.execute("DELETE FROM inboxes WHERE id = ?", (inbox_id,))
        await self.conn.commit()

    async def rotate_secrets(
        self,
        inbox_id: str,
        read_hash: str,
        write_hash: str,
        conversation_token_hash: str | None = None,
    ) -> None:
        await self.conn.execute(
            "UPDATE inboxes SET read_secret_hash = ?, write_secret_hash = ?,"
            "                 conversation_token_hash = ? WHERE id = ?",
            (read_hash, write_hash, conversation_token_hash, inbox_id),
        )
        await self.conn.commit()

    async def inbox_stats(self, inbox_id: str) -> dict:
        async with self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE inbox_id = ?", (inbox_id,)
        ) as cur:
            row = await cur.fetchone()
        return {"message_count": row[0] if row else 0}

    # ---- messages -----------------------------------------------------

    async def insert_message(
        self,
        inbox_id: str,
        body: str,
        content_type: str | None,
        headers: dict,
        signature_valid: bool | None,
    ) -> dict:
        message_id = new_id()
        now = _now()
        await self.conn.execute(
            "INSERT INTO messages (id, inbox_id, received_at, content_type, body, headers,"
            "                       signature_valid)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                message_id,
                inbox_id,
                now,
                content_type,
                body,
                json.dumps(headers),
                None if signature_valid is None else int(signature_valid),
            ),
        )
        await self.conn.commit()
        return {"id": message_id, "received_at": now}

    async def list_messages(
        self,
        inbox_id: str,
        limit: int,
        before_id: str | None,
        after_id: str | None = None,
    ) -> tuple[list[dict], str | None]:
        """List messages for an inbox.

        Default (and with before_id): newest-first with keyset pagination.
        With after_id: only messages strictly newer than the anchor, returned
        oldest-first (conversation order) for polling consumers. An unknown
        after_id is ignored and the default listing is returned.
        """
        params: list = [inbox_id]
        cursor_clause = ""
        order_clause = "ORDER BY received_at DESC, id DESC"
        if after_id:
            async with self.conn.execute(
                "SELECT received_at, id FROM messages WHERE id = ? AND inbox_id = ?",
                (after_id, inbox_id),
            ) as cur:
                anchor = await cur.fetchone()
            if anchor:
                cursor_clause = "AND (received_at > ? OR (received_at = ? AND id > ?))"
                params += [anchor[0], anchor[0], anchor[1]]
                order_clause = "ORDER BY received_at ASC, id ASC"
        elif before_id:
            async with self.conn.execute(
                "SELECT received_at, id FROM messages WHERE id = ? AND inbox_id = ?",
                (before_id, inbox_id),
            ) as cur:
                anchor = await cur.fetchone()
            if anchor:
                # Keyset pagination: strictly older than the anchor.
                cursor_clause = "AND (received_at < ? OR (received_at = ? AND id < ?))"
                params += [anchor[0], anchor[0], anchor[1]]
        query = (
            "SELECT id, received_at, content_type, body, headers, signature_valid"
            " FROM messages WHERE inbox_id = ? " + cursor_clause + " " + order_clause + " LIMIT ?"
        )
        params.append(limit + 1)  # fetch one extra to know if there is a next page
        async with self.conn.execute(query, params) as cur:
            rows = await cur.fetchall()
        out = []
        for r in rows[:limit]:
            out.append(
                {
                    "id": r[0],
                    "received_at": r[1],
                    "content_type": r[2],
                    "body": r[3],
                    "headers": json.loads(r[4] or "{}"),
                    "signature_valid": None if r[5] is None else bool(r[5]),
                }
            )
        next_before = rows[limit - 1][0] if len(rows) > limit and not after_id else None
        return out, next_before

    async def delete_message(self, inbox_id: str, message_id: str) -> bool:
        async with self.conn.execute(
            "DELETE FROM messages WHERE id = ? AND inbox_id = ?", (message_id, inbox_id)
        ) as cur:
            deleted = cur.rowcount or 0
        await self.conn.commit()
        return deleted > 0

    async def purge_expired(self, retention_days: int) -> int:
        """Delete messages older than the retention window. Returns count."""
        async with self.conn.execute(
            "DELETE FROM messages WHERE received_at < "
            "datetime('now', ?)",
            (f"-{int(retention_days)} days",),
        ) as cur:
            purged = cur.rowcount or 0
        await self.conn.commit()
        return purged


db = Database()
