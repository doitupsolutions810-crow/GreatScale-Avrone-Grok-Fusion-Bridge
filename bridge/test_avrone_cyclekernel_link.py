import io
import json
import unittest

import avrone_cyclekernal_link as m

from avrone_cyclekernal_link import (
    CycleKernelLinkError, fetch_observation, observation_digest,
    validate_observation, MAX_RESPONSE_BYTES,
)


def health():
    return {"status":"healthy","timestamp":"2026-11-05T12:00:00","genome_id":"g1","promoted":0}


def colony():
    return {"active":3,"promoted":1,"held":2,"dormant":0,"genome_id":"g1","mood_dot":0.5}


def state():
    return {"universes":{"U1":{"civilizations":{"c1":{},"c2":{},"c3":{}},"cycle_count":7}},
            "total_civilizations":3,"colony":{"universes":{"U1":{}}},"audit_logs":[]}


class Response:
    status = 200
    headers = {"Content-Type":"application/json"}
    def __init__(self, raw): self.raw=raw
    def read(self, n=-1): return self.raw[:n]
    def close(self): pass


class LinkTests(unittest.TestCase):
    def test_valid_health_colony_state(self):
        for path, data in [("/health",health()),("/colony",colony()),("/state",state())]:
            self.assertEqual(validate_observation(path,data),data)
            self.assertEqual(len(observation_digest(path,data)),64)

    def test_deterministic_digest_independent_of_key_order(self):
        a={"status":"healthy","timestamp":"t","genome_id":None,"promoted":0}
        b={"promoted":0,"genome_id":None,"timestamp":"t","status":"healthy"}
        self.assertEqual(observation_digest("/health",a),observation_digest("/health",b))

    def test_bad_urls_rejected(self):
        for url in ["", " localhost:5000", "ftp://localhost", "http://", "http://u:p@localhost",
                    "http://localhost/path", "http://localhost/?x=1", "http://localhost/#x",
                    "http://localhost:99999", "http://localhost:abc"]:
            with self.subTest(url=url), self.assertRaises(CycleKernelLinkError):
                fetch_observation(url,"/health")

    def test_ambiguous_or_noncanonical_url_authorities_are_rejected(self):
        invalid = [
            "http://example.com:", "http://example.com:/", "http://%65xample.com",
            "http://example.com%2f.evil", "http://127.1", "http://0177.0.0.1",
            "http://2130706433", "http://0x7f000001", "http://example..com",
            "http://-bad.example", "http://bad-.example", "http://foo_bar.example",
            "http://.example", "http://example.com:00080",
            "http://127.0.0.1.", "http://1.2.3.4.", "http://[v1.foo]",
        ]
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(CycleKernelLinkError):
                fetch_observation(url, "/health", opener=lambda *a, **k: None)
        valid = ["http://localhost:5000", "http://cyclekernel:5000",
                 "https://example.com", "http://127.0.0.1", "https://[::1]:8443",
                 "http://example.com."]
        for url in valid:
            with self.subTest(valid_url=url):
                self.assertTrue(m._base_url(url).startswith(("http://", "https://")))

    def test_write_or_unknown_routes_rejected_before_network(self):
        for path in ["/simulate/1","/chat/interact","/admin","/health/", None, [], {}]:
            with self.subTest(path=path), self.assertRaises(CycleKernelLinkError):
                fetch_observation("http://localhost:5000",path)

    def test_timeout_validation(self):
        cases = [("zero", 0), ("negative", -1), ("over-limit", 31),
                 ("nan", float("nan")), ("infinity", float("inf")),
                 ("boolean", True), ("string", "3"), ("huge-integer", 10**10000)]
        for label, timeout in cases:
            with self.subTest(case=label), self.assertRaises(CycleKernelLinkError):
                fetch_observation("http://localhost:5000","/health",timeout=timeout)

    def test_successful_fetch_and_headers(self):
        seen={}
        def opener(req, timeout):
            seen["url"]=req.full_url; seen["timeout"]=timeout
            seen["accept"]=req.get_header("Accept")
            return Response(json.dumps(health()).encode())
        result=fetch_observation("http://localhost:5000","/health",opener=opener)
        self.assertEqual(seen,{"url":"http://localhost:5000/health","timeout":3.0,"accept":"application/json"})
        self.assertEqual(result["schema"],"avrone.cyclekernel.observation/v1")
        self.assertEqual(result["data"],health())

    def test_non_200_and_wrong_content_type_fail_closed(self):
        class Bad(Response):
            status=503
        with self.assertRaises(CycleKernelLinkError):
            fetch_observation("http://localhost:5000","/health",opener=lambda *a,**k:Bad(b"{}"))
        for media_type in ("text/plain", "application/jsonp", "application/xml",
                           "application/octet-stream", "application/+json",
                           "application/++json", "application/foo bar+json",
                           "application/*+json", "application/.foo+json",
                           "application/%foo+json"):
            class WrongType(Response):
                headers={"Content-Type":media_type}
            with self.subTest(content_type=media_type), self.assertRaises(CycleKernelLinkError):
                # Valid body ensures rejection is caused by the media type itself.
                fetch_observation("http://localhost:5000","/health",
                                  opener=lambda *a,**k:WrongType(json.dumps(health()).encode()))
        class ParameterizedJSON(Response):
            headers={"Content-Type":"Application/JSON; charset=utf-8"}
        result=fetch_observation("http://localhost:5000","/health",
                                 opener=lambda *a,**k:ParameterizedJSON(json.dumps(health()).encode()))
        self.assertEqual(result["data"],health())
        class VendorJSON(Response):
            headers={"Content-Type":"application/vnd.avrone.observation+json"}
        result=fetch_observation("http://localhost:5000","/health",
                                 opener=lambda *a,**k:VendorJSON(json.dumps(health()).encode()))
        self.assertEqual(result["data"],health())
        class MissingType(Response):
            headers={}
        with self.assertRaises(CycleKernelLinkError):
            fetch_observation("http://localhost:5000","/health",
                              opener=lambda *a,**k:MissingType(b"{}"))

    def test_ambiguous_http_framing_headers_fail_closed(self):
        from email.message import Message
        raw = json.dumps(health()).encode()
        cases = {
            "duplicate_content_length": [("Content-Length", str(len(raw))), ("Content-Length", str(len(raw) + 1))],
            "duplicate_transfer_encoding": [("Transfer-Encoding", "chunked"), ("Transfer-Encoding", "chunked")],
            "transfer_encoding_and_content_length": [("Transfer-Encoding", "chunked"), ("Content-Length", str(len(raw)))],
            "unsupported_transfer_encoding": [("Transfer-Encoding", "gzip")],
            "invalid_content_length": [("Content-Length", "42, 42")],
            "negative_content_length": [("Content-Length", "-1")],
            "oversized_content_length": [("Content-Length", str(MAX_RESPONSE_BYTES + 1))],
        }
        for label, framing in cases.items():
            headers = Message()
            headers.add_header("Content-Type", "application/json")
            for name, value in framing:
                headers.add_header(name, value)
            class FramingResponse(Response):
                pass
            response = FramingResponse(raw)
            response.headers = headers
            with self.subTest(case=label), self.assertRaises(CycleKernelLinkError):
                fetch_observation("http://localhost:5000", "/health",
                                  opener=lambda *a, **k: response)

        headers = Message()
        headers.add_header("Content-Type", "application/json")
        headers.add_header("Content-Length", str(len(raw)))
        class ValidLengthResponse(Response):
            pass
        response = ValidLengthResponse(raw)
        response.headers = headers
        self.assertEqual(fetch_observation("http://localhost:5000", "/health",
                                           opener=lambda *a, **k: response)["data"], health())

    def test_duplicate_content_type_headers_fail_closed(self):
        class DuplicateHeaders:
            def get(self, key, default=""):
                return "application/json"
            def get_all(self, key):
                return ["application/json", "text/plain"]
        class DuplicateResponse(Response):
            headers = DuplicateHeaders()
        with self.assertRaises(CycleKernelLinkError):
            fetch_observation("http://localhost:5000", "/health",
                              opener=lambda *a, **k: DuplicateResponse(json.dumps(health()).encode()))

        class RepeatedSameHeader(DuplicateHeaders):
            def get_all(self, key):
                return ["application/json", "application/json"]
        class RepeatedResponse(Response):
            headers = RepeatedSameHeader()
        with self.assertRaises(CycleKernelLinkError):
            fetch_observation("http://localhost:5000", "/health",
                              opener=lambda *a, **k: RepeatedResponse(json.dumps(health()).encode()))

    def test_content_type_parameters_are_fully_validated(self):
        valid = [
            "application/json", "APPLICATION/JSON", " application/json ",
            "application/json; charset=utf-8", 'application/json; charset="utf-8"',
            "application/json ; version = 1", "application/vnd.avrone+json; profile=compact",
        ]
        invalid = [
            "application/json;", "application/json; x:y", "application/json; charset=bad value",
            'application/json; charset="unterminated', 'application/json; charset="bad"quote"',
            "application/json; charset=utf-8; charset=latin-1", "application/json;\r\n x:y",
            "application/json\r\nX-Evil: y", "application/json\x00", "application/json\x7f",
            "application/json\u200b", "application/json; charset=\u00e9", "text/plain; x=y",
            "application/not-json+html",
        ]
        for ctype in valid:
            class ValidResponse(Response):
                headers = {"Content-Type": ctype}
            with self.subTest(valid=ctype):
                fetch_observation("http://localhost:5000", "/health",
                                  opener=lambda *a, **k: ValidResponse(json.dumps(health()).encode()))
        for ctype in invalid:
            class InvalidResponse(Response):
                headers = {"Content-Type": ctype}
            with self.subTest(invalid=ctype), self.assertRaises(CycleKernelLinkError):
                fetch_observation("http://localhost:5000", "/health",
                                  opener=lambda *a, **k: InvalidResponse(json.dumps(health()).encode()))

    def test_malformed_json_and_oversized_payload_fail_closed(self):
        for raw in [b"{", b"\xff", b"null", b"[]" , b'{"status":"healthy","status":"healthy"}', b'{"mood_dot":NaN}', b'{"mood_dot":1e999}', b'{"promoted":'+b'9'*5000+b'}', b"x"*(MAX_RESPONSE_BYTES+1)]:
            with self.subTest(length=len(raw)), self.assertRaises(CycleKernelLinkError):
                fetch_observation("http://localhost:5000","/health",opener=lambda *a,raw=raw,**k:Response(raw))

    def test_health_schema_and_count_boundaries(self):
        for bad in [None,{},{"status":"degraded","timestamp":"t","genome_id":None,"promoted":0},
                    {**health(),"promoted":True},{**health(),"promoted":-1},
                    {**health(),"timestamp":None},{**health(),"genome_id":[]}]:
            with self.subTest(bad=bad), self.assertRaises(CycleKernelLinkError):
                validate_observation("/health",bad)

    def test_colony_consistency_and_nonfinite_values(self):
        cases = [("boolean-count", {**colony(),"active":True}),
                 ("promoted-exceeds-active", {**colony(),"promoted":4}),
                 ("huge-mood-integer", {**colony(),"mood_dot":10**10000}),
                 ("held-exceeds-active", {**colony(),"held":3}),
                 ("nan", {**colony(),"mood_dot":float("nan")}),
                 ("infinity", {**colony(),"mood_dot":float("inf")}),
                 ("above-one", {**colony(),"mood_dot":1.1})]
        for label, bad in cases:
            with self.subTest(case=label), self.assertRaises(CycleKernelLinkError):
                validate_observation("/colony",bad)

    def test_state_consistency_and_structure(self):
        for bad in [{}, {**state(),"total_civilizations":2},
                    {**state(),"universes":[]},
                    {**state(),"audit_logs":[{}]*101},
                    {**state(),"universes":{"U1":{**state()["universes"]["U1"],"cycle_count":True}}}]:
            with self.subTest(bad=bad), self.assertRaises(CycleKernelLinkError):
                validate_observation("/state",bad)

    def test_response_is_closed_after_success_and_rejection(self):
        class Tracked(Response):
            def __init__(self, raw):
                super().__init__(raw)
                self.closed = False
            def close(self):
                self.closed = True
        ok = Tracked(json.dumps(health()).encode())
        fetch_observation("http://localhost:5000", "/health", opener=lambda *a, **k: ok)
        self.assertTrue(ok.closed)
        class Wrong(Tracked):
            headers={"Content-Type":"text/plain"}
        wrong = Wrong(b"{}")
        with self.assertRaises(CycleKernelLinkError):
            fetch_observation("http://localhost:5000", "/health", opener=lambda *a, **k: wrong)
        self.assertTrue(wrong.closed)

    def test_falsey_callable_transport_is_not_discarded(self):
        calls = []
        class FalseyOpener:
            def __bool__(self): return False
            def __call__(self, request, timeout):
                calls.append((request.full_url, request.get_method(), timeout))
                return Response(json.dumps(health()).encode())
        result = fetch_observation("http://localhost:5000", "/health", timeout=1,
                                   opener=FalseyOpener())
        self.assertEqual(result["data"], health())
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1:], ("GET", 1.0))

    def test_non_callable_transport_override_is_rejected(self):
        with self.assertRaises(CycleKernelLinkError):
            fetch_observation("http://localhost:5000", "/health", opener=object())

    def test_transport_errors_are_sanitized(self):
        def broken(*args,**kwargs): raise TimeoutError("secret details should not leak")
        with self.assertRaisesRegex(CycleKernelLinkError,"CycleKernel transport failed") as caught:
            fetch_observation("http://localhost:5000","/health",opener=broken)
        self.assertNotIn("secret details", str(caught.exception))

    def test_unexpected_transport_and_read_failures_are_sanitized(self):
        def broken(*args, **kwargs):
            raise RuntimeError("authorization=top-secret")
        with self.assertRaisesRegex(CycleKernelLinkError, "CycleKernel transport failed") as caught:
            fetch_observation("http://localhost:5000", "/health", opener=broken)
        self.assertNotIn("top-secret", str(caught.exception))

        class BrokenRead(Response):
            def read(self, n=-1):
                raise RuntimeError("body-token=secret")
        with self.assertRaisesRegex(CycleKernelLinkError, "CycleKernel transport failed") as caught:
            fetch_observation("http://localhost:5000", "/health",
                              opener=lambda *a, **k: BrokenRead(b""))
        self.assertNotIn("body-token", str(caught.exception))

    def test_transport_cyclekernel_errors_are_sanitized(self):
        secret = "credential=TOP_SECRET_X91"
        def broken_opener(*args, **kwargs):
            raise CycleKernelLinkError(secret)
        with self.assertRaisesRegex(CycleKernelLinkError, "CycleKernel transport failed") as caught:
            fetch_observation("http://localhost:5000", "/health", opener=broken_opener)
        self.assertNotIn(secret, str(caught.exception))

        class BrokenClose(Response):
            def close(self):
                raise CycleKernelLinkError(secret)
        with self.assertRaisesRegex(CycleKernelLinkError, "CycleKernel transport failed") as caught:
            fetch_observation("http://localhost:5000", "/health",
                              opener=lambda *a, **k: BrokenClose(json.dumps(health()).encode()))
        self.assertNotIn(secret, str(caught.exception))

        class BrokenHeaders:
            def get(self, *args, **kwargs):
                raise CycleKernelLinkError(secret)
        class BrokenHeaderResponse(Response):
            headers = BrokenHeaders()
        with self.assertRaisesRegex(CycleKernelLinkError, "CycleKernel transport failed") as caught:
            fetch_observation("http://localhost:5000", "/health",
                              opener=lambda *a, **k: BrokenHeaderResponse(json.dumps(health()).encode()))
        self.assertNotIn(secret, str(caught.exception))

    def test_transport_exception_class_name_is_not_disclosed(self):
        secret_name = "credential_TOP_SECRET_DYNAMIC"
        SecretException = type(secret_name, (Exception,), {})
        def broken(*args, **kwargs):
            raise SecretException("untrusted exception text")
        with self.assertRaises(CycleKernelLinkError) as caught:
            fetch_observation("http://localhost:5000", "/health", opener=broken)
        self.assertNotIn(secret_name, str(caught.exception))
        self.assertNotIn("untrusted exception text", str(caught.exception))

    def test_http_error_diagnostics_do_not_trust_custom_code(self):
        from urllib.error import HTTPError
        secret = "authorization=HTTP_SECRET_Z77"
        def broken(*args, **kwargs):
            raise HTTPError("http://localhost:5000/health", secret, secret, {}, None)
        with self.assertRaises(CycleKernelLinkError) as caught:
            fetch_observation("http://localhost:5000", "/health", opener=broken)
        self.assertNotIn(secret, str(caught.exception))

    def test_rejects_hostile_builtin_subclasses_at_trust_boundaries(self):
        class EvilPath(str):
            def __new__(cls): return super().__new__(cls, "/admin")
            def __hash__(self): return hash("/health")
            def __eq__(self, other): return other == "/health"
        class EvilFloat(float):
            def __lt__(self, other): return False
            def __gt__(self, other): return False
        class EvilInt(int):
            def __new__(cls, value=0): return super().__new__(cls, value)
            def __lt__(self, other): return False
            def __gt__(self, other): return False
        class EvilUrl(str):
            def __new__(cls): return super().__new__(cls, "ftp://evil.invalid")
            def strip(self, *args, **kwargs): return "http://localhost:5000"
        class EvilHeader(str):
            def strip(self, *args, **kwargs): return "application/json"
            def split(self, *args, **kwargs): return ["application/json"]
            def lower(self): return "application/json"
        class EvilStatus(int):
            def __new__(cls): return super().__new__(cls, 500)
            def __ne__(self, other): return False
        class EvilBytes(bytes):
            def decode(self, *args, **kwargs):
                return json.dumps(health())
        class EvilDict(dict):
            def get(self, *args, **kwargs): return "healthy"
        class SpoofKey(str):
            def __new__(cls): return super().__new__(cls, "not-status")
            def __hash__(self): return hash("status")
            def __eq__(self, other): return other == "status"
        with self.assertRaises(CycleKernelLinkError):
            fetch_observation("http://localhost:5000", EvilPath(), opener=lambda *a, **k: None)
        with self.assertRaises(CycleKernelLinkError):
            fetch_observation(EvilUrl(), "/health", opener=lambda *a, **k: None)
        with self.assertRaises(CycleKernelLinkError):
            fetch_observation("http://localhost:5000", "/health", timeout=EvilFloat(999), opener=lambda *a, **k: None)
        with self.assertRaises(CycleKernelLinkError):
            validate_observation(EvilPath(), health())
        with self.assertRaises(CycleKernelLinkError):
            validate_observation("/health", EvilDict())
        spoofed = health()
        spoofed[SpoofKey()] = "healthy"
        spoofed.pop("status")
        with self.assertRaises(CycleKernelLinkError):
            validate_observation("/health", spoofed)
        with self.assertRaises(CycleKernelLinkError):
            validate_observation("/health", {**health(), "promoted": EvilInt(0)})
        with self.assertRaises(CycleKernelLinkError):
            validate_observation("/colony", {"active": 3, "promoted": 1, "held": 2,
                "dormant": 0, "genome_id": "g", "mood_dot": EvilFloat(0.5)})
        nested_spoof = {"universes": {"U1": {"civilizations": {"c1": {}, "c2": {}, "c3": {}}, "cycle_count": 7}},
            "total_civilizations": 3, "colony": {"universes": {"U1": {}}}, "audit_logs": []}
        class SpoofUniverseKey(str):
            def __new__(cls): return super().__new__(cls, "not-U1")
            def __hash__(self): return hash("U1")
            def __eq__(self, other): return other == "U1"
        nested_spoof["universes"][SpoofUniverseKey()] = nested_spoof["universes"].pop("U1")
        with self.assertRaises(CycleKernelLinkError):
            validate_observation("/state", nested_spoof)

        class HeaderResponse(Response):
            headers = {"Content-Type": EvilHeader("text/plain")}
        with self.assertRaises(CycleKernelLinkError):
            fetch_observation("http://localhost:5000", "/health",
                              opener=lambda *a, **k: HeaderResponse(json.dumps(health()).encode()))

        class StatusResponse(Response):
            status = EvilStatus()
        with self.assertRaises(CycleKernelLinkError):
            fetch_observation("http://localhost:5000", "/health",
                              opener=lambda *a, **k: StatusResponse(json.dumps(health()).encode()))

        class MissingStatusResponse:
            headers = {"Content-Type": "application/json"}
            def read(self, n=-1): return json.dumps(health()).encode()
            def close(self): pass
        with self.assertRaises(CycleKernelLinkError):
            fetch_observation("http://localhost:5000", "/health",
                              opener=lambda *a, **k: MissingStatusResponse())

        with self.assertRaises(CycleKernelLinkError):
            fetch_observation("http://localhost:5000", "/health",
                              opener=lambda *a, **k: Response(EvilBytes(b"not-json")))

    def test_redirects_are_not_followed(self):
        from urllib.error import HTTPError
        def redirect(*args, **kwargs):
            raise HTTPError("http://localhost:5000/health", 302, "redirect", {}, None)
        with self.assertRaises(CycleKernelLinkError):
            fetch_observation("http://localhost:5000", "/health", opener=redirect)


if __name__ == "__main__":
    unittest.main()
