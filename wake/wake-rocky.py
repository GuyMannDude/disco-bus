#!/usr/bin/env python3
"""WAKE exec for Rocky (Hermes) — disco-bus DISCOBUS_AUTO_REPLY contract.

stdin : one bus envelope (JSON).  stdout: one JSON reply body.
Door  : Hermes HUD backend, POST /api/chat/sessions -> POST /api/chat/sessions/{id}/message (SSE).
Policy: wake-policy.json beside this file (budget/allowlist) — v0 = fixed constants below.
Spec  : brain spec-wake-v0.md  (S425, 2026-10-16).  No Rocky config is touched: this is
        the same call the HUD page makes when Guy types.
"""
import json, sys, time, urllib.request, urllib.error

HUD = "http://127.0.0.1:3001/api"
TIMEOUT_S = 170          # listener default is 120; raise DISCOBUS_AUTO_REPLY_TIMEOUT to 180 when wiring
MAX_PROMPT = 4000        # a letter is a prompt, not a transcript
PREFIX = ("[WAKE via disco-bus] A letter arrived for you. Reply in plain English, briefly, "
          "as yourself. What you say here IS your bus reply and is posted for you: do NOT call "
          "the bus ping tool yourself, or the sender gets your answer twice. "
          "Sender: {sender}. Subject: {subject}.\n\n")


def fail(msg, **extra):
    print(json.dumps({"error": msg, **extra}))
    sys.exit(0)  # the listener posts stdout either way; a non-zero exit adds nothing


def body_text(env):
    b = env.get("body")
    if isinstance(b, dict):
        return b.get("text") or b.get("body") or json.dumps(b)
    return "" if b is None else str(b)


def post(path, payload, stream=False):
    req = urllib.request.Request(HUD + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=TIMEOUT_S)


def main():
    t0 = time.time()
    try:
        env = json.load(sys.stdin)
    except Exception as e:
        fail(f"bad envelope: {e}")
    if env.get("reply_to"):
        fail("letter is a reply; wake skipped (loop guard)")
    sender, subject = env.get("from", "?"), env.get("subject", "")
    text = body_text(env).strip()
    if not text:
        fail("empty body")
    prompt = (PREFIX.format(sender=sender, subject=subject) + text)[:MAX_PROMPT]

    try:
        with post("/chat/sessions", {"profile": None, "model": None}) as r:
            sid = json.load(r)["id"]
    except urllib.error.HTTPError as e:
        fail(f"hud create session HTTP {e.code}", detail=e.read().decode()[:300])
    except Exception as e:
        fail(f"hud create session: {e}")

    msgs = {"messages": [{"role": "user", "parts": [{"type": "text", "text": prompt}]}]}
    out, finished = [], False
    try:
        with post(f"/chat/sessions/{sid}/message", msgs, stream=True) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    finished = True
                    break
                try:
                    ev = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "text-delta":
                    out.append(ev.get("delta", ""))
                elif ev.get("type") == "finish":
                    finished = True
    except urllib.error.HTTPError as e:
        fail(f"hud message HTTP {e.code}", detail=e.read().decode()[:300], session_id=sid)
    except Exception as e:
        fail(f"hud stream: {e}", session_id=sid, partial="".join(out)[:500])

    reply = "".join(out).strip()
    print(json.dumps({
        "text": reply if reply else "(Rocky returned no text)",
        "wake": {"agent": "Rocky", "door": "hud-message-sse", "session_id": sid,
                 "finished": finished, "ms": int((time.time() - t0) * 1000),
                 "in_reply_to": env.get("id")},
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
