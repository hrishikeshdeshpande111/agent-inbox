"""Tests for the agent-to-agent rendezvous protocol.

Covers: llm.txt endpoints, GET-based delivery (browse-to-send), after_id
polling, conversation-token scoping, and the public send page.
Run with:  pytest  (from the repo root, venv active).
"""

import os
import sys
import urllib.parse

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

os.environ["AGENT_INBOX_DB_PATH"] = "/tmp/agent-inbox-test-conv.db"
os.environ["AGENT_INBOX_RATE_LIMIT_PER_MIN"] = "1000"
os.environ["AGENT_INBOX_BASE_URL"] = "http://testserver"

if os.path.exists("/tmp/agent-inbox-test-conv.db"):
    os.remove("/tmp/agent-inbox-test-conv.db")

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


async def _create(client, label="conv") -> dict:
    r = await client.post("/v1/inboxes", json={"label": label})
    assert r.status_code == 201, r.text
    return r.json()


def _token(llm_txt_url: str) -> str:
    return urllib.parse.parse_qs(urllib.parse.urlparse(llm_txt_url).query)["token"][0]


async def test_root_llm_txt(client):
    r = await client.get("/llm.txt")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "Muse" in r.text and "llm_txt_url" in r.text


async def test_create_returns_llm_txt_url(client):
    inbox = await _create(client)
    assert inbox["llm_txt_url"].startswith("http://testserver/v1/inboxes/")
    assert inbox["llm_txt_url"].endswith("/llm.txt?token=") is False
    assert "token=" in inbox["llm_txt_url"]


async def test_conversation_llm_txt_contents(client):
    inbox = await _create(client, label="grok-chat")
    tok = _token(inbox["llm_txt_url"])
    r = await client.get(f"/v1/inboxes/{inbox['id']}/llm.txt?token={tok}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert inbox["id"] in r.text
    assert tok in r.text  # deliver/read URLs embed the token
    assert "grok-chat" in r.text
    assert "MODE A" in r.text and "MODE B" in r.text
    # master secrets must NOT appear in the conversation file
    assert inbox["write_secret"] not in r.text
    assert inbox["read_secret"] not in r.text


async def test_llm_txt_requires_token(client):
    inbox = await _create(client)
    r = await client.get(f"/v1/inboxes/{inbox['id']}/llm.txt")
    assert r.status_code == 401
    r = await client.get(f"/v1/inboxes/{inbox['id']}/llm.txt?token=wrong")
    assert r.status_code == 401
    r = await client.get("/v1/inboxes/doesnotexist/llm.txt?token=x")
    assert r.status_code == 404


async def test_deliver_via_get_roundtrip(client):
    inbox = await _create(client)
    tok = _token(inbox["llm_txt_url"])
    iid = inbox["id"]
    # no body -> harmless no-op, never a message
    r = await client.get(f"/v1/inboxes/{iid}/deliver?token={tok}&nonce=1")
    assert r.status_code == 200
    assert "deliver a message" in r.text.lower()
    # real delivery via plain GET fetch
    r = await client.get(
        f"/v1/inboxes/{iid}/deliver",
        params={"token": tok, "body": "hello from grok", "nonce": "abc123"},
    )
    assert r.status_code == 202, r.text
    mid = r.json()["message_id"]
    # read back with the token (no read secret needed)
    r = await client.get(f"/v1/inboxes/{iid}/messages?token={tok}")
    assert r.status_code == 200
    msgs = r.json()["messages"]
    assert len(msgs) == 1 and msgs[0]["body"] == "hello from grok"
    assert msgs[0]["id"] == mid


async def test_token_scoping(client):
    inbox = await _create(client)
    tok = _token(inbox["llm_txt_url"])
    iid = inbox["id"]
    # token must not authorize rotate / delete / ack
    r = await client.post(f"/v1/inboxes/{iid}/rotate", headers={"X-Read-Secret": tok})
    assert r.status_code == 401
    r = await client.delete(f"/v1/inboxes/{iid}", headers={"X-Read-Secret": tok})
    assert r.status_code == 401
    # token must not pass as the write/read secrets either
    r = await client.post(f"/v1/inboxes/{iid}", json={}, headers={"X-Write-Secret": tok})
    assert r.status_code == 401
    r = await client.get(f"/v1/inboxes/{iid}/messages", headers={"X-Read-Secret": tok})
    assert r.status_code == 401


async def test_after_id_polling(client):
    inbox = await _create(client)
    tok = _token(inbox["llm_txt_url"])
    iid = inbox["id"]
    ids = []
    for text in ("one", "two", "three"):
        r = await client.get(
            f"/v1/inboxes/{iid}/deliver",
            params={"token": tok, "body": text, "nonce": text},
        )
        assert r.status_code == 202
        ids.append(r.json()["message_id"])
    # after the first -> only newer, oldest-first
    r = await client.get(f"/v1/inboxes/{iid}/messages?token={tok}&after_id={ids[0]}")
    assert r.status_code == 200
    msgs = r.json()["messages"]
    assert [m["body"] for m in msgs] == ["two", "three"]
    # after the latest -> empty
    r = await client.get(f"/v1/inboxes/{iid}/messages?token={tok}&after_id={ids[2]}")
    assert r.json()["messages"] == []
    # unknown after_id -> ignored, default listing returned
    r = await client.get(f"/v1/inboxes/{iid}/messages?token={tok}&after_id=nope")
    assert r.status_code == 200
    assert len(r.json()["messages"]) == 3


async def test_rotate_invalidates_conversation_token(client):
    inbox = await _create(client)
    old_tok = _token(inbox["llm_txt_url"])
    iid = inbox["id"]
    r = await client.post(
        f"/v1/inboxes/{iid}/rotate", headers={"X-Read-Secret": inbox["read_secret"]}
    )
    assert r.status_code == 200
    new_tok = _token(r.json()["llm_txt_url"])
    assert new_tok != old_tok
    # old token dead everywhere
    r = await client.get(f"/v1/inboxes/{iid}/llm.txt?token={old_tok}")
    assert r.status_code == 401
    r = await client.get(f"/v1/inboxes/{iid}/deliver?token={old_tok}&body=x&nonce=1")
    assert r.status_code == 401
    # new token works
    r = await client.get(f"/v1/inboxes/{iid}/llm.txt?token={new_tok}")
    assert r.status_code == 200


async def test_wait_for_reply(client):
    import asyncio

    inbox = await _create(client)
    tok = _token(inbox["llm_txt_url"])
    iid = inbox["id"]

    async def ask():
        return await client.get(
            f"/v1/inboxes/{iid}/deliver",
            params={"token": tok, "body": "what happened to my tesla application?",
                    "nonce": "n1", "wait_seconds": 8},
            timeout=30,
        )

    task = asyncio.create_task(ask())
    await asyncio.sleep(1.5)  # let the wait begin
    # Muse replies via the normal write-secret path
    r = await client.post(
        f"/v1/inboxes/{iid}",
        content=b"tesla: filed and confirmed",
        headers={"Content-Type": "text/plain", "X-Write-Secret": inbox["write_secret"]},
    )
    assert r.status_code == 202
    r = await task
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "replied"
    assert any(m["body"] == "tesla: filed and confirmed" for m in data["replies"])
    # the asker's own question is not presented as a reply
    assert all(m["id"] != data["question_id"] for m in data["replies"])


async def test_wait_timeout(client):
    inbox = await _create(client)
    tok = _token(inbox["llm_txt_url"])
    r = await client.get(
        f"/v1/inboxes/{inbox['id']}/deliver",
        params={"token": tok, "body": "anybody home?", "nonce": "n2", "wait_seconds": 2},
        timeout=30,
    )
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "timeout"
    assert "question_id" in data


async def test_send_page(client):
    inbox = await _create(client, label="sendpage")
    iid = inbox["id"]
    r = await client.get(f"/v1/inboxes/{iid}/send")
    assert r.status_code == 200
    assert "<form" in r.text and "textarea" in r.text
    # prefill
    r = await client.get(f"/v1/inboxes/{iid}/send?body=hi+there")
    assert "hi there" in r.text
    # submit the form (urlencoded, no secret)
    r = await client.post(
        f"/v1/inboxes/{iid}/send",
        content=b"body=form+message+here",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 200
    assert "delivered" in r.text.lower()
    # message landed, readable with the real read secret
    r = await client.get(
        f"/v1/inboxes/{iid}/messages", headers={"X-Read-Secret": inbox["read_secret"]}
    )
    assert any(m["body"] == "form message here" for m in r.json()["messages"])
    # empty body rejected
    r = await client.post(
        f"/v1/inboxes/{iid}/send",
        content=b"body=%20%20",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 400
    # unknown inbox 404s
    r = await client.get("/v1/inboxes/doesnotexist/send")
    assert r.status_code == 404
    # the send page shows recent messages (human-assisted read path)
    r = await client.get(f"/v1/inboxes/{iid}/send")
    assert r.status_code == 200
    assert "form message here" in r.text
    assert "Recent messages" in r.text


async def test_deliver_get_html_confirmation(client):
    inbox = await _create(client)
    tok = _token(inbox["llm_txt_url"])
    # a browser (address-bar paste) gets a readable confirmation page
    r = await client.get(
        f"/v1/inboxes/{inbox['id']}/deliver",
        params={"token": tok, "body": "via address bar", "nonce": "n3"},
        headers={"accept": "text/html,application/xhtml+xml"},
    )
    assert r.status_code == 200
    assert "Message delivered" in r.text
    # a non-browser client still gets JSON
    r = await client.get(
        f"/v1/inboxes/{inbox['id']}/deliver",
        params={"token": tok, "body": "via api", "nonce": "n4"},
        headers={"accept": "application/json"},
    )
    assert r.json()["message_id"]
