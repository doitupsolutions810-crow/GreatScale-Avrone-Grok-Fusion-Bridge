"""Read-only CycleKernel adapter. No mutation endpoints; bounded HTTP and payload size."""
from __future__ import annotations
import hashlib, ipaddress, json, math, re, time, unicodedata
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_RESPONSE_BYTES = 1_048_576
DEFAULT_TIMEOUT_SECONDS = 3.0
_ALLOWED_PATHS = {"/health", "/state", "/colony"}
_JSON_MEDIA_TYPE = re.compile(r"^application/[A-Za-z0-9][A-Za-z0-9!#$%&'*+.^_\x60|~-]*\+json$", re.IGNORECASE)
_CONTENT_TYPE_TOKEN = re.compile(r"^[!#$%&'*+.^_\x60|~0-9A-Za-z-]+$")


def _header_values(headers: Any, name: str) -> list[Any]:
    """Return all instances of a response header where the HTTP stack exposes them."""
    get_all = getattr(headers, "get_all", None)
    if callable(get_all):
        values = get_all(name)
        if values is None:
            return []
        if type(values) is not list:
            raise CycleKernelLinkError("CycleKernel response headers are malformed")
        return values
    getter = getattr(headers, "get", None)
    if callable(getter):
        value = getter(name)
        return [] if value is None else [value]
    return []


def _is_json_content_type(value: str) -> bool:
    """Validate the complete media type and its parameters, not only its prefix."""
    if type(value) is not str or not value:
        return False
    # MIME field values are ASCII. HTAB is permitted as optional whitespace;
    # all other controls, DEL, non-ASCII, and obs-fold remnants are rejected.
    if any((ord(ch) < 0x20 and ch != "\t") or ord(ch) > 0x7e for ch in value):
        return False
    parts = value.split(";")
    media_type = parts[0].strip(" \t").lower()
    if media_type != "application/json" and not _JSON_MEDIA_TYPE.fullmatch(media_type):
        return False
    seen_parameters: set[str] = set()
    for part in parts[1:]:
        if "=" not in part:
            return False
        name, parameter_value = part.split("=", 1)
        name = name.strip(" \t").lower()
        parameter_value = parameter_value.strip(" \t")
        if not _CONTENT_TYPE_TOKEN.fullmatch(name) or name in seen_parameters:
            return False
        seen_parameters.add(name)
        if _CONTENT_TYPE_TOKEN.fullmatch(parameter_value):
            continue
        if len(parameter_value) < 2 or parameter_value[0] != '"' or parameter_value[-1] != '"':
            return False
        i = 1
        while i < len(parameter_value) - 1:
            ch = parameter_value[i]
            code = ord(ch)
            if ch == "\\":
                i += 1
                if i >= len(parameter_value) - 1:
                    return False
                code = ord(parameter_value[i])
                if code != 0x09 and not 0x20 <= code <= 0x7e:
                    return False
            elif ch == '"' or (code < 0x20 and code != 0x09) or code > 0x7e:
                return False
            i += 1
    return True


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
    if p.netloc.endswith(":"):
        raise CycleKernelLinkError("base_url must not contain an empty port")
    if port is not None:
        raw_port = p.netloc.rsplit(":", 1)[-1]
        if raw_port != str(port):
            raise CycleKernelLinkError("base_url port must use canonical decimal notation")
        if not 1 <= port <= 65535:
            raise CycleKernelLinkError("base_url port is out of range")
    host = p.hostname
    if p.netloc.startswith("[") and ":" not in host:
        raise CycleKernelLinkError("bracketed URL host must be an IPv6 literal")
    if "%" in host or not host.isascii():
        raise CycleKernelLinkError("base_url hostname must not use escaped or non-ASCII forms")
    if ":" in host:
        try:
            ipaddress.IPv6Address(host)
        except ValueError as exc:
            raise CycleKernelLinkError("base_url IPv6 host is invalid") from exc
    else:
        trailing_dot = host.endswith(".")
        dns_host = host[:-1] if trailing_dot else host
        numeric_host = re.fullmatch(r"(?:[0-9]+|0[xX][0-9A-Fa-f]+)(?:\.(?:[0-9]+|0[xX][0-9A-Fa-f]+))*", dns_host)
        if numeric_host:
            if trailing_dot:
                raise CycleKernelLinkError("IPv4 literals must not use a trailing DNS dot")
            try:
                ipaddress.IPv4Address(dns_host)
            except ValueError as exc:
                raise CycleKernelLinkError("base_url IPv4 host must use four canonical decimal octets") from exc
        else:
            if not dns_host or len(dns_host) > 253:
                raise CycleKernelLinkError("base_url DNS hostname length is invalid")
            labels = dns_host.split(".")
            if any(not label or len(label) > 63 or label[0] == "-" or label[-1] == "-"
                   or not re.fullmatch(r"[A-Za-z0-9-]+", label) for label in labels):
                raise CycleKernelLinkError("base_url DNS hostname contains an invalid label")
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
        transport = opener if opener is not None else build_opener(_RejectRedirects()).open
        response = transport(req, timeout=timeout)
        try:
            status = getattr(response, "status", None); headers = getattr(response, "headers", {})
            content_types = _header_values(headers, "Content-Type")
            content_lengths = _header_values(headers, "Content-Length")
            transfer_encodings = _header_values(headers, "Transfer-Encoding")
            ctype = content_types[0] if len(content_types) == 1 else ""
            if type(status) is not int or status != 200:
                raise CycleKernelLinkError("CycleKernel returned a non-200 HTTP status")
            if type(ctype) is not str or not ctype.strip():
                raise CycleKernelLinkError("CycleKernel response is missing a JSON content type")
            if not _is_json_content_type(ctype):
                raise CycleKernelLinkError("CycleKernel response content type is not valid JSON MIME syntax")
            if len(content_lengths) > 1 or len(transfer_encodings) > 1:
                raise CycleKernelLinkError("CycleKernel response has duplicate framing headers")
            expected_length = None
            if content_lengths and transfer_encodings:
                raise CycleKernelLinkError("CycleKernel response cannot combine transfer-encoding and content-length")
            if content_lengths:
                declared_length = content_lengths[0]
                if type(declared_length) is not str or not re.fullmatch(r"[0-9]+", declared_length):
                    raise CycleKernelLinkError("CycleKernel response has an invalid content length")
                normalized_length = declared_length.lstrip("0") or "0"
                max_length = str(MAX_RESPONSE_BYTES)
                if len(normalized_length) > len(max_length) or (len(normalized_length) == len(max_length) and normalized_length > max_length):
                    raise CycleKernelLinkError("CycleKernel response exceeds the 1 MiB limit")
                expected_length = int(normalized_length)
            if transfer_encodings:
                encoding = transfer_encodings[0]
                if type(encoding) is not str or encoding.strip().lower() != "chunked":
                    raise CycleKernelLinkError("CycleKernel response has an unsupported transfer encoding")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if expected_length is not None and (type(raw) is not bytes or len(raw) != expected_length):
                raise CycleKernelLinkError("CycleKernel response body does not match Content-Length")
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
        # Do not expose exception names or text from an untrusted transport.
        raise CycleKernelLinkError("CycleKernel transport failed") from None
    data = validate_observation(path, _decode(raw))
    return {"schema":"avrone.cyclekernel.observation/v1","source":"cyclekernel","path":path,
            "observed_at_unix":time.time(),"elapsed_ms":round((time.monotonic()-started)*1000,3),
            "digest":observation_digest(path,data),"data":data}
