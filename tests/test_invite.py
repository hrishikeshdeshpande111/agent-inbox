"""Tests for one-link onboarding: GET /invite/{slug} mints a fresh channel
per opener and notifies the switchboard inbox. Run with: pytest."""

import os
import sys
import urllib.parse

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

os.environ["AGENT_INBOX_DB_PATH"] = "/tmp/agent-inbox-test-invite.db"
os.environ["AGENT_INBOX_RATE_LIMIT_PER_MIN"] = "1000"
os.environ["AGENT_INBOX_BASE_URL"] = "http://testserver"
os.environ["AGENT_INBOX_INVITE_SLUG"] = "hrishikesh"

if os.path.exists("/tmp/agent-inbox-test-invite.db"):
    os.remove("/tmp/agent-inbox-test-invite.db")

from agent_inbox import main as main_mod  # noqa: E402
from agent_inbox.main import app  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
async def _database():
    from agent_inbox.db import db as _db

    await _db.init()
    yield
    await _db.close()


@pytest.fixture(scope="module", autouse=True)
def _invite_slug():
    # settings is a process-wide singleton created by the first-importing
    # test module, so env vars set here would not take effect — set it
    # directly and restore afterwards.
    prev = main_mod.settings.invite_slug
    object.__setattr__(main_mod.settings, "invite_slug", "hrishikesh")
    yield
    object.__setattr__(main_mod.settings, "invite_slug", prev)


@pytest.fixture()
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


async def test_invite_mint_creates_channel(client):
    r = await client.get("/invite/hrishikesh")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/plain")
    text = r.text
    assert "Invite" in text
    assert "/mcp" in text
    # a fresh inbox id is embedded in the doc
    r2 = await client.get("/invite/hrishikesh")
    assert r2.status_code == 200
    assert r2.text != text  # every open mints a NEW channel


async def test_invite_wrong_slug_404(client):
    r = await client.get("/invite/someone-else")
    assert r.status_code == 404


async def test_invite_disabled_without_slug(client, monkeypatch):
    object.__setattr__(main_mod.settings, "invite_slug", None)
    try:
        r = await client.get("/invite/hrishikesh")
        assert r.status_code == 404
    finally:
        object.__setattr__(main_mod.settings, "invite_slug", "hrishikesh")


async def test_invite_html_variant(client):
    r = await client.get("/invite/hrishikesh", headers={"accept": "text/html"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "Muse" in r.text


async def test_invite_notifies_switchboard(client, monkeypatch):
    # operator's switchboard inbox
    r = await client.post("/v1/inboxes", json={"label": "switchboard"})
    assert r.status_code == 201
    sw = r.json()
    object.__setattr__(main_mod.settings, "switchboard_id", sw["id"])
    try:
        r2 = await client.get("/invite/hrishikesh")
        assert r2.status_code == 200
        # the minted channel id appears in the doc; pull it from a deliver URL
        qs = r2.text
        # switchboard got the announcement
        r3 = await client.get(
            f"/v1/inboxes/{sw['id']}/messages",
            headers={"X-Read-Secret": sw["read_secret"]},
        )
        assert r3.status_code == 200
        msgs = r3.json()["messages"]
        assert len(msgs) == 1
        body = msgs[0]["body"]
        assert "New channel opened from your invite link" in body
        assert "Invite: http://testserver/v1/inboxes/" in body
        assert "/llm.txt?token=" in body
        assert qs.split("inbox_id: ")[1].split("\n")[0] in body
    finally:
        object.__setattr__(main_mod.settings, "switchboard_id", None)
