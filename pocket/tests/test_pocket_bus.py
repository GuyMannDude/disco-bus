"""Pocket Bus against a stub dispatcher: auth, sender binding, listener, send
and read paths, honesty, audit, static. Spec: brain spec-pocket-bus-v0 §9."""
import email.message
import io
import json
import os
import stat
from pathlib import Path

import pytest

import pocket_bus as pb
from conftest import DOORS, LOGIN, OTHER_TOKEN, TOKEN, Pocket


# ---------------------------------------------------------------- auth (S2, S8)

def test_no_token_is_401(pocket):
    status, data, _ = pocket.api("GET", "health", token=None)
    assert status == 401 and data["auth"] == "token"


def test_wrong_token_is_401(pocket):
    assert pocket.api("GET", "health", token="pb-not-it")[0] == 401


def test_non_bearer_scheme_is_401(pocket):
    status, _, _ = pocket.api("GET", "health", token=None, headers={"Authorization": f"Basic {TOKEN}"})
    assert status == 401


def test_overlong_token_is_401(pocket):
    assert pocket.api("GET", "health", token="x" * 600)[0] == 401


def test_revoked_token_line_is_401_on_the_next_request(pocket):
    assert pocket.api("GET", "health")[0] == 200
    tokens = pocket.config_dir / "tokens.json"
    table = json.loads(tokens.read_text())
    del table["pixel"]
    tokens.write_text(json.dumps(table))
    assert pocket.api("GET", "health")[0] == 401
    assert pocket.api("GET", "health", token=OTHER_TOKEN)[0] == 200   # other device unaffected


def test_missing_token_file_is_401(pocket):
    (pocket.config_dir / "tokens.json").unlink()
    assert pocket.api("GET", "health")[0] == 401


def test_loose_token_file_permissions_fail_closed(pocket):
    os.chmod(pocket.config_dir / "tokens.json", 0o644)
    status, data, _ = pocket.api("GET", "health")
    assert status == 500 and "token file" in data["error"]


def test_right_token_absent_login_is_403(pocket):
    status, data, _ = pocket.api("GET", "health", login=None)
    assert status == 403 and data["auth"] == "login"


def test_right_token_wrong_login_is_403(pocket):
    assert pocket.api("GET", "health", login="someone-else@example.com")[0] == 403


def test_login_match_ignores_case(pocket):
    assert pocket.api("GET", "health", login=LOGIN.upper())[0] == 200


def test_right_token_and_login_is_200(pocket):
    status, data, _ = pocket.api("GET", "health")
    assert status == 200 and data["agent"] == "Guy" and data["host_label"] == "testhost"


def test_kill_switch_503s_every_api_route(pocket):
    (pocket.config_dir / "DISABLED").touch()
    for method, route, body in (("GET", "health", None), ("GET", "fleet", None), ("GET", "inbox", None),
                                ("POST", "send", {"to": ["CC"], "text": "hi"}), ("POST", "pause", {"agent": "CC"})):
        status, data, _ = pocket.api(method, route, body)
        assert status == 503 and data["disabled"] is True, route
    assert pocket.stub.paths("POST") == []


def test_kill_switch_beats_a_missing_token(pocket):
    (pocket.config_dir / "DISABLED").touch()
    assert pocket.api("GET", "health", token=None)[0] == 503


def test_kill_switch_keeps_the_listener_open(pocket):
    (pocket.config_dir / "DISABLED").touch()
    status, _, _ = pocket.raw("POST", "/inbox", {"id": 9, "to": "Guy", "from": "CC"})
    assert status == 200
    (pocket.config_dir / "DISABLED").unlink()
    _, data, _ = pocket.api("GET", "health")
    assert data["listener"]["received"] == 1 and data["listener"]["last_id"] == 9


# ---------------------------------------------------------------- sender binding (S3)

def test_client_cannot_choose_the_sender(pocket):
    status, data, _ = pocket.api("POST", "send", {"to": ["CC"], "text": "hello", "from": "CC", "agent": "Opie"})
    assert status == 200 and data["ok"]
    sent = [c[3] for c in pocket.stub.calls if c[1] == "/mesh/ping"]
    assert len(sent) == 1 and sent[0]["from"] == "Guy"


def test_send_body_is_text_via_pocket(pocket):
    pocket.api("POST", "send", {"to": ["CC"], "text": "hello"})
    env = [c[3] for c in pocket.stub.calls if c[1] == "/mesh/ping"][0]
    assert env["body"] == {"text": "hello", "via": "pocket"} and env["mesh_version"] == "0.5"


def test_send_only_to_the_fleet_so_never_to_a_sink(pocket):
    status, data, _ = pocket.api("POST", "send", {"to": ["sec-watch"], "text": "ok", "reply_to": 1})
    assert status == 400 and "sec-watch" in data["error"]
    assert "/mesh/ping" not in pocket.stub.paths()


@pytest.mark.parametrize("agent", ["sec-watch", "Guy", "nobody", None, 5])
def test_pause_play_only_fleet_agents(pocket, agent):
    for action in ("pause", "play"):
        assert pocket.api("POST", action, {"agent": agent})[0] == 400
    assert pocket.stub.paths("POST") == []


@pytest.mark.parametrize("route", ["release/1", "drop/1", "ack/1", "ack", "archive", "ping"])
def test_no_route_reaches_release_drop_ack(pocket, route):
    assert pocket.api("POST", route, {"agent": "CC"})[0] == 404
    assert not any(p.startswith(("/mesh/release", "/mesh/drop", "/mesh/archive")) for p in pocket.stub.paths())


# ---------------------------------------------------------------- the listener

def test_listener_takes_a_loopback_letter(pocket):
    status, data, _ = pocket.raw("POST", "/inbox", {"id": 4, "to": "Guy", "from": "CC", "body": {"text": "x"}})
    assert status == 200 and data == {"ok": True}
    assert pocket.app.listener["received"] == 1


@pytest.mark.parametrize("header", ["X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "Forwarded",
                                    "Tailscale-User-Login", "Tailscale-User-Name"])
def test_listener_refuses_proxied_requests(pocket, header):
    status, _, _ = pocket.raw("POST", "/inbox", {"id": 4, "to": "Guy"}, headers={header: "192.0.2.7"})
    assert status == 403 and pocket.app.listener["received"] == 0


def test_listener_refuses_a_non_loopback_source(pocket):
    h = pb.Handler.__new__(pb.Handler)
    h.server, h.client_address = pocket.server, ("192.0.2.9", 51515)
    h.command, h.request_version, h.requestline, h.path = "POST", "HTTP/1.0", "POST /inbox HTTP/1.0", "/inbox"
    h.headers = email.message.Message()
    body = json.dumps({"id": 1, "to": "Guy"}).encode()
    h.headers["Content-Length"] = str(len(body))
    h.rfile, h.wfile = io.BytesIO(body), io.BytesIO()
    h._route("POST")
    assert h.wfile.getvalue().startswith(b"HTTP/1.0 403") and pocket.app.listener["received"] == 0


def test_listener_caps_the_body(pocket):
    status, _, _ = pocket.raw("POST", "/inbox", data=b"x" * (pb.INBOX_MAX_BYTES + 1))
    assert status == 413


def test_listener_takes_a_full_size_letter(pocket):
    big = {"id": 5, "to": "Guy", "from": "CC", "subject": "s", "body": {"text": "y" * (1024 * 1024 - 20)}}
    assert pocket.raw("POST", "/inbox", big)[0] == 200


def test_listener_only_takes_letters_to_the_owner(pocket):
    assert pocket.raw("POST", "/inbox", {"id": 5, "to": "CC"})[0] == 400


def test_listener_is_post_only(pocket):
    assert pocket.raw("GET", "/inbox")[0] == 405


# ---------------------------------------------------------------- send path

@pytest.mark.parametrize("text,subject", [
    ("Can you check the printer tonight?", "can-you-check-the-printer-tonight"),
    ("  Café über straße — ok?  ", "café-über-straße-ok"),
    ("first line here\nsecond line ignored", "first-line-here"),
    ("🎸🎸🎸", "note-from-guy"),
    ("snake_case_word and more", "snake-case-word-and-more"),
])
def test_subject_derivation(text, subject):
    assert pb.derive_subject(text) == subject


def test_subject_from_a_200_char_first_line_is_whole_words_under_60():
    s = pb.derive_subject(" ".join(["guitar"] * 40))
    assert len(s) <= 60 and s == "-".join(["guitar"] * 8)


def test_subject_from_one_giant_word_is_cut_to_60():
    assert pb.derive_subject("a" * 200) == "a" * 60


def test_blank_subject_is_derived_and_a_given_one_is_kept(pocket):
    pocket.api("POST", "send", {"to": ["CC"], "text": "Hello from the porch", "subject": "   "})
    pocket.api("POST", "send", {"to": ["CC"], "text": "x", "subject": "  amp   is humming "})
    subjects = [c[3]["subject"] for c in pocket.stub.calls if c[1] == "/mesh/ping"]
    assert subjects == ["hello-from-the-porch", "amp is humming"]


def test_multi_recipient_is_one_letter_each_with_its_own_receipt(pocket):
    status, data, _ = pocket.api("POST", "send", {"to": ["CC", "Opie", "CC3"], "text": "band meeting at 7"})
    assert status == 200 and data["ok"]
    assert [r["to"] for r in data["receipts"]] == ["CC", "Opie", "CC3"]
    ids = [r["id"] for r in data["receipts"]]
    assert len(set(ids)) == 3 and all(r["state"] == "SENT" for r in data["receipts"])
    assert [pocket.stub.by_id(i)["to"] for i in ids] == ["CC", "Opie", "CC3"]


def test_a_recipient_tapped_twice_gets_one_letter(pocket):
    _, data, _ = pocket.api("POST", "send", {"to": ["CC", "CC"], "text": "once"})
    assert len(data["receipts"]) == 1


def test_to_may_be_a_single_name(pocket):
    _, data, _ = pocket.api("POST", "send", {"to": "Opie", "text": "hi"})
    assert data["receipts"][0]["to"] == "Opie"


def test_reply_to_is_passed_through(pocket):
    parent = pocket.stub.add("CC", "Guy", "question")
    pocket.api("POST", "send", {"to": ["CC"], "text": "yes", "reply_to": parent})
    assert [c[3] for c in pocket.stub.calls if c[1] == "/mesh/ping"][0]["reply_to"] == parent


@pytest.mark.parametrize("bad", [True, 0, -3, "12", 1.5])
def test_bad_reply_to_is_refused(pocket, bad):
    assert pocket.api("POST", "send", {"to": ["CC"], "text": "x", "reply_to": bad})[0] == 400


@pytest.mark.parametrize("body", [{"to": ["CC"], "text": "   "}, {"to": ["CC"]}, {"to": [], "text": "x"},
                                  {"text": "x"}, {"to": [3], "text": "x"}, {"to": ["CC"], "text": "x", "subject": 7}])
def test_malformed_sends_are_refused(pocket, body):
    assert pocket.api("POST", "send", body)[0] == 400
    assert "/mesh/ping" not in pocket.stub.paths()


def test_text_over_16kb_is_413(pocket):
    status, data, _ = pocket.api("POST", "send", {"to": ["CC"], "text": "é" * 8193})
    assert status == 413 and "16 KB" in data["error"]


def test_request_body_over_cap_is_413(pocket):
    status, _, _ = pocket.api("POST", "send", {"to": ["CC"], "text": "x" * (pb.API_BODY_MAX_BYTES + 10)})
    assert status == 413


def test_rate_limit_trips_at_31(pocket):
    for i in range(30):
        assert pocket.api("POST", "send", {"to": ["CC"], "text": f"n{i}"})[0] == 200
    status, data, _ = pocket.api("POST", "send", {"to": ["CC"], "text": "one too many"})
    assert status == 429 and "10 minutes" in data["error"]
    assert len([p for p in pocket.stub.paths() if p == "/mesh/ping"]) == 30


def test_rate_limit_refuses_a_whole_batch_that_would_cross_the_line(pocket):
    for i in range(28):
        pocket.api("POST", "send", {"to": ["CC"], "text": f"n{i}"})
    assert pocket.api("POST", "send", {"to": ["CC", "Opie", "CC3"], "text": "batch"})[0] == 429
    assert len([p for p in pocket.stub.paths() if p == "/mesh/ping"]) == 28   # none of the 3 went


def test_rate_limit_is_per_token(pocket):
    for i in range(30):
        pocket.api("POST", "send", {"to": ["CC"], "text": f"n{i}"})
    assert pocket.api("POST", "send", {"to": ["CC"], "text": "tablet"}, token=OTHER_TOKEN)[0] == 200


def test_held_receipt_when_the_owner_is_paused(pocket):
    pocket.stub.pauses["Guy"] = "2026-10-23T21:00:00Z"
    _, data, _ = pocket.api("POST", "send", {"to": ["CC"], "text": "x"})
    assert data["receipts"][0]["state"] == "HELD" and data["ok"]


def test_dispatcher_refusal_is_a_receipt_not_a_crash(pocket):
    pocket.stub.registry.discard("CC4")
    _, data, _ = pocket.api("POST", "send", {"to": ["CC", "CC4"], "text": "x"})
    assert data["ok"] is False
    assert data["receipts"][0]["ok"] and not data["receipts"][1]["ok"]
    assert "CC4" in data["receipts"][1]["error"]


def test_dispatcher_down_during_send_is_reported_and_audited(pocket):
    pocket.stub.close()
    status, data, _ = pocket.api("POST", "send", {"to": ["CC"], "text": "x"})
    assert status == 200 and data["ok"] is False
    assert "Can't reach" in data["receipts"][0]["error"]
    assert pocket.audit_lines()[-1]["result"].startswith("error:")


# ---------------------------------------------------------------- read path

def test_inbox_all_and_unread(pocket):
    a = pocket.stub.add("CC", "Guy", "one", read_at="2026-10-23T20:05:00Z")
    b = pocket.stub.add("CC", "Guy", "two")
    pocket.stub.add("CC", "Opie", "not mine")
    _, all_, _ = pocket.api("GET", "inbox?filter=all")
    _, unread, _ = pocket.api("GET", "inbox?filter=unread")
    assert [m["id"] for m in all_["letters"]] == [b, a] and all_["total"] == 2
    assert [m["id"] for m in unread["letters"]] == [b]


def test_inbox_filter_must_be_known(pocket):
    assert pocket.api("GET", "inbox?filter=unreplied")[0] == 400


def test_single_door_tags_only_unsolicited_letters_from_others(pocket):
    s = pocket.stub
    mine = s.add("Guy", "Opie", "ask")
    reply = s.add("Opie", "Guy", "answer", reply_to=mine)
    follow = s.add("Opie", "Guy", "and also", reply_to=reply)          # parent is a letter to Guy
    cc_first = s.add("CC", "Guy", "cc writes first")
    cold = s.add("CC3", "Guy", "linus writes first")
    sideways = s.add("CC", "CC3", "between agents")
    hijack = s.add("CC3", "Guy", "replying to a letter Guy was never in", reply_to=sideways)
    _, data, _ = pocket.api("GET", "inbox")
    tag = {m["id"]: m["outside_door"] for m in data["letters"]}
    assert tag == {reply: False, follow: False, cc_first: False, cold: True, hijack: True}


def test_inbox_when_owner_not_registered(stub, tmp_path):
    stub.registry.discard("Guy")
    p = Pocket(tmp_path, stub)
    try:
        status, data, _ = p.api("GET", "inbox")
        assert status == 409 and data["not_registered"] is True
        _, health, _ = p.api("GET", "health")
        assert health["registered"] is False and health["ok"] is False
    finally:
        p.close()


def test_read_marks_the_owners_letter(pocket):
    mid = pocket.stub.add("CC", "Guy", "hi")
    status, data, _ = pocket.api("POST", f"read/{mid}", {})
    assert status == 200 and data["first_read"] is True
    assert pocket.stub.by_id(mid)["read_at"]
    assert [c[3] for c in pocket.stub.calls if c[1] == f"/mesh/read/{mid}"] == [{"agent": "Guy"}]


def test_read_of_someone_elses_letter_is_a_plain_403(pocket):
    mid = pocket.stub.add("CC", "Opie", "theirs")
    status, data, _ = pocket.api("POST", f"read/{mid}", {})
    assert status == 403 and "isn't addressed to you" in data["error"]
    assert pocket.stub.by_id(mid)["read_at"] is None


@pytest.mark.parametrize("route,status", [("read/999", 404), ("read/abc", 400), ("read/0", 400)])
def test_read_bad_ids(pocket, route, status):
    assert pocket.api("POST", route, {})[0] == status


def test_thread_is_chronological_with_mine_flag_and_readable_text(pocket):
    s = pocket.stub
    root = s.add("Guy", "CC", "plan", body={"text": "what's the plan?", "via": "pocket"})
    r1 = s.add("CC", "Guy", "re", body={"status": "ok", "not_done": ["tests", "deploy"]}, reply_to=root)
    status, data, _ = pocket.api("GET", f"thread/{r1}")
    assert status == 200 and data["root_id"] == root and data["focus"] == r1
    assert [(m["id"], m["mine"]) for m in data["messages"]] == [(root, True), (r1, False)]
    assert data["messages"][1]["text"] == "status: ok\nnot done:\n  - tests\n  - deploy"
    assert data["messages"][1]["body"]["status"] == "ok"           # raw body kept for Details


def test_thread_unknown_is_404(pocket):
    assert pocket.api("GET", "thread/77")[0] == 404


def test_search_uses_q(pocket):
    pocket.stub.add("CC", "Guy", "amp repair", body={"text": "the tube amp"})
    pocket.stub.add("CC", "Guy", "other")
    status, data, _ = pocket.api("GET", "search?q=tube")
    assert status == 200 and [m["subject"] for m in data["letters"]] == ["amp repair"] and data["total"] == 1
    calls = [c[2] for c in pocket.stub.calls if c[1] == "/mesh/search"]
    assert calls == [{"q": "tube", "limit": "50"}]


def test_search_needs_words(pocket):
    assert pocket.api("GET", "search?q=%20")[0] == 400


def test_traffic_limit_is_capped_and_checked(pocket):
    pocket.api("GET", "traffic?limit=9999")
    assert [c[2] for c in pocket.stub.calls if c[1] == "/mesh/history"] == [{"limit": "200"}]
    assert pocket.api("GET", "traffic?limit=lots")[0] == 400


def test_agent_view_merges_both_directions(pocket):
    s = pocket.stub
    a = s.add("Guy", "Opie", "hi opie")
    b = s.add("Opie", "Guy", "hi guy", reply_to=a)
    s.add("CC", "Guy", "not opie")
    status, data, _ = pocket.api("GET", "agent/Opie")
    assert status == 200 and [m["id"] for m in data["letters"]] == [b, a] and data["label"] == "Opie"


def test_agent_view_only_for_the_fleet(pocket):
    assert pocket.api("GET", "agent/sec-watch")[0] == 404


# ---------------------------------------------------------------- rendering (server side)

def test_display_text_prefers_body_text():
    assert pb.display_text({"text": "hello", "via": "pocket"}) == "hello"


def test_display_text_flattens_a_json_only_body():
    body = {"status": "ORDER-COMPLETE", "commit": "abc123", "checks": {"tests": 84, "red": 0},
            "ok": True, "gaps": [], "files": ["a.py", {"name": "b.py"}]}
    assert pb.display_text(body) == (
        "status: ORDER-COMPLETE\ncommit: abc123\nchecks:\n  tests: 84\n  red: 0\nok: yes\ngaps: (none)\n"
        "files:\n  - a.py\n  -\n    name: b.py")


def test_display_text_odd_bodies():
    assert pb.display_text({}) == "(empty letter)"
    assert pb.display_text({"text": "   "}) == "text:    "
    assert pb.display_text(["x", "y"]) == "- x\n- y"
    assert pb.display_text(None) == "(none)"


def test_preview_is_one_line_and_short():
    m = pb.slim({"id": 1, "body": {"text": "line one\n\nline two   " + "z" * 500}})
    assert "\n" not in m["preview"] and len(m["preview"]) == pb.PREVIEW_CHARS and "text" not in m


def test_hostile_text_passes_through_as_data(pocket):
    evil = '<img src=x onerror=alert(1)><script>alert(2)</script>'
    mid = pocket.stub.add("CC", "Guy", evil, body={"text": evil})
    _, data, _ = pocket.api("GET", f"thread/{mid}")
    assert data["messages"][0]["text"] == evil                     # unchanged; the app renders text only


# ---------------------------------------------------------------- honesty

def test_dispatcher_down_health_says_so(pocket):
    pocket.stub.close()
    status, data, _ = pocket.api("GET", "health")
    assert status == 200 and data["ok"] is False and data["dispatcher"]["ok"] is False


def test_dispatcher_down_lists_are_502_with_a_flag(pocket):
    pocket.stub.close()
    status, data, _ = pocket.api("GET", "inbox")
    assert status == 502 and data["dispatcher_down"] is True


def test_health_counts_unread(pocket):
    pocket.stub.add("CC", "Guy", "a")
    pocket.stub.add("Opie", "Guy", "b")
    assert pocket.api("GET", "health")[1]["unread"] == 2


def test_fleet_cards(pocket):
    s = pocket.stub
    s.add("CC", "Guy", "old", created_at="2026-10-23T18:00:00Z")
    s.add("CC", "Opie", "newest from CC", created_at="2026-10-23T19:00:00Z")
    s.add("CC", "Guy", "read", read_at="2026-10-23T19:30:00Z")
    _, data, _ = pocket.api("GET", "fleet")
    cards = {c["agent"]: c for c in data["agents"]}
    assert list(cards) == list(DOORS)                                # file order
    assert cards["CC"]["last_heard"] == "2026-10-23T20:00:00Z" and cards["CC"]["unread"] == 1
    assert cards["Opie"]["last_heard"] is None and cards["Opie"]["on_bus"] is True
    assert cards["CC4"]["on_bus"] is False                           # not in the registry
    assert cards["CC3"]["label"] == "Linus" and cards["CC3"]["door"]["source"] == "doors"


def test_door_claim_without_a_policy_file_is_the_doors_value(pocket):
    doors = dict(DOORS, CC={**DOORS["CC"], "wakes": True})
    (pocket.config_dir / "doors.json").write_text(json.dumps(doors))
    _, data, _ = pocket.api("GET", "fleet")
    assert data["agents"][0]["door"] == {"wakes": True, "how": DOORS["CC"]["how"], "source": "doors"}


def test_policy_wins_over_a_doors_claim(pocket):
    doors = dict(DOORS, CC={**DOORS["CC"], "wakes": True})
    (pocket.config_dir / "doors.json").write_text(json.dumps(doors))
    pocket.cfg.wake_policy.write_text(json.dumps({"agents": {"CC": {"wakes": False}, "Opie": {"wakes": True}}}))
    _, data, _ = pocket.api("GET", "fleet")
    cards = {c["agent"]: c["door"] for c in data["agents"]}
    assert cards["CC"]["wakes"] is False and "wake policy says it does not" in cards["CC"]["note"]
    assert cards["Opie"] == {"wakes": True, "how": DOORS["Opie"]["how"], "source": "wake-policy"}
    assert cards["CC3"]["wakes"] is False                            # not in the policy = no wake


def test_unreadable_policy_claims_no_wake(pocket):
    doors = dict(DOORS, CC={**DOORS["CC"], "wakes": True})
    (pocket.config_dir / "doors.json").write_text(json.dumps(doors))
    pocket.cfg.wake_policy.write_text("{not json")
    _, data, _ = pocket.api("GET", "fleet")
    assert data["agents"][0]["door"]["wakes"] is False and "Can't read" in data["agents"][0]["door"]["note"]


def test_missing_doors_file_is_said_not_hidden(pocket):
    (pocket.config_dir / "doors.json").unlink()
    _, fleet, _ = pocket.api("GET", "fleet")
    _, health, _ = pocket.api("GET", "health")
    assert fleet["agents"] == [] and "no doors file" in fleet["doors_error"]
    assert "no doors file" in health["doors_error"]


def test_holds_show_paused_agents_and_held_counts(pocket):
    pocket.api("POST", "pause", {"agent": "CC"})
    pocket.stub.add("CC", "Opie", "waiting", state="HELD")
    _, data, _ = pocket.api("GET", "holds")
    rows = {a["agent"]: a for a in data["agents"]}
    assert rows["CC"]["paused"] and rows["CC"]["held"] == 1 and not rows["Opie"]["paused"]
    _, played, _ = pocket.api("POST", "play", {"agent": "CC"})
    assert played == {"agent": "CC", "paused": False, "released": 1}


# ---------------------------------------------------------------- audit (S6)

def test_one_audit_line_per_write_with_device_and_no_bodies(pocket):
    mid = pocket.stub.add("CC", "Guy", "hi")
    pocket.api("POST", "send", {"to": ["CC", "Opie"], "text": "SECRET-WORDS in the body"})
    pocket.api("POST", f"read/{mid}", {})
    pocket.api("POST", "pause", {"agent": "CC"})
    pocket.api("POST", "play", {"agent": "CC"}, token=OTHER_TOKEN)
    pocket.api("GET", "inbox")                                         # reads are not writes
    lines = pocket.audit_lines()
    assert [(x["route"], x["token"]) for x in lines] == [
        ("send", "pixel"), ("send", "pixel"), ("read", "pixel"), ("pause", "pixel"), ("play", "tablet")]
    assert [x.get("to") for x in lines[:2]] == ["CC", "Opie"] and all(x["id"] for x in lines[:2])
    assert "SECRET-WORDS" not in pocket.cfg.audit_file.read_text()
    assert stat.S_IMODE(pocket.cfg.audit_file.stat().st_mode) == 0o600


def test_audit_rotates_and_never_deletes(pocket):
    pocket.app.audit_max = 300
    for i in range(8):
        pocket.api("POST", "send", {"to": ["CC"], "text": f"n{i}"})
    files = sorted(Path(pocket.cfg.state_dir).glob("audit*.jsonl"))
    rotated = [f for f in files if f.name != "audit.jsonl"]
    assert rotated and pocket.cfg.audit_file.exists()
    total = sum(len(f.read_text().splitlines()) for f in files)
    assert total == 8


# ---------------------------------------------------------------- static (S4, S5)

@pytest.mark.parametrize("path,ctype", [
    ("/app/", "text/html"), ("/app/app.js", "text/javascript"), ("/app/app.css", "text/css"),
    ("/app/sw.js", "text/javascript"), ("/app/manifest.webmanifest", "application/manifest+json"),
    ("/app/icon.svg", "image/svg+xml"), ("/app/icon-192.png", "image/png"), ("/app/icon-512.png", "image/png"),
])
def test_allowlisted_static_files_are_served_without_a_token(pocket, path, ctype):
    status, _, headers = pocket.raw("GET", path)
    assert status == 200 and headers["Content-Type"].startswith(ctype)
    assert headers["Content-Security-Policy"] == pb.CSP


@pytest.mark.parametrize("path", ["/app/../pocket_bus.py", "/app/%2e%2e/pocket_bus.py", "/app/index.html",
                                  "/app/tests/conftest.py", "/pocket_bus.py", "/app/static/app.js",
                                  "/app//etc/passwd", "/static/app.js"])
def test_nothing_outside_the_allowlist(pocket, path):
    assert pocket.raw("GET", path)[0] == 404


def test_root_redirects_to_the_app(pocket):
    status, _, headers = pocket.raw("GET", "/")
    assert status == 302 and headers["Location"] == "/app/"


def test_security_headers_on_api_and_static(pocket):
    for _, _, headers in (pocket.raw("GET", "/app/"), pocket.api("GET", "health")):
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY" and headers["Referrer-Policy"] == "no-referrer"
        assert "default-src 'self'" in headers["Content-Security-Policy"]
    assert pocket.api("GET", "health")[2]["Cache-Control"] == "no-store"


def test_csp_forbids_inline_script():
    assert "'unsafe-inline'" not in pb.CSP and "script-src 'self'" in pb.CSP


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH"])
def test_other_methods_are_405(pocket, method):
    assert pocket.raw(method, "/api/health")[0] == 405


def test_static_files_hold_no_secrets():
    for f in pb.STATIC_DIR.iterdir():
        if f.suffix in (".png",):
            continue
        text = f.read_text()
        assert "@" not in text.replace("@media", "") and "Bearer pb-" not in text, f.name


# ---------------------------------------------------------------- config + mint

def test_config_requires_the_dispatcher_url_and_login():
    with pytest.raises(pb.ConfigError, match="POCKET_DISCOBUS_URL"):
        pb.config_from_env({"POCKET_TAILSCALE_LOGIN": LOGIN})
    with pytest.raises(pb.ConfigError, match="POCKET_TAILSCALE_LOGIN"):
        pb.config_from_env({"POCKET_DISCOBUS_URL": "http://127.0.0.1:9100"})
    with pytest.raises(pb.ConfigError, match="http"):
        pb.config_from_env({"POCKET_DISCOBUS_URL": "ftp://x", "POCKET_TAILSCALE_LOGIN": LOGIN})


def test_config_defaults():
    cfg = pb.config_from_env({"POCKET_DISCOBUS_URL": "http://203.0.113.2:9100/", "POCKET_TAILSCALE_LOGIN": LOGIN})
    assert (cfg.dispatcher, cfg.agent, cfg.door_agent, cfg.port) == ("http://203.0.113.2:9100", "Guy", "CC", 9101)


def test_binds_loopback_only(pocket):
    assert pocket.server.server_address[0] == "127.0.0.1"


def test_main_refuses_to_start_unconfigured(monkeypatch):
    monkeypatch.delenv("POCKET_DISCOBUS_URL", raising=False)
    assert pb.main([]) == pb.EXIT_CONFIG == 78


def test_mint_stores_a_hash_only(tmp_path):
    token = pb.mint("pixel", tmp_path / "cfg")
    path = tmp_path / "cfg" / "tokens.json"
    table = json.loads(path.read_text())
    assert token.startswith("pb-") and token not in path.read_text()
    assert table == {"pixel": pb.hash_token(token)}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE((tmp_path / "cfg").stat().st_mode) == 0o700
    with pytest.raises(pb.ConfigError, match="already exists"):
        pb.mint("pixel", tmp_path / "cfg")
    with pytest.raises(pb.ConfigError):
        pb.mint("../evil", tmp_path / "cfg")


def test_a_minted_token_opens_the_api(pocket):
    token = pb.mint("phone2", pocket.config_dir)
    assert pocket.api("GET", "health", token=token)[0] == 200


# -- shipped examples ----------------------------------------------------------

POCKET_DIR = Path(__file__).resolve().parent.parent


def _env_file(path: Path) -> dict:
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            k, _, v = line.partition("=")
            out[k] = v
    return out


def test_env_example_names_both_required_settings_and_is_refused_unfilled():
    env = _env_file(POCKET_DIR / "env.example")
    assert set(env) == {"POCKET_DISCOBUS_URL", "POCKET_TAILSCALE_LOGIN"}
    with pytest.raises(pb.ConfigError, match="placeholder"):
        pb.config_from_env(env)
    filled = dict(env, POCKET_DISCOBUS_URL="http://203.0.113.2:9100")
    with pytest.raises(pb.ConfigError, match="POCKET_TAILSCALE_LOGIN is still"):
        pb.config_from_env(filled)


def test_doors_example_loads(tmp_path):
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "doors.json").write_text((POCKET_DIR / "doors.example.json").read_text())
    app = pb.App(pb.Config(dispatcher="http://127.0.0.1:1", login=LOGIN, config_dir=cfg_dir))
    doors, err = app.doors()
    assert err is None and len(doors) == 3 and doors["Helper"]["wakes"] is True


def test_unit_file_requires_the_env_file_and_does_not_spin_on_bad_config():
    unit = (POCKET_DIR / "systemd" / "pocket-bus.service").read_text()
    assert "EnvironmentFile=%h/.config/pocket-bus/env" in unit      # no "-": required
    assert f"RestartPreventExitStatus={pb.EXIT_CONFIG}" in unit
    assert "ExecStart=/usr/bin/python3 %h/github/disco-bus/pocket/pocket_bus.py" in unit


# -- review fixes (crafted letters and requests degrade, never drop the page) ----

def _deep(n):
    v = {"leaf": "x"}
    for _ in range(n):
        v = {"k": v}
    return v


def test_display_text_survives_a_deeply_nested_body():
    text = pb.display_text(_deep(5000))
    assert text.splitlines()[pb.FLATTEN_MAX_DEPTH].strip() == "…"
    assert len(text.splitlines()) == pb.FLATTEN_MAX_DEPTH + 1


def test_a_deeply_nested_letter_does_not_break_inbox_traffic_or_thread(pocket, stub):
    mid = stub.add("Opie", "Guy", "deep", body=_deep(900))
    for route in ("inbox?filter=unread", "traffic", f"thread/{mid}", "agent/Opie"):
        status, data, _ = pocket.api("GET", route)
        assert status == 200, (route, data)
    assert "…" in pocket.api("GET", f"thread/{mid}")[1]["messages"][0]["text"]


def test_an_unexpected_error_is_a_plain_500_not_a_dropped_connection(pocket, monkeypatch):
    def boom(*a, **k):
        raise KeyError("surprise")
    monkeypatch.setattr(pb.Handler, "_traffic", boom)
    status, data, _ = pocket.api("GET", "traffic")
    assert status == 500 and data["server_error"] is True and "surprise" not in json.dumps(data)


def _raw_request_line(pocket, path: bytes) -> bytes:
    import socket
    with socket.create_connection(("127.0.0.1", pocket.server.server_port), timeout=5) as sock:
        sock.sendall(b"GET " + path + b" HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer " + TOKEN.encode()
                     + b"\r\nTailscale-User-Login: " + LOGIN.encode() + b"\r\nConnection: close\r\n\r\n")
        out = b""
        while chunk := sock.recv(65536):
            out += chunk
    return out


@pytest.mark.parametrize("raw_id", [b"\xb2", b"1" * 19, b"1" * 5000], ids=["superscript", "19-digits", "5000-digits"])
def test_odd_letter_ids_are_a_400(pocket, raw_id):
    assert _raw_request_line(pocket, b"/api/thread/" + raw_id).startswith(b"HTTP/1.0 400")


def test_a_deeply_nested_request_body_is_a_400(pocket):
    status, _, _ = pocket.raw("POST", "/api/send", data=b"[" * 200000 + b"]" * 200000,
                              headers={"Authorization": f"Bearer {TOKEN}", "Tailscale-User-Login": LOGIN})
    assert status in (400, 413)
    status, _, _ = pocket.raw("POST", "/api/send", data=b"[" * 60000 + b"]" * 60000,
                              headers={"Authorization": f"Bearer {TOKEN}", "Tailscale-User-Login": LOGIN})
    assert status == 400


def test_listener_refuses_a_web_page_and_non_json(pocket):
    letter = json.dumps({"id": 4, "to": "Guy"}).encode()
    assert pocket.raw("POST", "/inbox", {"id": 4, "to": "Guy"}, headers={"Origin": "http://evil.example"})[0] == 403
    assert pocket.raw("POST", "/inbox", data=None, headers={"Content-Type": "text/plain"})[0] == 403
    import urllib.error
    import urllib.request as ur
    req = ur.Request(pocket.base + "/inbox", data=letter, method="POST", headers={"Content-Type": "text/plain"})
    try:
        ur.build_opener(ur.ProxyHandler({})).open(req, timeout=5)
        status = 200
    except urllib.error.HTTPError as e:
        status = e.code
    assert status == 403 and pocket.app.listener["received"] == 0


def test_listener_keeps_only_an_integer_last_id(pocket):
    assert pocket.raw("POST", "/inbox", {"id": "x" * 1000, "to": "Guy"})[0] == 200
    assert pocket.app.listener["last_id"] is None and pocket.app.listener["received"] == 1


def test_a_parent_that_does_not_exist_yet_is_not_cached(pocket, stub):
    future = len(stub.msgs) + 2
    early = stub.add("Opie", "Guy", "early reply", reply_to=future)
    tag = lambda: {m["id"]: m["outside_door"] for m in pocket.api("GET", "inbox")[1]["letters"]}[early]
    assert tag() is True
    assert stub.add("Guy", "Opie", "the letter it answers") == future
    assert tag() is False


def test_fleet_says_when_unread_counts_are_partial(pocket, stub):
    for i in range(pb.UNREAD_COUNT_PAGE + 1):
        stub.add("CC", "Guy", f"n{i}")
    data = pocket.api("GET", "fleet")[1]
    assert data["unread_partial"] == pb.UNREAD_COUNT_PAGE
    stub.msgs.clear()
    assert "unread_partial" not in pocket.api("GET", "fleet")[1]


def test_an_oversized_dispatcher_answer_is_a_502(pocket, stub, monkeypatch):
    stub.add("CC", "Guy", "s", {"text": "y" * 5000})
    monkeypatch.setattr(pb, "DISPATCHER_READ_MAX", 1000)
    status, data, _ = pocket.api("GET", "traffic")
    assert status == 502 and "more than" in data["error"]


def test_health_stays_json_when_the_dispatcher_dies_between_calls(pocket, monkeypatch):
    real = pb.App.dispatch

    def flaky(self, method, path, *a, **k):
        if path.startswith("/mesh/inbox/"):
            raise pb.DispatcherDown("gone")
        return real(self, method, path, *a, **k)
    monkeypatch.setattr(pb.App, "dispatch", flaky)
    status, data, _ = pocket.api("GET", "health")
    assert status == 200 and data["ok"] is False and data["dispatcher"] == {"ok": False, "error": "gone"}
