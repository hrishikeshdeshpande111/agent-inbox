"""End-to-end API tests. Run with:  pytest  (from the repo root, venv active)."""

import hashlib
import hmac
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Test-local settings: isolated DB, tight rate limit so the limiter is exercised.
os.environ["AGENT_INBOX_DB_PATH"] = "/tmp/agent-inbox-test.db"
os.environ["AGENT_INBOX_RATE_LIMIT_PER_MIN"] = "1000"
os.environ["AGENT_INBOX_BASE_URL"] = "http://testserver"

if os.path.exists("/tmp/agent-inbox-test.db"):
    os.remove("/tmp/agent-inbox-test.db")

from agent_inbox import __version__  # noqa: E402
from agent_inbox.config import settings  # noqa: E402
from agent_inbox.main import app  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
async def _database():
    # ASGITransport does not run the app lifespan, so init the DB explicitly.
    from agent_inbox.db import db as _db

    await _db.init()
    yield
    await _db.close()


@pytest.fixture()
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


async def _create(client, label="test") -> dict:
    r = await client.post("/v1/inboxes", json={"label": label})
    assert r.status_code == 201, r.text
    return r.json()


async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["version"] == __version__


async def test_create_returns_one_time_secrets(client):
    data = await _create(client)
    assert data["url"].startswith("http://testserver/v1/inboxes/")
    assert len(data["read_secret"]) > 20 and len(data["write_secret"]) > 20
    assert data["read_secret"] != data["write_secret"]


async def test_deliver_and_read_roundtrip(client):
    inbox = await _create(client)
    iid = inbox["id"]
    r = await client.post(
        f"/v1/inboxes/{iid}",
        json={"event": "ping", "n": 1},
        headers={"X-Write-Secret": inbox["write_secret"]},
    )
    assert r.status_code == 202, r.text
    assert r.json()["signature_valid"] is None

    r = await client.get(
        f"/v1/inboxes/{iid}/messages", headers={"X-Read-Secret": inbox["read_secret"]}
    )
    assert r.status_code == 200
    msgs = r.json()["messages"]
    assert len(msgs) == 1
    assert '"event": "ping"' in msgs[0]["body"] or '"event":"ping"' in msgs[0]["body"]
    assert msgs[0]["headers"].get("content-type", "").startswith("application/json")


async def test_deliver_via_query_param_secret(client):
    # Dumb webhook sources can't set headers — the query param must work.
    inbox = await _create(client)
    r = await client.post(
        f"/v1/inboxes/{inbox['id']}?write_secret={inbox['write_secret']}",
        content="hello from a form",
        headers={"content-type": "text/plain"},
    )
    assert r.status_code == 202, r.text


async def test_wrong_secrets_rejected(client):
    inbox = await _create(client)
    iid = inbox["id"]
    r = await client.post(f"/v1/inboxes/{iid}", json={}, headers={"X-Write-Secret": "nope"})
    assert r.status_code == 401
    r = await client.get(f"/v1/inboxes/{iid}/messages", headers={"X-Read-Secret": "nope"})
    assert r.status_code == 401
    # read secret must not authorize writes and vice versa
    r = await client.post(
        f"/v1/inboxes/{iid}", json={}, headers={"X-Write-Secret": inbox["read_secret"]}
    )
    assert r.status_code == 401


async def test_unknown_inbox_404(client):
    r = await client.get("/v1/inboxes/doesnotexist/messages", headers={"X-Read-Secret": "x"})
    assert r.status_code == 404


async def test_hmac_signature_verified(client):
    inbox = await _create(client)
    body = b'{"signed": true}'
    sig = "sha256=" + hmac.new(inbox["write_secret"].encode(), body, hashlib.sha256).hexdigest()
    r = await client.post(
        f"/v1/inboxes/{inbox['id']}",
        content=body,
        headers={"X-Write-Secret": inbox["write_secret"], "X-Signature-256": sig},
    )
    assert r.status_code == 202
    assert r.json()["signature_valid"] is True

    # Tampered signature is rejected outright.
    r = await client.post(
        f"/v1/inboxes/{inbox['id']}",
        content=body,
        headers={"X-Write-Secret": inbox["write_secret"], "X-Signature-256": "sha256=deadbeef"},
    )
    assert r.status_code == 401


async def test_sensitive_headers_redacted(client):
    inbox = await _create(client)
    await client.post(
        f"/v1/inboxes/{inbox['id']}",
        json={"a": 1},
        headers={"X-Write-Secret": inbox["write_secret"], "Authorization": "Bearer hunter2"},
    )
    r = await client.get(
        f"/v1/inboxes/{inbox['id']}/messages", headers={"X-Read-Secret": inbox["read_secret"]}
    )
    headers = r.json()["messages"][0]["headers"]
    assert headers.get("authorization") == "[redacted]"


async def test_pagination_and_ack(client):
    inbox = await _create(client)
    iid = inbox["id"]
    for i in range(3):
        await client.post(
            f"/v1/inboxes/{iid}", json={"i": i}, headers={"X-Write-Secret": inbox["write_secret"]}
        )
    h = {"X-Read-Secret": inbox["read_secret"]}
    r = await client.get(f"/v1/inboxes/{iid}/messages?limit=2", headers=h)
    page1 = r.json()
    assert len(page1["messages"]) == 2 and page1["next_before_id"]

    r = await client.get(f"/v1/inboxes/{iid}/messages?limit=2&before_id={page1['next_before_id']}", headers=h)
    page2 = r.json()
    assert len(page2["messages"]) == 1 and page2["next_before_id"] is None

    # Ack the newest message; it must disappear.
    newest = page1["messages"][0]["id"]
    r = await client.delete(f"/v1/inboxes/{iid}/messages/{newest}", headers=h)
    assert r.status_code == 200
    r = await client.get(f"/v1/inboxes/{iid}/messages?limit=10", headers=h)
    assert all(m["id"] != newest for m in r.json()["messages"])


async def test_rotate_and_delete(client):
    inbox = await _create(client)
    iid = inbox["id"]
    r = await client.post(f"/v1/inboxes/{iid}/rotate", headers={"X-Read-Secret": inbox["read_secret"]})
    assert r.status_code == 200
    new = r.json()
    # Old secrets are dead.
    r = await client.get(f"/v1/inboxes/{iid}/messages", headers={"X-Read-Secret": inbox["read_secret"]})
    assert r.status_code == 401
    # New read secret works.
    r = await client.get(f"/v1/inboxes/{iid}/messages", headers={"X-Read-Secret": new["read_secret"]})
    assert r.status_code == 200

    r = await client.delete(f"/v1/inboxes/{iid}", headers={"X-Read-Secret": new["read_secret"]})
    assert r.status_code == 200
    r = await client.get(f"/v1/inboxes/{iid}", headers={"X-Read-Secret": new["read_secret"]})
    assert r.status_code == 404


async def test_oversized_payload_rejected(client):
    from agent_inbox.config import settings as s

    inbox = await _create(client)
    big = "x" * (s.max_body_bytes + 1)
    r = await client.post(
        f"/v1/inboxes/{inbox['id']}",
        content=big,
        headers={"X-Write-Secret": inbox["write_secret"], "content-type": "text/plain"},
    )
    assert r.status_code == 413


async def test_rate_limit_enforced(client):
    from agent_inbox.main import _ip_limiter

    _ip_limiter.per_minute = 2
    _ip_limiter._hits.clear()
    try:
        # /health is intentionally exempt from rate limiting (load-balancer
        # probes), so exercise the limiter against a real guarded endpoint.
        for _ in range(2):
            r = await client.post("/v1/inboxes", json={})
            assert r.status_code == 201, r.text
        r = await client.post("/v1/inboxes", json={})
        assert r.status_code == 429
        assert "Retry-After" in r.headers
    finally:
        _ip_limiter.per_minute = 1000
        _ip_limiter._hits.clear()


async def test_retention_purge():
    from agent_inbox import cleanup
    from agent_inbox.db import db

    old_hash = "0" * 64
    row = await db.create_inbox(old_hash, old_hash, "purge-test")
    iid = row["id"]
    # Insert a message, then backdate it past retention.
    msg = await db.insert_message(iid, "old", "text/plain", {}, None)
    await db.conn.execute(
        "UPDATE messages SET received_at = datetime('now', '-30 days') WHERE id = ?",
        (msg["id"],),
    )
    await db.conn.commit()
    purged = await cleanup.purge_once()
    assert purged >= 1
    stats = await db.inbox_stats(iid)
    assert stats["message_count"] == 0
    await db.delete_inbox(iid)
