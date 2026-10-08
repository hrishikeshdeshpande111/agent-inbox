# Agent Inbox

**A mailbox for your AI agent.** Give it a URL — anything that can POST a webhook can reach it.

AI agents today can only call *out*. They can't receive Stripe notifications, GitHub events, form submissions, or messages from other agents, because they have no inbound address. Agent Inbox is that address: one API call creates an inbox and returns a stable URL; webhook sources deliver JSON to it; your agent reads and acknowledges messages on its own schedule.

```
agent ──POST /v1/inboxes──▶  https://your-server/v1/inboxes/aBc12XyZ
                                                        │
Stripe / GitHub / forms / another agent ──POST─────────▶│──▶ agent reads via API
```

MIT licensed. Single binary-ish deploy (Python + SQLite). No external services required.

## Quickstart

```bash
docker compose up -d        # serves API + landing page on :8000
# or without docker:
pip install -r requirements.txt
python -m uvicorn agent_inbox.main:app --app-dir src --port 8000
```

Then:

```bash
# 1. Create an inbox (secrets are shown ONCE — save them)
curl -s -X POST localhost:8000/v1/inboxes \
  -H 'Content-Type: application/json' -d '{"label":"deploy alerts"}'
# {"id":"...","url":"http://localhost:8000/v1/inboxes/...",
#  "read_secret":"...","write_secret":"...","created_at":"..."}

# 2. Deliver (header auth preferred; ?write_secret= works for webhook
#    providers that can't set custom headers)
curl -s -X POST "$URL?write_secret=$WRITE_SECRET" \
  -H 'Content-Type: application/json' -d '{"event":"deploy.finished"}'

# 3. Read (newest first) and acknowledge
curl -s "$URL/messages" -H "X-Read-Secret: $READ_SECRET"
curl -s -X DELETE "$URL/messages/$MID" -H "X-Read-Secret: $READ_SECRET"
```

Open `http://localhost:8000` for the landing page and `/docs` for interactive OpenAPI docs.

## For agents

`SKILL.md` ships in the repo root — drop it into your agent's skills directory and any agent (Claude Code, Cursor, Codex, …) can create inboxes, deliver, read, and ack unassisted. Thin clients live in `clients/js` and `clients/python`.

## MCP (native tools)

The app also exposes a Streamable HTTP MCP server at `/mcp` with first-class tools, so MCP-capable clients (ChatGPT plugins, Claude, agent frameworks) don't need hand-rolled HTTP:

| Tool | Purpose |
|---|---|
| `create_inbox` | create inbox → id, URL, one-time secrets |
| `send_message` | deliver a message; `wait_seconds` (max 50) holds for a reply |
| `check_messages` | read messages (`after_id` polls for new, oldest-first) |
| `acknowledge_message` | delete a handled message |
| `get_inbox` | info + unacknowledged message count |
| `rotate_secrets` | rotate both secrets (old die immediately) |

Every tool except `create_inbox` takes the inbox's `read_secret` / `write_secret` as parameters — the client holds the credentials from creation, mirroring the REST auth model. Point your MCP client at `https://<your-host>/mcp`.

## API

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/v1/inboxes` | — | create inbox → URL + one-time secrets |
| `POST` | `/v1/inboxes/{id}` | write secret | deliver a message (any content type, 256KB cap) |
| `GET` | `/v1/inboxes/{id}/messages` | read secret | list, newest first (`?limit`, `?before_id`) |
| `DELETE` | `/v1/inboxes/{id}/messages/{mid}` | read secret | acknowledge one message |
| `GET` | `/v1/inboxes/{id}` | read secret | info + message count |
| `POST` | `/v1/inboxes/{id}/rotate` | read secret | rotate both secrets (old die immediately) |
| `DELETE` | `/v1/inboxes/{id}` | read secret | delete inbox + all messages |
| `GET` | `/health` | — | liveness probe |

Auth: `X-Write-Secret` / `X-Read-Secret` headers, or `?write_secret=` / `?read_secret=` query params. Senders that sign webhooks (HMAC-SHA256) can pass `X-Signature-256: sha256=<hex>` — the server verifies it against the write secret, rejects bad signatures (401), and records `signature_valid` on the message.

## Security model

- **Unguessable IDs** — inbox and message IDs are 128-bit random tokens, not sequential integers.
- **Secrets shown once** — read/write secrets are returned at creation/rotation and stored only as SHA-256 hashes. A database leak grants nothing.
- **Constant-time comparison** on every secret check.
- **Read/write separation** — the secret that delivers cannot read, and vice versa.
- **HMAC webhook verification** for signed senders (Stripe, GitHub, …).
- **Rate limiting** — per client IP and per (inbox, IP), `429` + `Retry-After` when exceeded.
- **Payload caps** — 256KB max per message (configurable), UTF-8 enforced.
- **Header hygiene** — `Authorization`-like headers are redacted before storage, never persisted raw.
- **Retention** — messages auto-purge after `AGENT_INBOX_RETENTION_DAYS` (default 7). Inboxes are never auto-deleted.

## Configuration

All via environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_INBOX_DB_PATH` | `data/agent-inbox.db` | SQLite file |
| `AGENT_INBOX_BASE_URL` | `http://localhost:8000` | Public URL baked into inbox URLs — **set this in production** |
| `AGENT_INBOX_RETENTION_DAYS` | `7` | Message TTL |
| `AGENT_INBOX_RATE_LIMIT_PER_MIN` | `60` | Requests/min per IP and per (inbox, IP) |
| `AGENT_INBOX_MAX_BODY_BYTES` | `262144` | Max payload size |
| `AGENT_INBOX_CLEANUP_INTERVAL_SEC` | `3600` | Retention sweep interval |

## Deploying

The Docker image is self-contained (SQLite volume at `/app/data`). Set `AGENT_INBOX_BASE_URL` to your public domain — it goes into every inbox URL handed out. Put it behind any reverse proxy/TLS terminator; the app trusts `X-Forwarded-For`'s leftmost entry for rate limiting.

```bash
AGENT_INBOX_BASE_URL=https://inbox.example.com docker compose up -d
```

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && pip install pytest pytest-asyncio
pytest -q
```

CI runs the full suite plus a Docker build + health-check smoke test on every push.

## Layout

```
src/agent_inbox/   FastAPI service (config, db, security, rate_limit, cleanup, main)
static/index.html  landing page (served at /)
clients/js         zero-dependency JS client
clients/python     httpx-based Python client
SKILL.md           agent skill definition
tests/             pytest suite (13 tests)
```

## License

MIT — see [LICENSE](LICENSE).
