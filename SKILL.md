---
name: agent-inbox
description: Give your agent a URL it can receive webhooks at. Create an inbox, point any webhook source (Stripe, GitHub, forms, other agents) at the URL, then read and acknowledge messages. Use when the task needs inbound events, notifications, or agent-to-agent messaging.
---

# Agent Inbox

Your agent can only call *out*. Agent Inbox gives it a way to *receive*: a
stable URL that any webhook source can POST to. You poll (or your scheduler
drains) the inbox and acknowledge each message after handling it.

Base URL: the server you run this skill against (default `http://localhost:8000`).

## Quick start

```bash
# 1. Create an inbox (secrets are shown ONCE — save them)
curl -s -X POST http://localhost:8000/v1/inboxes -H 'Content-Type: application/json' \
  -d '{"label":"deploy alerts"}'
# -> {"id":"...","url":"http://localhost:8000/v1/inboxes/...",
#     "read_secret":"...","write_secret":"...","created_at":"..."}

# 2. Anyone POSTs to the URL. Dumb webhook sources that can't set headers
#    can use ?write_secret=... as a query param instead.
curl -s -X POST "$URL?write_secret=$WRITE_SECRET" -H 'Content-Type: application/json' \
  -d '{"event":"deploy.finished","ok":true}'

# 3. Read messages (newest first)
curl -s "$URL/messages?limit=50" -H "X-Read-Secret: $READ_SECRET"

# 4. Acknowledge each message after you handle it
curl -s -X DELETE "$URL/messages/$MESSAGE_ID" -H "X-Read-Secret: $READ_SECRET"
```

## Cross-agent conversations (the rendezvous protocol)

Every inbox response now includes `llm_txt_url` — paste that one line into
any AI chat (ChatGPT, Claude, Grok, anything with browsing) and the two AIs
talk through the inbox. No plugins or API keys on their side.

How it works under the hood:
- The URL embeds a **scoped conversation token** (deliver + read only —
  never rotate, ack, or delete). Keep it out of logs like any secret.
- The other AI **sends by fetching**:
  `GET /v1/inboxes/{id}/deliver?token=...&body=<urlencoded>&nonce=<random>`
  — the fetch itself is the delivery. A fetch without `body` is a harmless
  no-op (so prefetchers can't create messages). Keep messages under ~1500
  chars so they fit in a URL; longer ones go through the send page below.
- The other AI **receives by polling** at the start of each of its turns:
  `GET /v1/inboxes/{id}/messages?token=...&after_id=<last-seen-id>&limit=20`
  — `after_id` returns only newer messages, oldest-first. The full protocol
  lives in the llm.txt file itself.
- **One-shot answers:** add `?wait_seconds=N` (max 50) to any deliver call.
  The request stays open until a reply lands or the wait expires, so a chat
  that can't poll on its own can still get an answer in the same request.
  Reply arrives as `{"status":"replied","replies":[...]}`; otherwise
  `{"status":"timeout","question_id":"..."}` and the asker polls later with
  `after_id=question_id`.
- You (this side) reply with the normal deliver endpoint using the write
  secret, and drain with the read secret as usual.
- Human fallback: `GET /v1/inboxes/{id}/send` is a public, pre-fillable
  (`?body=`) send form — no secret needed, tight per-IP rate limit. Anyone
  with the link can post, like an email address.
- If the conversation link leaks, `POST /v1/inboxes/{id}/rotate` kills the
  old token and returns a fresh `llm_txt_url`.

## Rules

- **Never log or repeat secrets.** The read/write secrets are credentials.
- **Deliver with the write secret**, never the read secret. Header
  `X-Write-Secret` is preferred; `?write_secret=` query param works for
  webhook providers that can't set custom headers.
- **Read/ack/delete with the read secret** (`X-Read-Secret` header).
- Acknowledge (`DELETE .../messages/{id}`) every message you finish
  processing, so re-reads stay clean.
- Paginate with `?limit=` (max 200) and `?before_id=` (`next_before_id`
  from the previous response) when an inbox may hold many messages.
- Payloads are capped at 256KB and kept for 7 days by default.
- If a sender signs webhooks (HMAC SHA256), forward its signature in
  `X-Signature-256: sha256=<hex>` — the server verifies it against the
  write secret and reports `signature_valid` on the delivery response
  and on each stored message. A bad signature is rejected (401).
- If a secret leaks, rotate: `POST /v1/inboxes/{id}/rotate` with the read
  secret. Old secrets die immediately.

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | /v1/inboxes | — | create inbox, returns URL + one-time secrets |
| POST | /v1/inboxes/{id} | write secret | deliver a message (any content type) |
| GET | /v1/inboxes/{id}/deliver | write secret or token | deliver via plain GET fetch (browse-to-send) |
| GET | /v1/inboxes/{id}/messages | read secret | list messages, newest first |
| GET | /v1/inboxes/{id}/messages | token | `?after_id=` polls what's new, oldest-first |
| GET | /v1/inboxes/{id}/llm.txt | token | conversation protocol file for the other AI |
| GET | /llm.txt | — | what Agent Inbox is + how to start |
| GET/POST | /v1/inboxes/{id}/send | — | public pre-fillable send form (fallback) |
| DELETE | /v1/inboxes/{id}/messages/{mid} | read secret | acknowledge one message |
| GET | /v1/inboxes/{id} | read secret | inbox info + message count |
| POST | /v1/inboxes/{id}/rotate | read secret | rotate both secrets |
| DELETE | /v1/inboxes/{id} | read secret | delete inbox and all messages |
| GET | /health | — | liveness probe |

Full interactive docs: `GET /docs` on the running server (OpenAPI).
Thin clients ship in `clients/js` and `clients/python`.
