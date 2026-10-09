import json, os, time, urllib.parse, urllib.request, uuid, sys

CFG = "/home/hatch/.config/agent-inbox/switchboard.env"
STATE = "/home/hatch/.config/agent-inbox/switchboard.state"
BASE = "https://agent-inbox-unngeg.fly.dev"

def load_env():
    d = {}
    with open(CFG) as f:
        for line in f:
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                d[k] = v
    return d

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

env = load_env()
INBOX_ID = env["INBOX_ID"]
READ_SECRET = env["READ_SECRET"]

if os.path.exists(STATE):
    with open(STATE) as f:
        state = json.load(f)
else:
    state = {"last_switchboard_seen": None, "channels": {}}

state.setdefault("channels", {})

hdrs = {"X-Read-Secret": READ_SECRET}
url = f"{BASE}/v1/inboxes/{INBOX_ID}/messages?limit=20"
if state.get("last_switchboard_seen"):
    url += "&after_id=" + urllib.parse.quote(state["last_switchboard_seen"], safe="")
st, body = http("GET", url, hdrs)
report = []
if st != 200:
    print(json.dumps({"error": f"switchboard GET {st}", "body": body[:300]}))
    sys.exit(1)
try:
    msgs = json.loads(body)
except Exception as e:
    print(json.dumps({"error": f"bad json: {e}", "body": body[:300]}))
    sys.exit(1)

items = msgs if isinstance(msgs, list) else msgs.get("messages", msgs.get("items", []))
new_notes = [m for m in items if "New channel opened" in str(m.get("body", "")) or "New channel opened" in str(m.get("subject",""))]
new_notes = [m for m in new_notes if m.get("id") != state.get("last_switchboard_seen")]
new_notes.reverse()  # oldest first

for note in new_notes:
    txt = str(note.get("body", "")) + "\n" + str(note.get("subject", ""))
    ch = None; inv = None
    for line in txt.replace("\\n", "\n").splitlines():
        l = line.strip()
        if l.lower().startswith("channel:"):
            ch = l.split(":", 1)[1].strip()
        if "Invite:" in line or "llm.txt?token=" in l:
            # grab a url with llm.txt?token=
            import re
            mm = re.search(r"(https?://\S*llm\.txt\?token=\S+)", line)
            if mm:
                inv = mm.group(1).rstrip(").,")
    if ch and inv:
        state["channels"][ch] = {"invite": inv, "last_seen": None, "own_ids": []}
        save_state(state)
        tok = inv.split("token=", 1)[1].split("&")[0]
        hello = ("Hi there! I'm Muse, Hrishikesh Deshpande's personal AI agent. "
                 "Looks like he shared his invite link with you. Who are you, and what would you like to talk about?")
        d_url = (f"{BASE}/v1/inboxes/{ch}/deliver?token={urllib.parse.quote(tok)}"
                 f"&body={urllib.parse.quote(hello)}&nonce={uuid.uuid4().hex}")
        st2, b2 = http("GET", d_url, timeout=30)
        mid = None
        try:
            mid = json.loads(b2).get("message_id")
        except Exception:
            pass
        if st2 == 200:
            state["channels"][ch]["last_seen"] = mid
            if mid:
                state["channels"][ch].setdefault("own_ids", []).append(mid)
        save_state(state)
        report.append(("greeted", ch, st2))
    # ack the note
    nid = note.get("id")
    del_url = f"{BASE}/v1/inboxes/{INBOX_ID}/messages/{nid}"
    st3, b3 = http("DELETE", del_url, hdrs)
    state["last_switchboard_seen"] = nid
    save_state(state)
    report.append(("ack", nid, st3))

print(json.dumps({"new_channel_notes": len(new_notes), "actions": report}, indent=1))
