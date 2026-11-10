"""Strict syntax for optional payload fields heat, cone_angle, and beta_t.

These rules are about JSON types and bytes. They are not a test of the
Bernoulli identity heat(h) = h(1-h) in avrone-daemon, and they are not a
physical measurement.
"""
from __future__ import annotations

import json
import math
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any


class FieldError(ValueError):
    def __init__(self, field: str, reason: str):
        self.field = field
        self.reason = reason
        super().__init__(f"{field}: {reason}")


def _reject_bool(value: Any, field: str) -> None:
    # bool is a subclass of int. Treating it as a number would turn true into 1.
    if isinstance(value, bool):
        raise FieldError(field, "boolean is not a number")


def parse_heat(value: Any, *, literal: str | None = None) -> dict[str, Any]:
    """Accept a finite JSON number. Reject bool, str, list, and non-finite values.

    If ``literal`` is the original JSON token and it is a non-zero decimal that
    underflows to IEEE-754 0.0, the returned value is 0.0 and ``underflow`` is
    true. The literal token is kept so the wire form is not forgotten.
    """
    _reject_bool(value, "heat")
    if isinstance(value, (str, list, dict)) or value is None:
        raise FieldError("heat", f"type {type(value).__name__} is not a JSON number")
    if not isinstance(value, (int, float)):
        raise FieldError("heat", f"type {type(value).__name__} is not a JSON number")
    number = float(value)
    if not math.isfinite(number):
        raise FieldError("heat", "non-finite")
    underflow = False
    if literal:
        try:
            written = Decimal(literal)
        except InvalidOperation:
            written = None
        if written is not None and written != 0 and number == 0.0:
            underflow = True
    return {"value": number, "literal": literal, "underflow": underflow}


def parse_cone_angle(value: Any) -> float:
    """A finite JSON number only. Strings are never parsed, stripped, or decoded."""
    _reject_bool(value, "cone_angle")
    if isinstance(value, str):
        raise FieldError("cone_angle", "string is not accepted")
    if not isinstance(value, (int, float)):
        raise FieldError("cone_angle", f"type {type(value).__name__} is not a JSON number")
    number = float(value)
    if not math.isfinite(number):
        raise FieldError("cone_angle", "non-finite")
    return number


def parse_beta_t(value: Any) -> int | float | str:
    """A finite number, or a plain string that is not a wrapped JSON container.

    Emoji and other symbols are kept. Format characters (zero-width and other
    Cf/Cc/Cs) are rejected. A string whose entire value is a JSON object or
    array is rejected instead of being decoded.
    """
    _reject_bool(value, "beta_t")
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            raise FieldError("beta_t", "non-finite")
        return value
    if not isinstance(value, str):
        raise FieldError("beta_t", f"type {type(value).__name__} is not a number or string")
    for char in value:
        if unicodedata.category(char) in {"Cc", "Cf", "Cs"}:
            raise FieldError("beta_t", "control or format character")
    if value.strip()[:1] in "{[":
        try:
            embedded = json.loads(value)
        except json.JSONDecodeError:
            embedded = None
        if isinstance(embedded, (dict, list)):
            raise FieldError("beta_t", "double-encoded JSON container")
    return value


def reject_non_finite(value: Any, field: str = "payload", depth: int = 0) -> None:
    """Reject NaN and Infinity anywhere in a payload, and refuse a deep nest.

    ``json.dumps`` would emit ``NaN``, which is not JSON. A nest deeper than
    32 is rejected here so a small hostile document cannot blow the stack.
    """
    if depth > 32:
        raise FieldError(field, "nested too deep")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise FieldError(field, "non-finite")
        return
    if isinstance(value, dict):
        for item in value.values():
            reject_non_finite(item, field, depth + 1)
    elif isinstance(value, list):
        for item in value:
            reject_non_finite(item, field, depth + 1)


def validate_known_fields(payload: dict) -> dict[str, Any]:
    """Validate heat, cone_angle, and beta_t when present. Other keys are ignored."""
    if not isinstance(payload, dict):
        raise FieldError("payload", "not an object")
    out: dict[str, Any] = {}
    if "heat" in payload:
        out["heat"] = parse_heat(payload["heat"])
    if "cone_angle" in payload:
        out["cone_angle"] = parse_cone_angle(payload["cone_angle"])
    if "beta_t" in payload:
        out["beta_t"] = parse_beta_t(payload["beta_t"])
    return out


def loads_strict_object(raw: str) -> dict:
    """Parse one JSON object. A repeated key is an error, not a silent last-wins."""

    def hook(pairs: list) -> dict:
        obj: dict = {}
        for key, value in pairs:
            if key in obj:
                raise FieldError(str(key), "duplicate key")
            obj[key] = value
        return obj

    try:
        parsed = json.loads(raw, object_pairs_hook=hook)
    except json.JSONDecodeError as exc:
        raise FieldError("heat", "malformed JSON") from exc
    if not isinstance(parsed, dict):
        raise FieldError("heat", "missing")
    return parsed


def heat_from_wire(raw: str) -> dict[str, Any]:
    """Parse one JSON object and keep the raw ``heat`` number token."""
    obj = loads_strict_object(raw)
    if "heat" not in obj:
        raise FieldError("heat", "missing")
    return parse_heat(obj["heat"], literal=_raw_number_token(raw, "heat"))


def _raw_number_token(raw: str, field: str) -> str | None:
    key = f'"{field}"'
    index = raw.find(key)
    if index < 0:
        return None
    colon = raw.find(":", index + len(key))
    if colon < 0:
        return None
    i = colon + 1
    while i < len(raw) and raw[i] in " \t\r\n":
        i += 1
    start = i
    if i < len(raw) and raw[i] == "-":
        i += 1
    saw_digit = False
    while i < len(raw) and raw[i].isdigit():
        saw_digit = True
        i += 1
    if i < len(raw) and raw[i] == ".":
        i += 1
        while i < len(raw) and raw[i].isdigit():
            saw_digit = True
            i += 1
    if i < len(raw) and raw[i] in "eE":
        i += 1
        if i < len(raw) and raw[i] in "+-":
            i += 1
        while i < len(raw) and raw[i].isdigit():
            i += 1
    if not saw_digit:
        return None
    return raw[start:i]


def choose_payload(entry: dict) -> tuple[str, dict]:
    """Which side wins when both a structured payload and text are present.

    A dict ``payload`` wins. ``text`` is not parsed and does not override it.
    A body over 4096 bytes is rejected before either side is interpreted.
    """
    structured = entry.get("payload")
    # An empty object is not a structured payload. It must not hide ``text``.
    if isinstance(structured, dict) and structured:
        return "payload", dict(structured)
    text = entry.get("text")
    if isinstance(text, str) and text.startswith("{"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return "text", parsed
    return "text", {"text": text}


def parse_retry_after(header: str | None, *, cap_s: float = 30.0) -> float | None:
    """Delta-seconds only. Words, signs, HTTP-dates, and values above ``cap_s`` are invalid.

    Invalid values return None so the caller uses its own bounded backoff
    instead of sleeping the untrusted number.
    """
    if header is None:
        return None
    text = str(header).strip()
    if not text or text[0] in "+-":
        return None
    if text.count(".") > 1:
        return None
    for char in text:
        if char not in "0123456789.":
            return None
    try:
        value = float(text)
    except ValueError:
        return None
    if not math.isfinite(value) or value < 0 or value > cap_s:
        return None
    return value
