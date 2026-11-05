"""Read-only CycleKernel adapter. No mutation endpoints; bounded HTTP and payload size."""
from __future__ import annotations
import hashlib, json, math, time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_RESPONSE_BYTES = 1_048_576
DEFAULT_TIMEOUT_SECONDS = 3.0
_ALLOWED_PATHS = {"/health", "/state", "/colony"}

class CycleKernelLinkError(ValueError):
    """Invalid endpoint, transport failure, or invalid CycleKernel response."""

def _base_url(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CycleKernelLinkError("base_url must be a canonical URL")
    try:
        p = urlsplit(value); port = p.port
    except ValueError as exc:
        raise CycleKernelLinkError("base_url has invalid authority or port") from exc
    if p.scheme not in {"http", "https"} or not p.hostname:
        raise CycleKernelLinkError("base_url must use http(s) and include hostname")
    if p.username is not None or p.password is not None:
        raise CycleKernelLinkError("credentials in URLs are forbidden")
    if p.path not in {"", "/"} or p.query or p.fragment:
        raise CycleKernelLinkError("base_url must not include path, query, or fragment")
    if port is not None and not 1 <= port <= 65535:
        raise CycleKernelLinkError("base_url port is out of range")
    return urlunsplit((p.scheme, p.netloc, "", "", ""))

def _timeout(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not .05 <= value <= 30:
        raise CycleKernelLinkError("timeout must be finite and between 0.05 and 30 seconds")
    return float(value)

def _decode(raw: bytes) -> Any:
    if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
        raise CycleKernelLinkError("response exceeds the 1 MiB limit or is not bytes")
    try: return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise CycleKernelLinkError("response is not valid UTF-8 JSON") from exc

def validate_observation(path: str, data: Any) -> dict[str, Any]:
    if path == "/health":
        if not isinstance(data, dict) or data.get("status") != "healthy" or not isinstance(data.get("timestamp"), str) or not data["timestamp"]:
            raise CycleKernelLinkError("health response is not healthy or timestamp is missing")
        if not isinstance(data.get("genome_id"), (str, type(None))): raise CycleKernelLinkError("health genome_id has invalid type")
        n = data.get("promoted")
        if isinstance(n, bool) or not isinstance(n, int) or n < 0: raise CycleKernelLinkError("health promoted count is invalid")
    elif path == "/colony":
        if not isinstance(data, dict): raise CycleKernelLinkError("colony response must be an object")
        for k in ("active", "promoted", "held", "dormant"):
            n = data.get(k)
            if isinstance(n, bool) or not isinstance(n, int) or n < 0: raise CycleKernelLinkError(f"colony {k} count is invalid")
        if data["promoted"] > data["active"] or data["held"] > data["active"] or data["promoted"] + data["held"] > data["active"]:
            raise CycleKernelLinkError("colony counts are inconsistent")
        if not isinstance(data.get("genome_id"), (str, type(None))): raise CycleKernelLinkError("colony genome_id has invalid type")
        m = data.get("mood_dot")
        if isinstance(m, bool) or not isinstance(m, (int,float)) or not math.isfinite(m) or not 0 <= m <= 1:
            raise CycleKernelLinkError("colony mood_dot must be finite and in [0,1]")
    elif path == "/state":
        if not isinstance(data, dict) or not isinstance(data.get("universes"), dict): raise CycleKernelLinkError("state universes must be an object")
        total = data.get("total_civilizations")
        if isinstance(total, bool) or not isinstance(total, int) or total < 0: raise CycleKernelLinkError("total_civilizations is invalid")
        logs = data.get("audit_logs")
        if not isinstance(logs, list) or len(logs) > 100: raise CycleKernelLinkError("audit_logs must contain at most 100 entries")
        if not isinstance(data.get("colony"), dict) or not isinstance(data["colony"].get("universes"), dict): raise CycleKernelLinkError("state colony envelope is malformed")
        count = 0
        for uid, u in data["universes"].items():
            if not isinstance(uid, str) or not isinstance(u, dict) or not isinstance(u.get("civilizations"), dict): raise CycleKernelLinkError("universe entry is malformed")
            c = u.get("cycle_count")
            if isinstance(c, bool) or not isinstance(c, int) or c < 0: raise CycleKernelLinkError("universe cycle_count is invalid")
            count += len(u["civilizations"])
        if count != total: raise CycleKernelLinkError("total_civilizations does not match universe contents")
    else: raise CycleKernelLinkError("only documented read-only GET endpoints are allowed")
    return data

def observation_digest(path: str, data: Any) -> str:
    validated = validate_observation(path, data)
    try: raw = json.dumps(validated, sort_keys=True, separators=(",",":"), ensure_ascii=True, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc: raise CycleKernelLinkError("response cannot be canonicalized") from exc
    return hashlib.sha256(raw).hexdigest()

class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CycleKernelLinkError(f"CycleKernel redirect rejected (HTTP {code})")


def fetch_observation(base_url: str, path: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS, opener: Callable[...,Any] | None = None) -> dict[str,Any]:
    root = _base_url(base_url); timeout = _timeout(timeout)
    if path not in _ALLOWED_PATHS: raise CycleKernelLinkError("only documented read-only GET endpoints are allowed")
    req = Request(root + path, headers={"Accept":"application/json","User-Agent":"AVRONE-CycleKernel-Link/1"})
    started = time.monotonic()
    try:
        response = (opener or build_opener(_RejectRedirects()).open)(req, timeout=timeout)
        try:
            status = getattr(response, "status", 200); headers = getattr(response, "headers", {})
            ctype = headers.get("Content-Type", "") if hasattr(headers,"get") else ""
            if status != 200: raise CycleKernelLinkError(f"CycleKernel returned HTTP {status}")
            if ctype and "application/json" not in ctype.lower(): raise CycleKernelLinkError("CycleKernel response content type is not JSON")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        finally:
            close = getattr(response, "close", None)
            if callable(close): close()
    except HTTPError as exc: raise CycleKernelLinkError(f"CycleKernel returned HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc: raise CycleKernelLinkError(f"CycleKernel transport failed: {type(exc).__name__}") from exc
    data = validate_observation(path, _decode(raw))
    return {"schema":"avrone.cyclekernel.observation/v1","source":"cyclekernel","path":path,
            "observed_at_unix":time.time(),"elapsed_ms":round((time.monotonic()-started)*1000,3),
            "digest":observation_digest(path,data),"data":data}
