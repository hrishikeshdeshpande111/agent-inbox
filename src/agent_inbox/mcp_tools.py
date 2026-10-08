"""Native MCP server for Agent Inbox.

Exposes the inbox primitives as MCP tools so MCP-capable clients
(ChatGPT plugins, Claude, agent frameworks) get first-class tools instead
of hand-rolled HTTP. Mounted at /mcp (Streamable HTTP) by main.py.

This module is deliberately standalone (imports config/db/security only,
never main) so the tool functions stay importable for tests.

Authentication: every tool except create_inbox takes the inbox's
read_secret or write_secret as an explicit parameter — the MCP client
holds the credentials it received at creation time, exactly like the
REST API's header auth. Additionally, the scoped conversation token from
an inbox's invite link (llm.txt URL) is accepted wherever a read or
write credential is needed: it grants send + read on that inbox only
(never acknowledge, rotate, or delete). This makes one invite link the
complete onboarding credential for the other agent.
"""

import asyncio
import time
import urllib.parse

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from .config import settings
from .db import db
from .rate_limit import RateLimiter
from .security import hash_secret, new_secret, secrets_match

mcp = MCPServer("agent-inbox")

# Abuse brake for the MCP surface. REST does per-IP + per-inbox budgets;
# MCP calls carry no client IP at the tool layer, so we budget per inbox
# (and per tool for the unauthenticated create_inbox).
_mcp_limiter = RateLimiter(settings.rate_limit_per_min)


class InboxError(Exception):
    """A clean, client-presentable tool failure (bad id, bad secret, ...)."""


def _check_rate(key: str) -> None:
    if not _mcp_limiter.allowed(f"mcp:{key}"):
        raise InboxError("rate limit exceeded, try again in a minute")


def transport_security() -> TransportSecuritySettings:
    """DNS-rebinding protection allow-list: the configured public host plus
    loopback for local dev. The test suite sets AGENT_INBOX_BASE_URL to
    http://testserver, which lands here automatically."""
    hosts = {"localhost", "127.0.0.1"}
    try:
        host = urllib.parse.urlsplit(settings.base_url).hostname
        if host:
            hosts.add(host)
    except Exception:
        pass
    return TransportSecuritySettings(allowed_hosts=sorted(hosts))


async def _require_inbox(inbox_id: str) -> dict:
    inbox = await db.get_inbox(inbox_id)
    if inbox is None:
        raise InboxError("inbox not found")
    return inbox


def _check_write_secret(provided: str, inbox: dict) -> None:
    if not secrets_match(provided, inbox["write_secret_hash"]):
        raise InboxError("invalid write secret")


def _check_read_secret(provided: str, inbox: dict) -> None:
    if not secrets_match(provided, inbox["read_secret_hash"]):
        raise InboxError("invalid read secret")


def _check_conversation_token(provided: str, inbox: dict) -> bool:
    tok_hash = inbox.get("conversation_token_hash") or ""
    return bool(tok_hash) and secrets_match(provided, tok_hash)


def _check_send_credential(provided: str, inbox: dict) -> None:
    """Accept the write secret or the invite-link conversation token."""
    if secrets_match(provided, inbox["write_secret_hash"]):
        return
    if _check_conversation_token(provided, inbox):
        return
    raise InboxError("invalid write secret")


def _check_read_credential(provided: str, inbox: dict) -> None:
    """Accept the read secret or the invite-link conversation token."""
    if secrets_match(provided, inbox["read_secret_hash"]):
        return
    if _check_conversation_token(provided, inbox):
        return
    raise InboxError("invalid read secret")


async def _wait_for_replies(
    inbox_id: str, after_message_id: str, wait_seconds: int
) -> list[dict]:
    """Long-poll for messages newer than after_message_id (cap 50s)."""
    deadline = time.monotonic() + min(wait_seconds, 50)
    while time.monotonic() < deadline:
        await asyncio.sleep(1)
        msgs, _ = await db.list_messages(inbox_id, 20, None, after_id=after_message_id)
        if msgs:
            return msgs
    return []


@mcp.tool()
async def create_inbox(label: str = "") -> dict:
    """Create a new Agent Inbox — a mailbox that any webhook or agent can
    deliver messages to. Returns the inbox id, its URL, and the one-time
    read/write secrets (shown once; store them, they are never shown again).
    Keep the secrets: every other tool needs them."""
    _check_rate("create_inbox")
    read_secret, write_secret, conversation_token = new_secret(), new_secret(), new_secret()
    row = await db.create_inbox(
        hash_secret(read_secret),
        hash_secret(write_secret),
        label or None,
        hash_secret(conversation_token),
    )
    base = settings.base_url
    return {
        "id": row["id"],
        "url": f"{base}/v1/inboxes/{row['id']}",
        "read_secret": read_secret,
        "write_secret": write_secret,
        "created_at": row["created_at"],
    }


@mcp.tool()
async def send_message(
    inbox_id: str, write_secret: str, body: str, wait_seconds: int = 0
) -> dict:
    """Deliver a message to an agent's inbox. The agent on the other side
    reads it with check_messages. The write_secret may be the inbox's
    write secret or the conversation token from its invite link. Set
    wait_seconds (max 50) to hold the call open for a reply — useful for
    a single "ask and get the answer" turn:
    a reply arrives as {"status": "replied", "replies": [...]}, otherwise
    {"status": "timeout"} and you can check_messages later with after_id
    set to the returned message_id."""
    _check_rate(f"send:{inbox_id}")
    inbox = await _require_inbox(inbox_id)
    _check_send_credential(write_secret, inbox)
    if len(body.encode("utf-8")) > settings.max_body_bytes:
        raise InboxError(f"message too large (max {settings.max_body_bytes} bytes)")
    row = await db.insert_message(
        inbox_id=inbox_id,
        body=body,
        content_type="text/plain",
        headers={"source": "mcp"},
        signature_valid=None,
    )
    await db.touch_inbox(inbox_id)
    if wait_seconds:
        replies = await _wait_for_replies(inbox_id, row["id"], wait_seconds)
        if replies:
            return {
                "status": "replied",
                "message_id": row["id"],
                "replies": [
                    {"id": m["id"], "body": m["body"], "received_at": m["received_at"]}
                    for m in replies
                ],
            }
        return {
            "status": "timeout",
            "message_id": row["id"],
            "received_at": row["received_at"],
            "hint": "No reply yet. Call check_messages with after_id set to message_id.",
        }
    return {"message_id": row["id"], "received_at": row["received_at"]}


@mcp.tool()
async def check_messages(
    inbox_id: str,
    read_secret: str,
    after_id: str | None = None,
    limit: int = 50,
) -> dict:
    """Read messages from an inbox. The read_secret may be the inbox's
    read secret or the conversation token from its invite link.
    Pass after_id (a message id you have already seen) to get only newer
    messages, oldest-first — the polling primitive for following a
    conversation. Omit after_id for the newest messages."""
    _check_rate(f"check:{inbox_id}")
    inbox = await _require_inbox(inbox_id)
    _check_read_credential(read_secret, inbox)
    limit = max(1, min(limit, 200))
    messages, _ = await db.list_messages(inbox_id, limit, None, after_id)
    return {
        "messages": [
            {
                "id": m["id"],
                "body": m["body"],
                "received_at": m["received_at"],
                "content_type": m.get("content_type"),
            }
            for m in messages
        ],
        "count": len(messages),
    }


@mcp.tool()
async def acknowledge_message(
    inbox_id: str, read_secret: str, message_id: str
) -> dict:
    """Acknowledge (delete) a message after handling it, so it is not
    returned by check_messages again."""
    _check_rate(f"ack:{inbox_id}")
    inbox = await _require_inbox(inbox_id)
    _check_read_secret(read_secret, inbox)
    if not await db.delete_message(inbox_id, message_id):
        raise InboxError("message not found")
    return {"ok": True}


@mcp.tool()
async def get_inbox(inbox_id: str, read_secret: str) -> dict:
    """Get an inbox's status: label, creation time, last activity, and how
    many unacknowledged messages it holds."""
    _check_rate(f"info:{inbox_id}")
    inbox = await _require_inbox(inbox_id)
    _check_read_credential(read_secret, inbox)
    stats = await db.inbox_stats(inbox_id)
    return {
        "id": inbox["id"],
        "label": inbox["label"],
        "url": f"{settings.base_url}/v1/inboxes/{inbox_id}",
        "created_at": inbox["created_at"],
        "last_activity_at": inbox["last_activity_at"],
        "message_count": stats["message_count"],
    }


@mcp.tool()
async def rotate_secrets(inbox_id: str, read_secret: str) -> dict:
    """Rotate an inbox's read and write secrets. Old secrets stop working
    immediately — store the new ones. Use when a secret may have leaked."""
    _check_rate(f"rotate:{inbox_id}")
    inbox = await _require_inbox(inbox_id)
    _check_read_secret(read_secret, inbox)
    new_read, new_write, new_token = new_secret(), new_secret(), new_secret()
    await db.rotate_secrets(
        inbox_id,
        hash_secret(new_read),
        hash_secret(new_write),
        hash_secret(new_token),
    )
    return {"id": inbox_id, "read_secret": new_read, "write_secret": new_write}
