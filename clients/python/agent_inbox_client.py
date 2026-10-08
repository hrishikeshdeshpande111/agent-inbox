"""Minimal Python client for Agent Inbox.

pip-installable standalone (only needs httpx). Example:

    from agent_inbox_client import AgentInbox

    box = AgentInbox("http://localhost:8000")
    inbox = box.create_inbox(label="deploy alerts")
    print(inbox["url"], inbox["write_secret"])   # hand the URL to your webhook source

    for msg in box.messages(inbox["id"], inbox["read_secret"]):
        print(msg["body"])
        box.ack(inbox["id"], inbox["read_secret"], msg["id"])
"""

import httpx


class AgentInbox:
    def __init__(self, base_url: str, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout)

    def create_inbox(self, label: str | None = None) -> dict:
        r = self._http.post("/v1/inboxes", json={"label": label})
        r.raise_for_status()
        return r.json()

    def deliver(self, inbox_id: str, write_secret: str, payload: dict | str) -> dict:
        if isinstance(payload, dict):
            r = self._http.post(
                f"/v1/inboxes/{inbox_id}",
                json=payload,
                headers={"X-Write-Secret": write_secret},
            )
        else:
            r = self._http.post(
                f"/v1/inboxes/{inbox_id}",
                content=payload,
                headers={"X-Write-Secret": write_secret, "Content-Type": "text/plain"},
            )
        r.raise_for_status()
        return r.json()

    def messages(self, inbox_id: str, read_secret: str, limit: int = 50) -> list[dict]:
        out: list[dict] = []
        before_id: str | None = None
        while True:
            params = {"limit": min(limit, 200)}
            if before_id:
                params["before_id"] = before_id
            r = self._http.get(
                f"/v1/inboxes/{inbox_id}/messages",
                params=params,
                headers={"X-Read-Secret": read_secret},
            )
            r.raise_for_status()
            body = r.json()
            out.extend(body["messages"])
            before_id = body["next_before_id"]
            if not before_id or len(out) >= limit:
                break
        return out[:limit]

    def ack(self, inbox_id: str, read_secret: str, message_id: str) -> None:
        r = self._http.delete(
            f"/v1/inboxes/{inbox_id}/messages/{message_id}",
            headers={"X-Read-Secret": read_secret},
        )
        r.raise_for_status()

    def info(self, inbox_id: str, read_secret: str) -> dict:
        r = self._http.get(
            f"/v1/inboxes/{inbox_id}", headers={"X-Read-Secret": read_secret}
        )
        r.raise_for_status()
        return r.json()

    def rotate_secrets(self, inbox_id: str, read_secret: str) -> dict:
        r = self._http.post(
            f"/v1/inboxes/{inbox_id}/rotate", headers={"X-Read-Secret": read_secret}
        )
        r.raise_for_status()
        return r.json()

    def delete_inbox(self, inbox_id: str, read_secret: str) -> None:
        r = self._http.delete(
            f"/v1/inboxes/{inbox_id}", headers={"X-Read-Secret": read_secret}
        )
        r.raise_for_status()
