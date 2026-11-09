"""Fault-injecting stand-ins for Avrone and CycleKernal.

Docker is not required. These peers speak the fusion contract (4096-byte cap
before auth, ``X-Admin-Token``, ``Idempotency-Key``) and can reproduce the
known upstream bugs when a test asks for them.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import socket
import struct
import threading
import time
import uuid
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

try:
    from .contract import MAX_BODY_BYTES, normalize_event, normalize_post
except ImportError:
    from contract import MAX_BODY_BYTES, normalize_event, normalize_post


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class FaultPeer:
    """One blackboard or one event log. Mutate the attributes to inject faults."""

    def __init__(self, role: str, token: str = "", max_body: int = MAX_BODY_BYTES):
        if role not in ("avrone", "ck", "health"):
            raise ValueError("role must be avrone, ck, or health")
        self.role = role
        self.token = token
        self.previous_token = ""
        self.max_body = max_body
        self.lock = threading.Lock()
        self.posts: OrderedDict[str, dict] = OrderedDict()
        self.events: OrderedDict[tuple, dict] = OrderedDict()
        self.idempotency: dict[str, tuple[str, dict]] = {}
        self.boot = uuid.uuid4().hex[:12]
        self.started = time.time()
        self.started_at = self.started
        self.restarts = int(os.environ.get("PEER_RESTARTS", "0") or "0")
        self.frozen_health = False
        self.frozen_uptime = 1.0
        self.surrogate_bug = False
        self.utf8_bug = False
        self.dedup_mode = "source_id"  # or "id" for the legacy upstream bug
        self.disk_full = False
        self.fail_until = 0.0
        self.drop_until = 0.0
        self.drop_rate = 0.0
        self._drop_n = 0
        self.reset_every = 0
        self._hits = 0
        self.latency_s = 0.0
        self.latency_fn = None  # callable(hit_index) -> seconds, avrone board only
        self.read_timeout = 5.0
        self.mood = {
            "mood": "neutral",
            "traits": ["Neutral-Baseline"],
            "mood_dot": 0.5,
            "ns_fitness": 0.0,
            "genome_id": "gn-harness",
            "promote": {"action": "hold"},
        }
        self.port = 0
        self.base = ""
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self, host: str = "127.0.0.1", port: int = 0) -> "FaultPeer":
        httpd = _Server((host, port), _Handler)
        httpd.peer = self  # type: ignore[attr-defined]
        self._httpd = httpd
        self.port = httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=httpd.serve_forever, name=f"peer-{self.role}", daemon=True)
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

    def health_body(self) -> dict:
        if self.frozen_health:
            uptime = self.frozen_uptime
            restarts = 0
            started = self.started_at
        else:
            uptime = round(time.time() - self.started, 3)
            restarts = self.restarts
            started = self.started_at
        status = "ghost-online" if self.role == "ck" else "ok"
        return {
            "status": status,
            "version": "harness",
            "started_at": started,
            "uptime_s": uptime,
            "restarts": restarts,
            "boot": self.boot,
            "mood": self.mood.get("mood", "neutral"),
            "entries": len(self.posts),
            "stored": len(self.events),
        }

    def should_drop(self) -> bool:
        if time.monotonic() >= self.drop_until or self.drop_rate <= 0:
            return False
        self._drop_n += 1
        if self.drop_rate >= 0.5:
            return self._drop_n % 2 == 0
        return False

    def should_reset(self) -> bool:
        if self.reset_every <= 0:
            return False
        return self._hits > 0 and self._hits % self.reset_every == 0


class _Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        return


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        return

    def setup(self) -> None:
        super().setup()
        peer = getattr(self.server, "peer", None)
        timeout = peer.read_timeout if peer is not None else 5.0
        self.connection.settimeout(timeout)

    @property
    def peer(self) -> FaultPeer:
        return self.server.peer  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def _capped(self, path: str) -> bool:
        return path in ("/api/blackboard", "/v1/events", "/events", "/api/events")

    def _handle(self, method: str) -> None:
        peer = self.peer
        path = urlparse(self.path).path
        query = parse_qs(urlparse(self.path).query)
        with peer.lock:
            peer._hits += 1
            hit = peer._hits
            drop = peer.should_drop()
            reset = peer.should_reset()
        if drop or reset:
            self._rst()
            return
        delay = 0.0
        if peer.latency_fn is not None:
            delay = float(peer.latency_fn(path, hit) or 0.0)
        elif peer.latency_s:
            delay = peer.latency_s
        if delay > 0:
            time.sleep(delay)
        if peer.fail_until and time.monotonic() < peer.fail_until:
            self._send(503, {"detail": "unavailable", "retry_after_s": round(peer.fail_until - time.monotonic(), 3)})
            return
        body = b""
        if method == "POST":
            declared = self.headers.get("Content-Length")
            if declared is not None:
                try:
                    size = int(declared)
                except ValueError:
                    self._send(400, {"detail": "bad content-length"})
                    return
                if self._capped(path) and size > peer.max_body:
                    self._send(413, {"detail": f"request body too large (max {peer.max_body} bytes)"})
                    self.close_connection = True
                    return
            else:
                size = 0
            try:
                body = self.rfile.read(size) if size else b""
            except (TimeoutError, socket.timeout):
                self.close_connection = True
                return
            if self._capped(path) and len(body) > peer.max_body:
                self._send(413, {"detail": f"request body too large (max {peer.max_body} bytes)"})
                self.close_connection = True
                return
            encoding = (self.headers.get("Content-Encoding") or "").lower()
            if encoding == "gzip":
                try:
                    body = gzip.decompress(body)
                except (OSError, EOFError, gzip.BadGzipFile):
                    self._send(400, {"detail": "body must be JSON"})
                    return
                if self._capped(path) and len(body) > peer.max_body:
                    self._send(413, {"detail": f"request body too large (max {peer.max_body} bytes)"})
                    return
            if not self._authorized(path):
                self._send(401, {"detail": "Bad admin token"})
                return
            if peer.disk_full and self._capped(path):
                self._send(507, {"detail": "ENOSPC"})
                return
        elif path in ("/api/blackboard",) and not self._authorized(path):
            self._send(401, {"detail": "Bad admin token"})
            return
        self._route(method, path, query, body)

    def _authorized(self, path: str) -> bool:
        if path not in ("/api/blackboard", "/v1/events", "/events", "/api/events"):
            return True
        peer = self.peer
        if not peer.token and not peer.previous_token:
            return True
        got = self.headers.get("X-Admin-Token") or ""
        return _eq(got, peer.token) or _eq(got, peer.previous_token)

    def _route(self, method: str, path: str, query: dict, body: bytes) -> None:
        peer = self.peer
        if path in ("/health", "/api/status"):
            self._send(200, peer.health_body())
            return
        if path == "/v1/current_mood":
            payload = dict(peer.mood)
            payload["timestamp"] = time.time()
            payload["metrics"] = {"tick": time.time()}
            self._send(200, payload)
            return
        if path == "/api/blackboard" and method == "GET":
            self._board_get(query)
            return
        if path == "/api/blackboard" and method == "POST":
            self._board_post(body)
            return
        if path in ("/v1/events", "/events", "/api/events") and method == "POST":
            self._event_post(body)
            return
        if path in ("/v1/events", "/events") and method == "GET":
            with peer.lock:
                events = list(peer.events.values())
            self._send(200, {"events": events, "count": len(events)})
            return
        self._send(404, {"detail": "not found"})

    def _board_get(self, query: dict) -> None:
        peer = self.peer
        with peer.lock:
            if peer.surrogate_bug and any(_has_surrogate(p.get("text")) for p in peer.posts.values()):
                self._send(500, {"detail": "lone surrogate"})
                return
            posts = [dict(p) for p in peer.posts.values()]
            since = (query.get("since") or [None])[0]
            if since:
                ids = [p["id"] for p in posts]
                if since in ids:
                    posts = posts[ids.index(since) + 1 :]
            boot = peer.boot
        self._send(200, {"posts": posts, "count": len(posts), "boot": boot})

    def _board_post(self, body: bytes) -> None:
        key = self.headers.get("Idempotency-Key") or ""
        cached = self._idempotent(key, body)
        if cached is not None:
            return
        try:
            data = json.loads(body.decode("utf-8")) if body else None
        except UnicodeDecodeError:
            self._send(500 if self.peer.utf8_bug else 400, {"detail": "body must be JSON"})
            return
        except json.JSONDecodeError:
            self._send(400, {"detail": "body must be JSON"})
            return
        post, err = normalize_post(data)
        if err or post is None:
            self._send(400, {"detail": err or "body must be JSON"})
            return
        with self.peer.lock:
            current = self.peer.posts.get(post["id"])
            if current is not None:
                result = {"posted": True, "duplicate": True, "id": post["id"]}
            else:
                self.peer.posts[post["id"]] = post
                result = {"posted": True, "duplicate": False, "id": post["id"]}
            if key:
                self.peer.idempotency[key] = (hashlib.sha256(body).hexdigest(), result)
        self._send(200, result)

    def _event_post(self, body: bytes) -> None:
        key = self.headers.get("Idempotency-Key") or ""
        cached = self._idempotent(key, body)
        if cached is not None:
            return
        try:
            text = body.decode("utf-8") if body else ""
            data = json.loads(text) if text else None
        except UnicodeDecodeError:
            self._send(500 if self.peer.utf8_bug else 400, {"detail": "body must be JSON"})
            return
        except json.JSONDecodeError:
            self._send(400, {"detail": "body must be JSON"})
            return
        event, err = normalize_event(data)
        if err or event is None:
            self._send(400, {"detail": err or "body must be JSON"})
            return
        if self.peer.dedup_mode == "id":
            dedup_key: tuple = (event["id"],)
        else:
            dedup_key = (event["source"], event["id"])
        with self.peer.lock:
            if dedup_key in self.peer.events or (
                self.peer.dedup_mode == "id" and any(k[0] == event["id"] for k in self.peer.events)
            ):
                result = {"accepted": True, "duplicate": True, "id": event["id"]}
            else:
                self.peer.events[dedup_key] = event
                result = {"accepted": True, "duplicate": False, "id": event["id"]}
            if key:
                self.peer.idempotency[key] = (hashlib.sha256(body).hexdigest(), result)
        self._send(200, result)

    def _idempotent(self, key: str, body: bytes) -> bool | None:
        if not key:
            return None
        digest = hashlib.sha256(body).hexdigest()
        with self.peer.lock:
            cached = self.peer.idempotency.get(key)
        if cached is None:
            return None
        if cached[0] != digest:
            self._send(409, {"detail": "idempotency key reused with a different body"})
            return True
        replay = dict(cached[1])
        replay["duplicate"] = True
        self._send(200, replay)
        return True

    def _rst(self) -> None:
        try:
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        except OSError:
            pass
        self.close_connection = True

    def _send(self, code: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        version = getattr(self, "request_version", "")
        if version in ("HTTP/1.0", "HTTP/1.1"):
            self.protocol_version = version
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Request-Version", getattr(self, "request_version", ""))
            # Close after each response. Keep-alive on a full burst deadlocks
            # the client and the handler thread against each other.
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            self.close_connection = True


def _eq(got: str, expected: str) -> bool:
    if not expected or not isinstance(got, str):
        return False
    left, right = got.encode("utf-8"), expected.encode("utf-8")
    if len(left) != len(right):
        return False
    return hmac_compare(left, right)


def hmac_compare(left: bytes, right: bytes) -> bool:
    import hmac
    return hmac.compare_digest(left, right)


def _has_surrogate(value: object) -> bool:
    if not isinstance(value, str):
        return False
    return any(0xD800 <= ord(ch) <= 0xDFFF for ch in value)


class TcpProxy:
    """Extra hop in front of a peer. Stands in for a container network when Docker is down."""

    def __init__(self, upstream_port: int, drop_until: float = 0.0, drop_rate: float = 0.0):
        self.upstream_port = upstream_port
        self.drop_until = drop_until
        self.drop_rate = drop_rate
        self._n = 0
        self._stop = threading.Event()
        self.port = 0
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> "TcpProxy":
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(64)
        sock.settimeout(0.5)
        self._sock = sock
        self.port = sock.getsockname()[1]
        self._thread = threading.Thread(target=self._loop, name="tcp-proxy", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self._n += 1
            if time.monotonic() < self.drop_until and self.drop_rate >= 0.5 and self._n % 2 == 0:
                try:
                    conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                    conn.close()
                except OSError:
                    pass
                continue
            threading.Thread(target=self._splice, args=(conn,), daemon=True).start()

    def _splice(self, client: socket.socket) -> None:
        upstream = socket.socket()
        try:
            upstream.connect(("127.0.0.1", self.upstream_port))
            client.settimeout(5)
            upstream.settimeout(5)
            done = threading.Event()

            def pipe(src: socket.socket, dst: socket.socket) -> None:
                try:
                    while not done.is_set():
                        data = src.recv(65536)
                        if not data:
                            break
                        dst.sendall(data)
                except OSError:
                    pass
                finally:
                    done.set()
                    try:
                        dst.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass

            left = threading.Thread(target=pipe, args=(client, upstream), daemon=True)
            right = threading.Thread(target=pipe, args=(upstream, client), daemon=True)
            left.start()
            right.start()
            left.join(timeout=30)
            right.join(timeout=30)
        except OSError:
            pass
        finally:
            for sock in (client, upstream):
                try:
                    sock.close()
                except OSError:
                    pass


def serve_main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="bridge.harness")
    parser.add_argument("command", nargs="?", default="serve")
    parser.add_argument("--role", default="health")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--token", default="")
    parser.add_argument("--frozen-health", action="store_true")
    args = parser.parse_args(argv)
    peer = FaultPeer(args.role, token=args.token)
    peer.frozen_health = args.frozen_health
    peer.start(args.host, args.port)
    print(json.dumps({"role": peer.role, "base": peer.base, "port": peer.port}), flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        peer.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(serve_main())
