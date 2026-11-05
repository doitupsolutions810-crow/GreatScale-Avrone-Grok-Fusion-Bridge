"""Read-only CycleKernel adapter. No mutation endpoints; bounded HTTP and payload size."""
from __future__ import annotations
import hashlib, json, math, re, time, unicodedata
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_RESPONSE_BYTES = 1_048_576
DEFAULT_TIMEOUT_SECONDS = 3.0
_ALLOWED_PATHS = {"/health", "/state", "/colony"}
_JSON_MEDIA_TYPE = re.compile(r"^application/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*\+json$")

class CycleKernelLinkError(ValueError):
    """Invalid endpoint, transport failure, or invalid CycleKernel response."""

def _base_url(value: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise CycleKernelLinkError("base_url must be a canonical URL")
    # urllib.parse strips some embedded ASCII controls before parsing. Reject them
    # (and other formatting/whitespace characters) before normalization so a
    # malformed configured URL cannot silently become a different authority.
    if any(ch.isspace() or ch == "\\" or unicodedata.category(ch) in {"Cc", "Cf", "Cs"}
           for ch in value):
        raise CycleKernelLinkError("base_url contains whitespace, controls, formatting, or backslashes")
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
    if type(value) not in (int, float):
        raise CycleKernelLinkError("timeout must be finite and between 0.05 and 30 seconds")
    # Compare bounds before float conversion: math.isfinite(10**10000) can overflow.
    try:
        if value < .05 or value > 30:
            raise CycleKernelLinkError("timeout must be finite and between 0.05 and 30 seconds")
        result = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise CycleKernelLinkError("timeout must be finite and between 0.05 and 30 seconds") from exc
    if not math.isfinite(result):
        raise CycleKernelLinkError("timeout must be finite and between 0.05 and 30 seconds")
    return result

def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CycleKernelLinkError("response contains a duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise CycleKernelLinkError("response contains a non-finite JSON number")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise CycleKernelLinkError("response contains a non-finite JSON number")
    return parsed


def _decode(raw: bytes) -> Any:
    if type(raw) is not bytes or len(raw) > MAX_RESPONSE_BYTES:
        raise CycleKernelLinkError("response exceeds the 1 MiB limit or is not bytes")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                          parse_constant=_reject_json_constant, parse_float=_finite_float)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise CycleKernelLinkError("response is not valid UTF-8 JSON") from exc

def _has_plain_string_keys(value: dict[Any, Any]) -> bool:
    return all(type(key) is str for key in value)


def validate_observation(path: str, data: Any) -> dict[str, Any]:
    if type(path) is not str or path not in _ALLOWED_PATHS:
        raise CycleKernelLinkError("only documented read-only GET endpoints are allowed")
    if path == "/health":
        if type(data) is not dict or not _has_plain_string_keys(data) or type(data.get("status")) is not str or data.get("status") != "healthy" or type(data.get("timestamp")) is not str or not data["timestamp"]:
            raise CycleKernelLinkError("health response is not healthy or timestamp is missing")
        if type(data.get("genome_id")) not in (str, type(None)): raise CycleKernelLinkError("health genome_id has invalid type")
        n = data.get("promoted")
        if type(n) is not int or n < 0: raise CycleKernelLinkError("health promoted count is invalid")
    elif path == "/colony":
        if type(data) is not dict or not _has_plain_string_keys(data): raise CycleKernelLinkError("colony response must be an object with string keys")
        for k in ("active", "promoted", "held", "dormant"):
            n = data.get(k)
            if type(n) is not int or n < 0: raise CycleKernelLinkError(f"colony {k} count is invalid")
        if data["promoted"] > data["active"] or data["held"] > data["active"] or data["promoted"] + data["held"] > data["active"]:
            raise CycleKernelLinkError("colony counts are inconsistent")
        if type(data.get("genome_id")) not in (str, type(None)): raise CycleKernelLinkError("colony genome_id has invalid type")
        m = data.get("mood_dot")
        if type(m) not in (int, float) or not 0 <= m <= 1 or not math.isfinite(m):
            raise CycleKernelLinkError("colony mood_dot must be finite and in [0,1]")
    elif path == "/state":
        if type(data) is not dict or not _has_plain_string_keys(data) or type(data.get("universes")) is not dict or not _has_plain_string_keys(data["universes"]): raise CycleKernelLinkError("state universes must be an object with string keys")
        total = data.get("total_civilizations")
        if type(total) is not int or total < 0: raise CycleKernelLinkError("total_civilizations is invalid")
        logs = data.get("audit_logs")
        if type(logs) is not list or len(logs) > 100: raise CycleKernelLinkError("audit_logs must contain at most 100 entries")
        if type(data.get("colony")) is not dict or not _has_plain_string_keys(data["colony"]) or type(data["colony"].get("universes")) is not dict or not _has_plain_string_keys(data["colony"]["universes"]): raise CycleKernelLinkError("state colony envelope is malformed")
        count = 0
        for uid, u in data["universes"].items():
            if type(uid) is not str or type(u) is not dict or not _has_plain_string_keys(u) or type(u.get("civilizations")) is not dict or not _has_plain_string_keys(u["civilizations"]): raise CycleKernelLinkError("universe entry is malformed")
            c = u.get("cycle_count")
            if type(c) is not int or c < 0: raise CycleKernelLinkError("universe cycle_count is invalid")
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
    if opener is not None and not callable(opener):
        raise CycleKernelLinkError("opener must be callable")
    if type(path) is not str or path not in _ALLOWED_PATHS:
        raise CycleKernelLinkError("only documented read-only GET endpoints are allowed")
    req = Request(root + path, headers={"Accept":"application/json","User-Agent":"AVRONE-CycleKernel-Link/1"})
    started = time.monotonic()
    try:
        response = (opener or build_opener(_RejectRedirects()).open)(req, timeout=timeout)
        try:
            status = getattr(response, "status", None); headers = getattr(response, "headers", {})
            ctype = headers.get("Content-Type", "") if hasattr(headers,"get") else ""
            if type(status) is not int or status != 200:
                raise CycleKernelLinkError("CycleKernel returned a non-200 HTTP status")
            if type(ctype) is not str or not ctype.strip():
                raise CycleKernelLinkError("CycleKernel response is missing a JSON content type")
            media_type = ctype.split(";", 1)[0].strip().lower()
            if media_type != "application/json" and not _JSON_MEDIA_TYPE.fullmatch(media_type):
                raise CycleKernelLinkError("CycleKernel response content type is not JSON")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        finally:
            close = getattr(response, "close", None)
            if callable(close): close()
    except HTTPError as exc:
        try:
            exc.close()
        except Exception:
            pass
        raise CycleKernelLinkError("CycleKernel returned an HTTP error") from None
    except Exception as exc:
        # Third-party openers and response objects are an untrusted transport boundary.
        # Preserve only the exception class; never leak headers, URLs, or secret text.
        raise CycleKernelLinkError(f"CycleKernel transport failed: {type(exc).__name__}") from None
    data = validate_observation(path, _decode(raw))
    return {"schema":"avrone.cyclekernel.observation/v1","source":"cyclekernel","path":path,
            "observed_at_unix":time.time(),"elapsed_ms":round((time.monotonic()-started)*1000,3),
            "digest":observation_digest(path,data),"data":data}
