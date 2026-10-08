/**
 * Minimal JS client for Agent Inbox (Node 18+, uses global fetch).
 *
 *   import { AgentInbox } from "./index.js";
 *   const box = new AgentInbox("http://localhost:8000");
 *   const inbox = await box.createInbox("deploy alerts");
 *   // hand inbox.url to your webhook source, then:
 *   for (const msg of await box.messages(inbox.id, inbox.read_secret)) {
 *     console.log(msg.body);
 *     await box.ack(inbox.id, inbox.read_secret, msg.id);
 *   }
 */

export class AgentInbox {
  constructor(baseUrl, { timeoutMs = 15000 } = {}) {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.timeoutMs = timeoutMs;
  }

  async #req(path, { method = "GET", secret, secretKind = "read", body, query = {} } = {}) {
    const url = new URL(this.baseUrl + path);
    for (const [k, v] of Object.entries(query)) if (v != null) url.searchParams.set(k, v);
    const headers = {};
    if (secret) headers[secretKind === "read" ? "X-Read-Secret" : "X-Write-Secret"] = secret;
    let payload;
    if (body !== undefined) {
      if (typeof body === "string") {
        headers["Content-Type"] = "text/plain";
        payload = body;
      } else {
        headers["Content-Type"] = "application/json";
        payload = JSON.stringify(body);
      }
    }
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), this.timeoutMs);
    try {
      const res = await fetch(url, { method, headers, body: payload, signal: ctrl.signal });
      if (!res.ok) {
        const detail = await res.text().catch(() => "");
        throw new Error(`AgentInbox ${method} ${path} -> ${res.status}: ${detail}`);
      }
      const text = await res.text();
      return text ? JSON.parse(text) : {};
    } finally {
      clearTimeout(t);
    }
  }

  createInbox(label) {
    return this.#req("/v1/inboxes", { method: "POST", body: { label } });
  }

  deliver(inboxId, writeSecret, payload) {
    return this.#req(`/v1/inboxes/${inboxId}`, {
      method: "POST", secret: writeSecret, secretKind: "write", body: payload,
    });
  }

  async messages(inboxId, readSecret, limit = 50) {
    const out = [];
    let beforeId = null;
    while (out.length < limit) {
      const page = await this.#req(`/v1/inboxes/${inboxId}/messages`, {
        secret: readSecret,
        query: { limit: Math.min(limit, 200), before_id: beforeId },
      });
      out.push(...page.messages);
      beforeId = page.next_before_id;
      if (!beforeId) break;
    }
    return out.slice(0, limit);
  }

  ack(inboxId, readSecret, messageId) {
    return this.#req(`/v1/inboxes/${inboxId}/messages/${messageId}`, { method: "DELETE", secret: readSecret });
  }

  info(inboxId, readSecret) {
    return this.#req(`/v1/inboxes/${inboxId}`, { secret: readSecret });
  }

  rotateSecrets(inboxId, readSecret) {
    return this.#req(`/v1/inboxes/${inboxId}/rotate`, { method: "POST", secret: readSecret });
  }

  deleteInbox(inboxId, readSecret) {
    return this.#req(`/v1/inboxes/${inboxId}`, { method: "DELETE", secret: readSecret });
  }
}
