"""MCP tool tests. Run with:  pytest  (from the repo root, venv active)."""

import os
import sys
import urllib.parse

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

os.environ["AGENT_INBOX_DB_PATH"] = "/tmp/agent-inbox-test.db"
os.environ["AGENT_INBOX_RATE_LIMIT_PER_MIN"] = "1000"
os.environ["AGENT_INBOX_BASE_URL"] = "http://testserver"

from agent_inbox import mcp_tools as mt  # noqa: E402
from agent_inbox.main import app  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
async def _database():
    from agent_inbox.db import db as _db

    await _db.init()
    yield
    await _db.close()


@pytest.fixture()
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


async def _inbox(label="mcp-test"):
    return await mt.create_inbox(label)


async def test_create_inbox_returns_one_time_secrets():
    data = await _inbox()
    assert data["id"] and data["url"].endswith(f"/v1/inboxes/{data['id']}")
    assert len(data["read_secret"]) > 20 and len(data["write_secret"]) > 20
    assert data["read_secret"] != data["write_secret"]


async def test_full_message_flow():
    box = await _inbox()
    iid, rs, ws = box["id"], box["read_secret"], box["write_secret"]

    sent = await mt.send_message(iid, ws, "hello from mcp")
    assert sent["message_id"]

    seen = await mt.check_messages(iid, rs)
    assert seen["count"] == 1
    assert seen["messages"][0]["body"] == "hello from mcp"
    mid = seen["messages"][0]["id"]

    # after_id polling: nothing newer yet
    seen2 = await mt.check_messages(iid, rs, after_id=mid)
    assert seen2["count"] == 0

    ack = await mt.acknowledge_message(iid, rs, mid)
    assert ack["ok"] is True

    seen3 = await mt.check_messages(iid, rs)
    assert seen3["count"] == 0


async def test_get_inbox_counts_messages():
    box = await _inbox()
    iid, rs, ws = box["id"], box["read_secret"], box["write_secret"]
    info = await mt.get_inbox(iid, rs)
    assert info["message_count"] == 0
    await mt.send_message(iid, ws, "one")
    await mt.send_message(iid, ws, "two")
    info = await mt.get_inbox(iid, rs)
    assert info["message_count"] == 2


async def test_rotate_invalidates_old_secrets():
    box = await _inbox()
    iid, rs, ws = box["id"], box["read_secret"], box["write_secret"]
    rotated = await mt.rotate_secrets(iid, rs)
    assert rotated["read_secret"] != rs and rotated["write_secret"] != ws
    # old secrets dead
    with pytest.raises(mt.InboxError):
        await mt.check_messages(iid, rs)
    with pytest.raises(mt.InboxError):
        await mt.send_message(iid, ws, "x")
    # new secrets work
    seen = await mt.check_messages(iid, rotated["read_secret"])
    assert seen["count"] == 0


async def test_wrong_secrets_rejected():
    box = await _inbox()
    iid = box["id"]
    with pytest.raises(mt.InboxError):
        await mt.send_message(iid, "nope", "x")
    with pytest.raises(mt.InboxError):
        await mt.check_messages(iid, "nope")
    with pytest.raises(mt.InboxError):
        await mt.get_inbox(iid, "nope")
    with pytest.raises(mt.InboxError):
        await mt.rotate_secrets(iid, "nope")
    with pytest.raises(mt.InboxError):
        await mt.send_message("does-not-exist", "nope", "x")


async def test_conversation_token_works_as_mcp_credential(client):
    """The invite-link token is a complete onboarding credential: it sends
    and reads via MCP, but cannot ack or rotate (existing token policy)."""
    r = await client.post("/v1/inboxes", json={"label": "token-mcp"})
    assert r.status_code == 201, r.text
    box = r.json()
    iid = box["id"]
    tok = urllib.parse.parse_qs(urllib.parse.urlparse(box["llm_txt_url"]).query)[
        "token"
    ][0]

    sent = await mt.send_message(iid, tok, "hello via invite token")
    assert sent["message_id"]
    seen = await mt.check_messages(iid, tok)
    assert seen["count"] == 1
    assert seen["messages"][0]["body"] == "hello via invite token"
    info = await mt.get_inbox(iid, tok)
    assert info["id"] == iid

    mid = seen["messages"][0]["id"]
    with pytest.raises(mt.InboxError):
        await mt.acknowledge_message(iid, tok, mid)
    with pytest.raises(mt.InboxError):
        await mt.rotate_secrets(iid, tok)


async def test_send_message_wait_timeout():
    box = await _inbox()
    iid, ws = box["id"], box["write_secret"]
    # Nobody will reply: short wait must return a timeout, not hang.
    res = await mt.send_message(iid, ws, "knock knock", wait_seconds=2)
    assert res["status"] == "timeout"
    assert res["message_id"]


async def test_mcp_endpoint_mounted(client):
    # The Streamable HTTP endpoint must exist (405/400 on bare GET is fine;
    # 404 would mean the mount failed).
    r = await client.get("/mcp")
    assert r.status_code != 404, r.text


async def test_tool_descriptions_present():
    tools = await mt.mcp.list_tools()
    names = sorted(t.name for t in tools)
    assert names == [
        "acknowledge_message",
        "check_messages",
        "create_inbox",
        "create_network",
        "get_inbox",
        "join_network",
        "rotate_secrets",
        "send_message",
    ]
    for t in tools:
        assert t.description and len(t.description) > 20, t.name
