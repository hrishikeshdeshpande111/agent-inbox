"""Tests for private agent networks: POST /v1/networks mints a key, and
join_network(key) lands every holder in the same isolated channel.
Run with: pytest."""

import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

os.environ["AGENT_INBOX_DB_PATH"] = "/tmp/agent-inbox-test-networks.db"
os.environ["AGENT_INBOX_RATE_LIMIT_PER_MIN"] = "1000"
os.environ["AGENT_INBOX_BASE_URL"] = "http://testserver"

if os.path.exists("/tmp/agent-inbox-test-networks.db"):
    os.remove("/tmp/agent-inbox-test-networks.db")

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


async def test_mint_returns_key_and_prompt(client):
    r = await client.post("/v1/networks", json={"label": "joe"})
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["key"].startswith("aib_")
    assert len(data["key"]) > 20
    assert data["key"] in data["prompt"]
    assert "/mcp" in data["prompt"]
    assert "join_network" in data["prompt"]


async def test_two_mints_are_isolated(client):
    a = (await client.post("/v1/networks", json={})).json()
    b = (await client.post("/v1/networks", json={})).json()
    assert a["key"] != b["key"]
    ja = await mt.join_network(a["key"], "ChatGPT")
    jb = await mt.join_network(b["key"], "Muse")
    assert ja["inbox_id"] != jb["inbox_id"]
    # Joe's agent sends; Maya's key must not read Joe's channel.
    await mt.send_message(ja["inbox_id"], a["key"], "hello from joe", sender="ChatGPT")
    with pytest.raises(mt.InboxError):
        await mt.check_messages(ja["inbox_id"], b["key"])
    # ...and Maya's own channel is empty.
    seen = await mt.check_messages(jb["inbox_id"], b["key"])
    assert seen["count"] == 0
    # Joe's channel has the message, with sender attribution.
    seen_a = await mt.check_messages(ja["inbox_id"], a["key"])
    assert seen_a["count"] == 1
    assert seen_a["messages"][0]["sender"] == "ChatGPT"


async def test_join_is_idempotent(client):
    a = (await client.post("/v1/networks", json={})).json()
    j1 = await mt.join_network(a["key"], "ChatGPT")
    j2 = await mt.join_network(a["key"], "Muse")
    j3 = await mt.join_network(a["key"], "Claude")
    assert j1["inbox_id"] == j2["inbox_id"] == j3["inbox_id"]
    # all three see each other's messages
    await mt.send_message(j1["inbox_id"], a["key"], "hi all", sender="ChatGPT")
    await mt.send_message(j1["inbox_id"], a["key"], "hey", sender="Claude")
    seen = await mt.check_messages(j1["inbox_id"], a["key"])
    senders = {m["sender"] for m in seen["messages"]}
    assert senders == {"ChatGPT", "Claude"}


async def test_unknown_key_fails_closed(client):
    with pytest.raises(mt.InboxError):
        await mt.join_network("aib_nope_not_a_real_key_12345")
    r = await client.get("/v1/networks/aib_nope_not_a_real_key_12345/join")
    assert r.status_code == 404


async def test_https_join_endpoint(client):
    a = (await client.post("/v1/networks", json={"label": "https-joe"})).json()
    r = await client.get(f"/v1/networks/{a['key']}/join", params={"agent_name": "Grok"})
    assert r.status_code == 200, r.text
    data = r.json()
    # the key itself is the credential on the channel
    j = await mt.join_network(a["key"])
    assert data["inbox_id"] == j["inbox_id"]
    assert data["token"] == a["key"]


async def test_rest_deliver_sender_param(client):
    a = (await client.post("/v1/networks", json={})).json()
    j = await mt.join_network(a["key"])
    iid = j["inbox_id"]
    r = await client.get(
        f"/v1/inboxes/{iid}/deliver",
        params={"token": a["key"], "body": "yo", "nonce": "n1", "sender": "Muse"},
    )
    assert r.status_code in (200, 202), r.text
    seen = await mt.check_messages(iid, a["key"])
    assert seen["messages"][-1]["sender"] == "Muse"
    # REST list also carries sender
    r2 = await client.get(
        f"/v1/inboxes/{iid}/messages", params={"token": a["key"]}, headers={}
    )
    assert r2.status_code == 200
    assert r2.json()["messages"][-1]["sender"] == "Muse"


async def test_network_mint_rate_limited(client):
    # 10/min/IP budget; hammer it and expect a 429.
    statuses = []
    for _ in range(14):
        r = await client.post("/v1/networks", json={})
        statuses.append(r.status_code)
    assert 429 in statuses, statuses
