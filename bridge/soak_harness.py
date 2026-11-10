"""Bounded offered-rate soak. Not a physical heat/cone experiment.

Target rate is one offer per millisecond (1000 s^-1) for a finite window.
A missed schedule slot is an explicit late reject. A full queue is an explicit
backpressure reject. Neither is a silent drop.
"""
from __future__ import annotations

import json
import queue
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from tests.relay_stubs import ContractStub


def _rss_kb(pid: int | None = None) -> dict[str, int]:
    import os
    pid = pid or os.getpid()
    out = {"vm_rss_kb": 0, "vm_hwm_kb": 0}
    text = Path(f"/proc/{pid}/status").read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        if line.startswith("VmRSS:"):
            out["vm_rss_kb"] = int(line.split()[1])
        elif line.startswith("VmHWM:"):
            out["vm_hwm_kb"] = int(line.split()[1])
    return out


def run_soak(
    *,
    seconds: float = 60.0,
    target_hz: float = 1000.0,
    queue_max: int = 256,
    token: str = "fusion-test-token",
) -> dict[str, Any]:
    """Offer work for ``seconds`` wall-clock seconds, then drain.

    The window is finite. This does not disable relay deadlines and it does
    not run until killed.
    """
    if seconds <= 0 or seconds > 120:
        raise ValueError("soak window must be in (0, 120] seconds")
    stub = ContractStub("ck", token)
    stub.start()
    q: queue.Queue[dict] = queue.Queue(maxsize=queue_max)
    stop = threading.Event()
    counts = {
        "offered": 0,
        "queued": 0,
        "rejected_late": 0,
        "rejected_backpressure": 0,
        "delivered": 0,
        "duplicate": 0,
        "failed_http": 0,
    }
    lock = threading.Lock()
    samples: list[dict[str, float | int]] = []
    rss_start = _rss_kb()
    rss_peak = rss_start["vm_hwm_kb"]

    def consume() -> None:
        while not stop.is_set() or not q.empty():
            try:
                item = q.get(timeout=0.05)
            except queue.Empty:
                continue
            raw = json.dumps(item).encode("utf-8")
            req = urllib.request.Request(
                stub.base + "/v1/events",
                data=raw,
                headers={"Content-Type": "application/json", "X-Admin-Token": token},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=2) as resp:
                    body = json.loads(resp.read().decode())
                    key = "duplicate" if body.get("duplicate") else "delivered"
            except urllib.error.HTTPError:
                key = "failed_http"
            except Exception:
                key = "failed_http"
            with lock:
                counts[key] += 1
            q.task_done()

    worker = threading.Thread(target=consume, name="soak-consumer", daemon=True)
    worker.start()
    started = time.perf_counter()
    next_t = started
    period = 1.0 / target_hz
    seq = 0
    next_sample = started
    try:
        while True:
            now = time.perf_counter()
            if now - started >= seconds:
                break
            if now > next_t + 0.02:
                missed = int((now - next_t) / period)
                if missed > 0:
                    with lock:
                        counts["offered"] += missed
                        counts["rejected_late"] += missed
                    next_t += missed * period
            if now < next_t:
                time.sleep(min(next_t - now, 0.005))
                continue
            seq += 1
            item = {
                "id": f"soak{seq:08d}",
                "source": "avrone",
                "kind": "finding",
                "run_id": "soak",
                "payload": {"heat": 0.25},
                "ts": 1.0,
            }
            try:
                q.put_nowait(item)
                queued = True
            except queue.Full:
                queued = False
            with lock:
                counts["offered"] += 1
                if queued:
                    counts["queued"] += 1
                else:
                    counts["rejected_backpressure"] += 1
            next_t += period
            if now >= next_sample:
                rss = _rss_kb()
                rss_peak = max(rss_peak, rss["vm_hwm_kb"])
                samples.append({
                    "t_s": round(now - started, 3),
                    "backlog": q.qsize(),
                    "offered": counts["offered"],
                    "delivered": counts["delivered"],
                    "vm_rss_kb": rss["vm_rss_kb"],
                })
                next_sample = now + 1.0
    finally:
        producer_done = time.perf_counter()
        stop.set()
        drain_start = time.perf_counter()
        deadline = drain_start + 30.0
        while not q.empty() and time.perf_counter() < deadline:
            time.sleep(0.05)
        drain_s = time.perf_counter() - drain_start
        worker.join(timeout=5)
        rss_end = _rss_kb()
        rss_peak = max(rss_peak, rss_end["vm_hwm_kb"])
        stub.stop()

    pending = q.qsize()
    accounted = (
        counts["delivered"]
        + counts["duplicate"]
        + counts["failed_http"]
        + counts["rejected_late"]
        + counts["rejected_backpressure"]
        + pending
    )
    duration = producer_done - started
    return {
        "label": "new-soak-harness",
        "seconds_requested": seconds,
        "seconds_measured": duration,
        "target_hz": target_hz,
        "lambda_offered_per_s": counts["offered"] / duration if duration else 0.0,
        "mu_delivered_per_s": counts["delivered"] / duration if duration else 0.0,
        "queue_max": queue_max,
        "peak_backlog": max((int(s["backlog"]) for s in samples), default=0),
        "end_backlog": pending,
        "drain_s": drain_s,
        "counts": counts,
        "lost": counts["offered"] - accounted,
        "samples": samples,
        "rss_start_kb": rss_start,
        "rss_end_kb": rss_end,
        "rss_peak_hwm_kb": rss_peak,
        "stored_events": len(stub.log.list()),
    }
