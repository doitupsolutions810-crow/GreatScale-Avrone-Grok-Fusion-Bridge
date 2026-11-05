import io
import json
import unittest

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
                           "application/octet-stream"):
            class WrongType(Response):
                headers={"Content-Type":media_type}
            with self.subTest(content_type=media_type), self.assertRaises(CycleKernelLinkError):
                fetch_observation("http://localhost:5000","/health",
                                  opener=lambda *a,**k:WrongType(b"{}"))
        class ParameterizedJSON(Response):
            headers={"Content-Type":"Application/JSON; charset=utf-8"}
        result=fetch_observation("http://localhost:5000","/health",
                                 opener=lambda *a,**k:ParameterizedJSON(json.dumps(health()).encode()))
        self.assertEqual(result["data"],health())

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

    def test_non_callable_transport_override_is_rejected(self):
        with self.assertRaises(CycleKernelLinkError):
            fetch_observation("http://localhost:5000", "/health", opener=object())

    def test_transport_errors_are_sanitized(self):
        def broken(*args,**kwargs): raise TimeoutError("secret details should not leak")
        with self.assertRaisesRegex(CycleKernelLinkError,"TimeoutError") as caught:
            fetch_observation("http://localhost:5000","/health",opener=broken)
        self.assertNotIn("secret details", str(caught.exception))

    def test_unexpected_transport_and_read_failures_are_sanitized(self):
        def broken(*args, **kwargs):
            raise RuntimeError("authorization=top-secret")
        with self.assertRaisesRegex(CycleKernelLinkError, "RuntimeError") as caught:
            fetch_observation("http://localhost:5000", "/health", opener=broken)
        self.assertNotIn("top-secret", str(caught.exception))

        class BrokenRead(Response):
            def read(self, n=-1):
                raise RuntimeError("body-token=secret")
        with self.assertRaisesRegex(CycleKernelLinkError, "RuntimeError") as caught:
            fetch_observation("http://localhost:5000", "/health",
                              opener=lambda *a, **k: BrokenRead(b""))
        self.assertNotIn("body-token", str(caught.exception))

    def test_redirects_are_not_followed(self):
        from urllib.error import HTTPError
        def redirect(*args, **kwargs):
            raise HTTPError("http://localhost:5000/health", 302, "redirect", {}, None)
        with self.assertRaises(CycleKernelLinkError):
            fetch_observation("http://localhost:5000", "/health", opener=redirect)


if __name__ == "__main__":
    unittest.main()
