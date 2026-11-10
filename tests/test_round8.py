"""Another edge pass: redirects, bad URLs, and ambiguous answers.

These stay short. The recorded 60 second soak is not repeated.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bridge.avrone_cyclekernal_link import relay_loop
from tests.test_relay_integration import _finding


class _Quiet(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return


def _serve(handler) -> tuple[ThreadingHTTPServer, threading.Thread]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread


def _stop(httpd: ThreadingHTTPServer, thread: threading.Thread) -> None:
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=3)


def test_redirect_is_not_followed_and_token_stays_put(peers, monkeypatch):
    _av, _ck = peers
    caught: list[str | None] = []

    class Catcher(_Quiet):
        def do_GET(self):  # noqa: N802
            caught.append(self.headers.get("X-Admin-Token"))
            body = b'{"posts":[{"id":"stolen000001","text":"should not arrive","kind":"finding"}],"count":1}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_POST = do_GET

    class Redirect(_Quiet):
        def do_GET(self):  # noqa: N802
            target = self.server.target + self.path  # type: ignore[attr-defined]
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_POST = do_GET

    catcher, catcher_thread = _serve(Catcher)
    redir, redir_thread = _serve(Redirect)
    redir.target = f"http://127.0.0.1:{catcher.server_address[1]}"  # type: ignore[attr-defined]
    try:
        monkeypatch.setenv("AVRONE_BASE", f"http://127.0.0.1:{redir.server_address[1]}")
        stats = relay_loop(interval=0, max_cycles=1)
    finally:
        _stop(redir, redir_thread)
        _stop(catcher, catcher_thread)
    assert caught == []
    assert stats["forwarded"] == 0
    assert stats["redirects"] >= 1
    assert stats["circuit_opened"] == 0


def test_file_url_userinfo_and_token_newline_never_leave_the_process(peers, tmp_path, monkeypatch):
    av, ck = peers
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET-FILE-CONTENTS-do-not-relay", encoding="utf-8")
    monkeypatch.setenv("AVRONE_BASE", secret.as_uri())
    file_stats = relay_loop(interval=0, max_cycles=1)
    blob = b"".join(body for _path, body in ck.bodies)
    assert b"SECRET-FILE-CONTENTS" not in blob
    assert file_stats["forwarded"] == 0
    assert file_stats["transport_errors"] >= 1

    monkeypatch.setenv("AVRONE_BASE", av.base.replace("://", "://user:secret@", 1))
    av.inject_post(_finding("user00000001", "must not dial"))
    user_stats = relay_loop(interval=0, max_cycles=1)
    assert user_stats["forwarded"] == 0
    assert not any(hit["path"] == "/api/blackboard" for hit in av.hits)

    monkeypatch.setenv("AVRONE_BASE", av.base)
    monkeypatch.setenv("FUSION_TOKEN", "good\r\nX-Injected: 1")
    av.inject_post(_finding("inject0000001", "must not send"))
    token_stats = relay_loop(interval=0, max_cycles=1)
    assert token_stats["forwarded"] == 0
    assert token_stats["auth_failures"] >= 1
    assert all("\r" not in (hit.get("token") or "") for hit in av.hits + ck.hits)
    assert not any(hit["path"] == "/v1/events" for hit in ck.hits)


def test_ambiguous_and_negative_answers_are_not_acks(peers):
    av, ck = peers
    av.inject_post(_finding("decl00000001", "declined once"))
    ck.events_script = [{"status": 200, "body": {"accepted": False, "duplicate": True, "source": "greatscale"}}]
    declined = relay_loop(interval=0, max_cycles=1)
    assert declined["forwarded"] == 0, declined
    assert ck.log.list() == []
    accepted = relay_loop(interval=0, max_cycles=1)
    assert accepted["forwarded"] == 1, accepted
    assert ck.log.list()[0]["source"] == "avrone"

    av.inject_post(_finding("dupkey000002", "ambiguous"))
    ck.events_script = [{
        "status": 200,
        "raw": '{"accepted":true,"duplicate":false,"duplicate":true,"source":"greatscale"}',
    }]
    ambiguous = relay_loop(interval=0, max_cycles=1)
    assert ambiguous["forwarded"] == 0
    assert ambiguous["malformed"] >= 1
    assert [event["id"] for event in ck.log.list()] == ["decl00000001"]
    clarified = relay_loop(interval=0, max_cycles=1)
    assert any(event["id"] == "dupkey000002" and event["source"] == "avrone" for event in ck.log.list())

    av.inject_post(_finding("http40900001", "retry me"))
    ck.events_script = [{"status": 409}]
    blocked = relay_loop(interval=0, max_cycles=1)
    assert blocked["forwarded"] == 0
    assert blocked["conflicts"] >= 1
    assert not any(event["id"] == "http40900001" for event in ck.log.list())
    retried = relay_loop(interval=0, max_cycles=1)
    assert retried["forwarded"] == 1
    assert any(event["id"] == "http40900001" for event in ck.log.list())


def test_conflict_then_500_does_not_skip_or_ack_the_rest(peers):
    av, ck = peers
    ck.legacy_bare_id = True
    with ck.lock:
        ck.log.add({
            "id": "hold00000001",
            "source": "greatscale",
            "kind": "evidence",
            "run_id": "gs",
            "payload": {"text": "kept"},
            "ts": 1.0,
        })
    ck.events_script = [{"status": 200}, {"status": 500}]
    av.inject_post(_finding("hold00000001", "avrone copy"))
    av.inject_post(_finding("midl00000001", "after the collision"))
    av.inject_post(_finding("tail00000001", "after the failure"))
    first = relay_loop(interval=0, max_cycles=1)
    assert first["conflicts"] == 1, first
    assert first["forwarded"] == 0
    assert first["upstream_errors"] >= 1
    ids = {(event["source"], event["id"]) for event in ck.log.list()}
    assert ids == {("greatscale", "hold00000001")}

    second = relay_loop(interval=0, max_cycles=1)
    assert second["conflicts"] == 1
    assert second["forwarded"] == 2, second
    stored = {(event["source"], event["id"]) for event in ck.log.list()}
    assert ("avrone", "hold00000001") not in stored
    assert ("avrone", "midl00000001") in stored
    assert ("avrone", "tail00000001") in stored


def test_repeated_id_keeps_the_first_text_and_odd_ids_stay_distinct(peers):
    av, ck = peers
    av.raw["/api/blackboard"] = json.dumps({
        "posts": [
            _finding("samepage0001", "first text"),
            _finding("samepage0001", "second text"),
        ],
        "count": 2,
        "boot": "boot-1",
    }).encode()
    once = relay_loop(interval=0, max_cycles=1)
    assert once["forwarded"] == 1, once
    stored = next(event for event in ck.log.list() if event["id"] == "samepage0001")
    assert stored["payload"]["text"] == "first text"
    av.raw.pop("/api/blackboard", None)

    long_ok = "a" * 80
    long_bad = "b" * 81
    upper = _finding("Case0000001", "upper")
    lower = _finding("case0000001", "lower")
    av.inject_post(_finding(long_ok, "fits"))
    av.inject_post(_finding(long_bad, "hashed"))
    av.inject_post(upper)
    av.inject_post(lower)
    stats = relay_loop(interval=0, max_cycles=1)
    ids = [event["id"] for event in ck.log.list()]
    assert long_ok in ids
    assert long_bad not in ids
    assert "Case0000001" in ids and "case0000001" in ids
    assert any(event_id.startswith("h-") for event_id in ids)
    assert stats["forwarded"] == 4


def test_lone_surrogate_does_not_block_the_next_post(peers):
    av, ck = peers
    bad = _finding("surr00000001", "bad\ud800tail")
    av.inject_post(bad)
    av.inject_post(_finding("surr00000002", "good"))
    stats = relay_loop(interval=0, max_cycles=1)
    assert stats["forwarded"] == 1, stats
    assert [event["id"] for event in ck.log.list() if event["source"] == "avrone"] == ["surr00000002"]
    raw = b"".join(body for path, body in ck.bodies if path == "/v1/events")
    assert b"\\ud800" not in raw and "\ud800".encode("utf-8", "replace") not in raw


def test_huge_refs_are_shrunk_under_the_cap(peers):
    av, ck = peers
    post = _finding("refs00000001", "short")
    post["refs"] = ["r" * 2000] * 30
    av.inject_post(post)
    stats = relay_loop(interval=0, max_cycles=1)
    assert stats["forwarded"] == 1, stats
    sent = [body for path, body in ck.bodies if path == "/v1/events"]
    assert sent and all(len(body) <= 4096 for body in sent)


def test_bom_blackboard_is_malformed_and_the_next_cycle_recovers(peers):
    av, ck = peers
    av.raw["/api/blackboard"] = b'\xef\xbb\xbf{"posts":[],"count":0,"boot":"boot-1"}'
    bad = relay_loop(interval=0, max_cycles=1)
    assert bad["malformed"] >= 1
    assert bad["forwarded"] == 0
    av.raw.pop("/api/blackboard", None)
    av.inject_post(_finding("afterbom0001", "after the bom"))
    recovered = relay_loop(interval=0, max_cycles=1)
    assert recovered["forwarded"] == 1, recovered
    assert any(event["id"] == "afterbom0001" for event in ck.log.list())
