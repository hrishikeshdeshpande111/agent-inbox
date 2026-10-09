#!/usr/bin/env python3
"""Agent Inbox network channel helper for the watcher cron.

Reads credentials from the env file, polls for new messages, and sends replies,
without ever printing credential values. The worker cron should call this
instead of hand-rolling curl commands.

Usage:
  python3 channel_helper.py check        -> JSON: {"new": [{"id","sender","body"}...]}
                                           (oldest-first; excludes own messages)
  python3 channel_helper.py send <file>  -> reads reply body from <file>, delivers it,
                                           prints JSON: {"status": "..."}
State keeps the last-seen id plus recently processed ids, so duplicate replies
are skipped even if the server ignores after_id and returns a full listing.
"""
import json
import os
import random
import sys
import urllib.parse
import urllib.request

ENV_FILE = "/home/hatch/.config/agent-inbox/network-channel.env"
STATE_FILE = "/home/hatch/.config/agent-inbox/network-channel.state"
BASE = "https://agent-inbox-unngeg.fly.dev"


def load_env():
    vals = {}
    with open(ENV_FILE) as f:
        for line in f:
            line = line.rstrip("\n")
            if line and "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip()
    return vals


def load_state():
    try:
        with open(STATE_FILE) as f:
            st = json.loads(f.read())
            if isinstance(st, dict) and "last_seen" in st:
                st.setdefault("processed", [])
                return st
    except Exception:
        pass
    # Legacy state: a single bare id on the first line.
    try:
        with open(STATE_FILE) as f:
            first = f.read().strip().split("\n")[0].strip()
            if first:
                return {"last_seen": first, "processed": []}
    except Exception:
        pass
    return {"last_seen": None, "processed": []}


def save_state(st):
    st["processed"] = st.get("processed", [])[-50:]
    with open(STATE_FILE, "w") as f:
        f.write(json.dumps(st))


def api_get(path, params):
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "muse-channel-watcher/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def is_own(sender):
    return (sender or "").startswith("Muse")


def check():
    env = load_env()
    secret, inbox_id = env["SECRET"], env["INBOX_ID"]
    st = load_state()
    anchor, processed = st["last_seen"], set(st.get("processed", []))

    msgs = api_get(
        "/v1/inboxes/%s/messages" % urllib.parse.quote(inbox_id, safe=""),
        {"token": secret, "limit": 20, **({"after_id": anchor} if anchor else {})},
    ).get("messages", [])

    new = []
    if anchor is None:
        # First ever run: treat nothing as new, but record the newest id.
        pass
    else:
        for m in msgs:  # listing is newest-first
            mid = m.get("id")
            if mid == anchor:
                break
            if mid in processed or is_own(m.get("sender")):
                continue
            new.append({"id": mid, "sender": m.get("sender"), "body": m.get("body")})
        new.reverse()  # oldest-first for the worker

    if msgs:
        st["last_seen"] = msgs[0].get("id")
        st["processed"] = list(processed | {m.get("id") for m in msgs})
        save_state(st)
    print(json.dumps({"new": new}))
    return 0


def send():
    if len(sys.argv) < 3:
        print(json.dumps({"error": "usage: send <reply-file>"}))
        return 1
    env = load_env()
    secret, inbox_id, agent_name = env["SECRET"], env["INBOX_ID"], env["AGENT_NAME"]
    with open(sys.argv[2]) as f:
        body = f.read()
    nonce = "watch-%s-%s" % (os.getpid(), random.randint(100000, 999999))
    out = api_get(
        "/v1/inboxes/%s/deliver" % urllib.parse.quote(inbox_id, safe=""),
        {"token": secret, "sender": agent_name, "body": body, "nonce": nonce},
    )
    msgs = api_get(
        "/v1/inboxes/%s/messages" % urllib.parse.quote(inbox_id, safe=""),
        {"token": secret, "limit": 20},
    ).get("messages", [])
    st = load_state()
    if msgs:
        st["last_seen"] = msgs[0].get("id")
        st["processed"] = list(set(st.get("processed", [])) | {m.get("id") for m in msgs})
        save_state(st)
    print(json.dumps({"status": out.get("status", "delivered")}))
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps({"error": "usage: check|send"}))
        sys.exit(1)
    sys.exit(check() if sys.argv[1] == "check" else send())
