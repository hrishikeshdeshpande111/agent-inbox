#!/usr/bin/env python3
"""Agent Inbox switchboard runner for Hrishikesh's invite-link channels.

Modes:
  scan    - read-only: fetch switchboard notes + per-channel messages, print new items (tokens masked)
  deliver - send replies: reads /tmp/switchboard_jobs.json {"greetings": {ch: body}, "replies": {ch: body}, "ack": [msg_id...]}
State and credentials are read/written by this script only; nothing secret is printed.
"""
import json, os, re, sys, urllib.request, urllib.parse, uuid

BASE = "https://agent-inbox-unngeg.fly.dev"
CFG = "/home/hatch/.config/agent-inbox"
ENVF = os.path.join(CFG, "switchboard.env")
STATEF = os.path.join(CFG, "switchboard.state")

def load_env():
    env = {}
    for line in open(ENVF):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env

def load_state():
    if not os.path.exists(STATEF):
        return {"last_switchboard_seen": None, "channels": {}}
    return json.load(open(STATEF))

def save_state(st):
    tmp = STATEF + ".tmp"
    json.dump(st, open(tmp, "w"), indent=2)
    os.rename(tmp, STATEF)

def mask(s):
    return re.sub(r"(token=)[^&\s\"']+", r"\1***", s)

def http(method, url, headers=None, timeout=25):
    req = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, f"TRANSPORT_ERROR: {e}"

def get_json(url, headers=None):
    st, body = http("GET", url, headers)
    if st != 200:
        return None, f"HTTP {st}: {mask(body)[:200]}"
    try:
        return json.loads(body), None
    except Exception as e:
        return None, f"BAD_JSON: {mask(body)[:200]}"

def scan():
    env = load_env()
    st = load_state()
    inbox = env["INBOX_ID"]
    rs = env["READ_SECRET"]
    out = {"new_notes": [], "channel_activity": {}, "errors": []}
    url = f"{BASE}/v1/inboxes/{inbox}/messages?limit=20"
    if st.get("last_switchboard_seen"):
        url += "&after_id=" + urllib.parse.quote(st["last_switchboard_seen"])
    data, err = get_json(url, {"X-Read-Secret": rs})
    if err:
        out["errors"].append(f"switchboard fetch: {err}")
        print(json.dumps(out, indent=2)); return
    msgs = data.get("messages", data) if isinstance(data, dict) else data
    if isinstance(msgs, dict):
        msgs = msgs.get("messages", [])
    for m in msgs:
        body = m.get("body", "") or m.get("text", "") or ""
        if "New channel opened" in body:
            chm = re.search(r"Channel:\s*(\S+)", body)
            invm = re.search(r"Invite:\s*(\S+)", body)
            out["new_notes"].append({
                "id": m.get("id"),
                "channel": chm.group(1) if chm else None,
                "invite": invm.group(1) if invm else None,
                "raw": mask(body)[:600],
            })
    # per-channel check
    for ch_id, ch in st.get("channels", {}).items():
        inv = ch.get("invite", "")
        pm = urllib.parse.urlparse(inv)
        token = urllib.parse.parse_qs(pm.query).get("token", [None])[0]
        if not token:
            out["errors"].append(f"{ch_id}: no token in invite")
            continue
        q = f"token={urllib.parse.quote(token)}&limit=20"
        if ch.get("last_seen"):
            q += "&after_id=" + urllib.parse.quote(ch["last_seen"])
        url = f"{BASE}/v1/inboxes/{ch_id}/messages?{q}"
        data, err = get_json(url)
        if err:
            out["errors"].append(f"{ch_id}: {err}")
            continue
        msgs = data.get("messages", data) if isinstance(data, dict) else data
        if isinstance(msgs, dict):
            msgs = msgs.get("messages", [])
        own = set(ch.get("own_ids", []))
        new = []
        for m in msgs:
            if m.get("id") in own:
                continue
            new.append(m)
        if new:
            out["channel_activity"][ch_id] = [
                {"id": m.get("id"), "sender": m.get("sender"),
                 "body": mask(m.get("body", "") or m.get("text", ""))[:800]}
                for m in new
            ]
    print(json.dumps(out, indent=2))

def deliver():
    env = load_env()
    st = load_state()
    inbox = env["INBOX_ID"]
    rs = env["READ_SECRET"]
    jobs = json.load(open("/tmp/switchboard_jobs.json"))
    report = {"greeted": [], "replied": [], "acked": [], "errors": []}
    # greetings for new channels
    for ch_id, body in (jobs.get("greetings") or {}).items():
        inv = st["channels"][ch_id]["invite"]
        pm = urllib.parse.urlparse(inv)
        token = urllib.parse.parse_qs(pm.query).get("token", [None])[0]
        nonce = uuid.uuid4().hex
        q = urllib.parse.urlencode({"token": token, "body": body, "nonce": nonce})
        durl = f"{BASE}/v1/inboxes/{ch_id}/deliver?{q}"
        d, err = get_json(durl)
        if err:
            report["errors"].append(f"greet {ch_id}: {err}")
            continue
        mid = d.get("message_id") or d.get("id")
        st["channels"][ch_id]["last_seen"] = mid
        st["channels"][ch_id].setdefault("own_ids", []).append(mid)
        report["greeted"].append({"channel": ch_id, "message_id": mid})
        save_state(st)
    # replies
    for ch_id, body in (jobs.get("replies") or {}).items():
        inv = st["channels"][ch_id]["invite"]
        pm = urllib.parse.urlparse(inv)
        token = urllib.parse.parse_qs(pm.query).get("token", [None])[0]
        nonce = uuid.uuid4().hex
        q = urllib.parse.urlencode({"token": token, "body": body, "nonce": nonce})
        durl = f"{BASE}/v1/inboxes/{ch_id}/deliver?{q}"
        d, err = get_json(durl)
        if err:
            report["errors"].append(f"reply {ch_id}: {err}")
            continue
        mid = d.get("message_id") or d.get("id")
        st["channels"][ch_id]["last_seen"] = mid
        st["channels"][ch_id].setdefault("own_ids", []).append(mid)
        report["replied"].append({"channel": ch_id, "message_id": mid})
        save_state(st)
    # register new channels (no greeting text yet) - handled by caller writing state first
    # ack switchboard notes
    for mid in (jobs.get("ack") or []):
        url = f"{BASE}/v1/inboxes/{inbox}/messages/{urllib.parse.quote(mid)}"
        scode, sbody = http("DELETE", url, {"X-Read-Secret": rs})
        if scode in (200, 204, 404):
            report["acked"].append(mid)
        else:
            report["errors"].append(f"ack {mid}: HTTP {scode} {mask(sbody)[:150]}")
    if report["acked"]:
        st["last_switchboard_seen"] = report["acked"][-1]
        save_state(st)
    print(json.dumps(report, indent=2))

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "scan"
    {"scan": scan, "deliver": deliver}[mode]()
