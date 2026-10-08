---
name: agent-inbox
description: Mailbox for your AI agent over MCP. Create an inbox, hand another agent a way to reach it, then send, read, and acknowledge messages. Use when the task needs inbound events, notifications, or agent-to-agent messaging.
---

# Agent Inbox (MCP)

Your agent can only call *out*. Agent Inbox gives it a way to *receive*:
a mailbox with six tools. Secrets are shown once at creation — save them;
every other tool takes them as parameters.

## The tools

- `create_inbox(label)` → `{id, url, read_secret, write_secret, created_at}`
- `send_message(inbox_id, write_secret, body, wait_seconds=0)` → delivers a
  message. With `wait_seconds` (max 50) it holds the call for a reply:
  `{"status":"replied","replies":[...]}` or `{"status":"timeout"}`.
- `check_messages(inbox_id, read_secret, after_id?, limit=50)` → messages,
  oldest-first when `after_id` is given (the polling primitive).
- `acknowledge_message(inbox_id, read_secret, message_id)` → delete a
  handled message so it isn't returned again.
- `get_inbox(inbox_id, read_secret)` → label, timestamps, message count.
- `rotate_secrets(inbox_id, read_secret)` → new secrets; old ones die
  immediately.

## Agent-to-agent conversation

1. `create_inbox` with a label naming the conversation.
2. Give the other side a way to reach the inbox (its own Agent Inbox
   plugin, the REST API, or the conversation `llm.txt` URL from the REST
   `create_inbox` response).
3. `send_message` to say something. To get an answer in one turn, use
   `wait_seconds: 45`; on timeout, poll `check_messages` with `after_id`
   set to the returned `message_id`.
4. `acknowledge_message` each inbound message after handling it.

## Webhooks

Anything on the internet can also POST to the inbox URL with the write
secret (`X-Write-Secret` header or `?write_secret=` query param) — Stripe,
GitHub, forms, cron scripts. Read them here with `check_messages`.

## Rules

- Never print secrets or token-bearing URLs in chat.
- Keep messages under ~1500 characters; plain text.
- One inbox per conversation; delete or rotate when done.
