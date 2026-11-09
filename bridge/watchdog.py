"""Ping fusion services every 2 seconds and restart the ones that are down.

Backoff starts at 1s and doubles up to 30s. A health document whose uptime,
restart count, and start time do not move is treated as down (stale health).
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable


Probe = Callable[[str], tuple[bool, dict | None]]


class StaleTracker:
    """Frozen uptime + restart count + start time across a real time gap."""

    def __init__(self, gap: float = 1.5) -> None:
        self.gap = gap
        self.prev: dict[str, tuple] = {}

    def observe(self, name: str, body: dict, now: float) -> bool:
        sample = (body.get("uptime_s"), body.get("restarts"), body.get("started_at"), now)
        previous = self.prev.get(name)
        self.prev[name] = sample
        if previous is None:
            return False
        if now - previous[3] < self.gap:
            return False
        return previous[0] == sample[0] and previous[1] == sample[1] and previous[2] == sample[2]

    def forget(self, name: str) -> None:
        self.prev.pop(name, None)


class Service:
    def __init__(self, name: str, url: str, command: list[str]) -> None:
        self.name = name
        self.url = url
        self.command = command
        self.backoff = 1.0
        self.next_restart = 0.0
        self.restarts = 0
        self.backoff_trace: list[float] = []
        self.proc: subprocess.Popen | None = None
        self.last_error = ""


class Watchdog:
    def __init__(self, interval: float = 2.0, probe: Probe | None = None) -> None:
        self.interval = interval
        self.probe = probe or _http_probe
        self.stale = StaleTracker()
        self.services: dict[str, Service] = {}
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._httpd: ThreadingHTTPServer | None = None
        self.port = 0

    def register(self, name: str, url: str, command: list[str]) -> None:
        with self.lock:
            current = self.services.get(name)
            if current and current.proc and current.proc.poll() is None:
                current.proc.kill()
            self.stale.forget(name)
            self.services[name] = Service(name, url, list(command))

    def unregister(self, name: str) -> None:
        with self.lock:
            svc = self.services.pop(name, None)
            self.stale.forget(name)
        if svc and svc.proc and svc.proc.poll() is None:
            svc.proc.kill()
            try:
                svc.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

    def status(self) -> dict:
        with self.lock:
            services = []
            for svc in self.services.values():
                alive = bool(svc.proc and svc.proc.poll() is None)
                services.append({
                    "name": svc.name,
                    "url": svc.url,
                    "restarts": svc.restarts,
                    "backoff": svc.backoff,
                    "backoff_trace": list(svc.backoff_trace),
                    "alive": alive,
                    "pid": svc.proc.pid if alive and svc.proc else None,
                    "last_error": svc.last_error,
                })
        return {"interval_s": self.interval, "services": services}

    def tick(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self.lock:
            services = list(self.services.values())
        for svc in services:
            up, body = self.probe(svc.url)
            stale = False
            if up:
                stale = self.stale.observe(svc.name, body or {}, now)
            if up and not stale:
                svc.backoff = 1.0
                svc.next_restart = 0.0
                svc.last_error = ""
                continue
            svc.last_error = "stale-health" if stale else "down"
            if now < svc.next_restart:
                continue
            svc.backoff_trace.append(svc.backoff)
            self._spawn(svc)
            svc.next_restart = now + svc.backoff
            svc.backoff = min(svc.backoff * 2.0, 30.0)
            svc.restarts += 1

    def _spawn(self, svc: Service) -> None:
        if svc.proc and svc.proc.poll() is None:
            svc.proc.kill()
            try:
                svc.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        svc.proc = subprocess.Popen(
            svc.command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def start(self) -> "Watchdog":
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Control)
        httpd.watchdog = self  # type: ignore[attr-defined]
        self._httpd = httpd
        self.port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, name="watchdog-control", daemon=True).start()
        self.tick()
        self._thread = threading.Thread(target=self._loop, name="watchdog", daemon=True)
        self._thread.start()
        return self

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self.tick()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 1)
        names = list(self.services)
        for name in names:
            self.unregister(name)
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None


def _http_probe(url: str) -> tuple[bool, dict | None]:
    try:
        with urllib.request.urlopen(url, timeout=1.0) as resp:
            if resp.status != 200:
                return False, None
            raw = resp.read()
            body = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(body, dict):
                body = {}
            return True, body
    except Exception:
        return False, None


class _Control(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return

    @property
    def watchdog(self) -> Watchdog:
        return self.server.watchdog  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/status":
            self._send(404, {"detail": "not found"})
            return
        self._send(200, self.watchdog.status())

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/services":
            self._send(404, {"detail": "not found"})
            return
        try:
            size = int(self.headers.get("Content-Length") or "0")
            data = json.loads(self.rfile.read(size).decode("utf-8") or "{}")
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            self._send(400, {"detail": "body must be JSON"})
            return
        name = str(data.get("name") or "")
        url = str(data.get("url") or "")
        command = data.get("command")
        if not name or not url or not isinstance(command, list) or not command:
            self._send(400, {"detail": "name, url, and command are required"})
            return
        self.watchdog.register(name, url, [str(part) for part in command])
        self._send(200, {"registered": name})

    def do_DELETE(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if not path.startswith("/services/"):
            self._send(404, {"detail": "not found"})
            return
        name = path[len("/services/"):]
        self.watchdog.unregister(name)
        self._send(200, {"unregistered": name})

    def _send(self, code: int, payload: dict) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--" in args:
        cmd = args[args.index("--") + 1 :]
    else:
        cmd = args
    if not cmd:
        print("usage: python -m bridge.watchdog -- python -m bridge.drill", file=sys.stderr)
        return 2
    wd = Watchdog(interval=2.0).start()
    env = os.environ.copy()
    env["FUSION_WATCHDOG"] = "1"
    env["FUSION_WATCHDOG_PORT"] = str(wd.port)
    proc = subprocess.Popen(cmd, env=env)
    try:
        return proc.wait()
    except KeyboardInterrupt:
        proc.send_signal(signal.SIGTERM)
        return proc.wait()
    finally:
        wd.stop()


if __name__ == "__main__":
    raise SystemExit(main())
