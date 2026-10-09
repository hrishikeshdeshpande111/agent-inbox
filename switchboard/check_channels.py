import json, os, re, time, urllib.parse, urllib.request, uuid, sys

STATE = "/home/hatch/.config/agent-inbox/switchboard.state"
BASE = "https://agent-inbox-unngeg.fly.dev"

def http(method, url, headers=None, timeout=30):
    req = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:
        return -1, str(e)

def save_state(state):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE)

with open(STATE) as f:
    state = json.load(f)

results = []
pending = []  # (channel_id, message_dict) needing a human-written reply

for ch, info in state["channels"].items():
    inv = info["invite"]
    tok = inv.split("token=", 1)[1].split("&")[0]
    url = f"{BASE}/v1/inboxes/{ch}/messages?token={urllib.parse.quote(tok)}&limit=20"
    if info.get("last_seen"):
        url += "&after_id=" + urllib.parse.quote(info["last_seen"], safe="")
    st, body = http("GET", url)
    if st != 200:
        results.append({"channel": ch, "error": f"GET {st}", "detail": body[:120]})
        continue
    try:
        data = json.loads(body)
    except Exception as e:
        results.append({"channel": ch, "error": f"bad json {e}"})
        continue
    items = data if isinstance(data, list) else data.get("messages", data.get("items", []))
    items.reverse()  # oldest first
    own = set(info.get("own_ids", []))
    new_msgs = [m for m in items if m.get("id") != info.get("last_seen") and m.get("id") not in own]
    # Also drop own-sent messages detected by sender text
    new_msgs = [m for m in new_msgs if not str(m.get("sender","")).lower().startswith("muse")]
    if not new_msgs:
        results.append({"channel": ch, "new": 0})
        continue
    # Record newest id; pending replies handled by the orchestrator
    newest = new_msgs[-1]["id"]
    results.append({"channel": ch, "new": len(new_msgs),
                    "messages": [{"id": m.get("id"), "sender": m.get("sender"), "body": str(m.get("body",""))[:1500]} for m in new_msgs],
                    "newest_id": newest})
    info["last_seen"] = newest  # advance anchor only after replies sent; do it in a second pass below
    pending.append(ch)

# do NOT persist yet; orchestrator updates after replies
with open("/tmp/switchboard_pending.json", "w") as f:
    json.dump({"results": results, "pending": pending}, f)
print(json.dumps(results, indent=1))
