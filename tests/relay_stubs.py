"""Reference servers for the fusion contract.

These are the authenticated, size-capped peers ``relay_loop`` is tested
against. They are not Avrone and they are not CycleKernal. They implement
only the routes ``CONTRACT.md`` says those repos must add, plus the two reads
that already exist (``/api/status``, ``/v1/current_mood``, ``/health``).
"""
from __future__ import annotations

import hmac
import json
import socket
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
from urllib.parse import parse_qs, urlparse

from bridge.contract import MAX_BODY_BYTES, Board, EventLog, normalize_event, normalize_post


class ContractStub:
    def __init__(self, kind: str, token: str, max_body: int = MAX_BODY_BYTES):
        if kind not in ("avrone", "ck"):
            raise ValueError("kind must be avrone or ck")
        if not token:
            raise ValueError("the reference stub always requires a token")
        self.kind = kind
        self.token = token
        self.max_body = max_body
        self.lock = threading.Lock()
        self.board = Board()
        self.log = EventLog()
        self.mood = {
            "mood": "convergent",
            "traits": ["Analytical/Convergent"],
            "mood_dot": 0.72,
            "ns_fitness": 0.61,
            "genome_id": "gn-stub",
            "promote": {"action": "hold"},
        }
        self.health = {"status": "ghost-online", "version": "3.1.0"}
        # Stable until the test simulates a process restart.
        self.boot = "boot-1"
        self.fail: dict[str, int] = {}
        self.delay: dict[str, float] = {}
        self.raw: dict[str, bytes] = {}
        self.accepting = True
        self.hits: list[dict] = []
        self.bodies: list[tuple[str, bytes]] = []
        self.events_script: list[dict] = []
        self.legacy_bare_id = False
        self.port = 0
        self.base = ""
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def inject_post(self, post: dict) -> None:
        """Put a post on the board without the write-path clip. Simulates a peer that mis-sized a row."""
        with self.lock:
            pid = str(post.get("id") or "")
            if not pid:
                raise ValueError("injected post needs an id")
            self.board.posts[pid] = post

    def start(self, host: str = "127.0.0.1") -> "ContractStub":
        port = self.port or 0
        if ":" in host:
            class _V6(_Server):
                address_family = socket.AF_INET6
            server_cls = _V6
        else:
            server_cls = _Server
        httpd = server_cls((host, port), _Handler)
        httpd.stub = self  # type: ignore[attr-defined]
        self._httpd = httpd
        self.port = httpd.server_address[1]
        shown = f"[{host}]" if ":" in host else host
        self.base = f"http://{shown}:{self.port}"
        self._thread = threading.Thread(target=httpd.serve_forever, name=f"stub-{self.kind}", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        httpd = self._httpd
        if httpd is None:
            return
        httpd.shutdown()
        httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._httpd = None
        self._thread = None


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt: str, *args) -> None:
        return

    @property
    def stub(self) -> ContractStub:
        return self.server.stub  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def _handle(self, method: str) -> None:
        stub = self.stub
        if not stub.accepting:
            self.close_connection = True
            return
        path = urlparse(self.path).path
        query = parse_qs(urlparse(self.path).query)
        body = b""
        if method == "POST":
            try:
                declared = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                self._send(400, {"detail": "bad content-length"})
                return
            if declared > stub.max_body:
                if declared <= 1_000_000:
                    self.rfile.read(declared)
                self._send(413, {"detail": f"request body too large (max {stub.max_body} bytes)"})
                return
            body = self.rfile.read(declared) if declared else b""
            if len(body) > stub.max_body:
                self._send(413, {"detail": f"request body too large (max {stub.max_body} bytes)"})
                return
        token = self.headers.get("X-Admin-Token")
        with stub.lock:
            # Record before any stall so a client that times out can still see the attempt.
            stub.hits.append({
                "method": method,
                "path": path,
                "token": token,
                "n": len(body),
                "idempotency": self.headers.get("Idempotency-Key"),
                "t": time.monotonic(),
            })
            if method == "POST":
                stub.bodies.append((path, body))
            delay = stub.delay.get(f"{method} {path}", stub.delay.get(path, 0.0))
            forced = stub.fail.get(path)
            raw = stub.raw.get(path) if method == "GET" else None
        if delay:
            time.sleep(delay)
        if path not in _routes(stub.kind):
            self._send(404, {"detail": "not found"})
            return
        if _protected(stub.kind, path) and not _token_ok(token, stub.token):
            self._send(401, {"detail": "Bad admin token"})
            return
        if forced:
            self._send(forced, {"detail": "forced failure"})
            return
        if raw is not None:
            self._send(200, raw=raw)
            return
        if stub.kind == "avrone":
            self._avrone(method, path, query, body)
        else:
            self._ck(method, path, body)

    def _avrone(self, method: str, path: str, query: dict, body: bytes) -> None:
        stub = self.stub
        if path == "/api/status":
            with stub.lock:
                count = len(stub.board.posts)
                boot = stub.boot
            self._send(200, {"status": "ok", "entries": count, "boot": boot})
            return
        if method == "GET":
            since = (query.get("since") or [None])[0]
            with stub.lock:
                posts = stub.board.export(since)
                boot = stub.boot
            self._send(200, {"posts": posts, "count": len(posts), "boot": boot})
            return
        try:
            data = json.loads(body.decode("utf-8")) if body else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(400, {"detail": "body must be JSON"})
            return
        post, err = normalize_post(data)
        if err or post is None:
            self._send(400, {"detail": err or "body must be a JSON object"})
            return
        with stub.lock:
            stored, duplicate = stub.board.add(post)
        self._send(200, {"posted": True, "duplicate": duplicate, "id": stored["id"]})

    def _ck(self, method: str, path: str, body: bytes) -> None:
        stub = self.stub
        if path == "/health":
            with stub.lock:
                payload = dict(stub.health)
                stored = len(stub.log.events)
            # Same extras CycleKernal PR #14 puts on /health. They change every
            # read; the relay must not treat that as a new health snapshot.
            payload["timestamp"] = datetime.now(timezone.utc).isoformat()
            payload["boot"] = stub.boot
            payload["mode"] = "relay"
            payload["events"] = {"accepted": stored, "tick": time.time()}
            payload["relay"] = {"attempts": time.time(), "connected": True}
            self._send(200, payload)
            return
        if path == "/v1/current_mood":
            with stub.lock:
                payload = dict(stub.mood)
            payload["timestamp"] = datetime.now(timezone.utc).isoformat()
            payload["metrics"] = {"tick": time.time()}
            self._send(200, payload)
            return
        if path == "/v1/events" and method == "POST":
            step = None
            with stub.lock:
                if stub.events_script:
                    step = stub.events_script.pop(0)
            if step is not None and int(step.get("status", 200)) != 200:
                if step.get("delay"):
                    time.sleep(float(step["delay"]))
                extra = {}
                if step.get("retry_after") is not None:
                    extra["Retry-After"] = str(step["retry_after"])
                self._send(int(step["status"]), {"detail": "scripted"}, extra=extra)
                return
        try:
            data = json.loads(body.decode("utf-8")) if body else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(400, {"detail": "body must be JSON"})
            return
        event, err = normalize_event(data)
        if err or event is None:
            self._send(400, {"detail": err or "body must be a JSON object"})
            return
        with stub.lock:
            if stub.legacy_bare_id:
                stored = stub.log.bare_id(str(event["id"]))
                if stored is not None:
                    self._send(200, {
                        "accepted": True,
                        "duplicate": True,
                        "id": event["id"],
                        "source": stored.get("source"),
                    })
                    return
            duplicate = stub.log.add(event)
            stored_source = event.get("source")
        self._send(200, {
            "accepted": True,
            "duplicate": duplicate,
            "id": event["id"],
            "source": stored_source,
        })

    def _send(self, code: int, payload: dict | None = None, raw: bytes | None = None, extra: dict | None = None) -> None:
        data = raw if raw is not None else json.dumps(payload).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True


def _routes(kind: str) -> set[str]:
    if kind == "avrone":
        return {"/api/status", "/api/blackboard"}
    return {"/health", "/v1/current_mood", "/v1/events"}


def _protected(kind: str, path: str) -> bool:
    if kind == "avrone":
        return path == "/api/blackboard"
    return path == "/v1/events"


def _token_ok(got: str | None, expected: str) -> bool:
    if not got:
        return False
    return hmac.compare_digest(got, expected)
