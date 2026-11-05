# Fusion bridge evidence adapter

This module mirrors the `avrone.greatscale.evidence/v1` envelope in
`avrone-daemon/avrone/integration_contract.py`, using only the Python standard
library so the bridge can validate events without a cross-repository runtime
dependency.

Run focused tests from this directory:
`python -m unittest -v test_greatscale_evidence`

Security boundary: SHA-256 is not a digital signature or proof of source identity.
Capability and human-root fields are references only. This adapter does not
authorize privileged actions or independently verify capability authority.


## CycleKernel observation link

`avrone_cyclekernal_link.py` provides a read-only adapter for the documented
`GET /health`, `GET /state`, and `GET /colony` endpoints. It rejects other
routes, credentials embedded in base URLs, redirects, deceptive or invalid JSON media types, malformed or ambiguous
JSON, non-finite or exponent-overflow JSON numbers, oversized payloads, invalid response schemas,
and unbounded timeout values. The transport timeout is capped at 30 seconds
and the response body at 1 MiB.

The base URL must come from trusted deployment configuration, not a request
parameter or untrusted agent output. This adapter does not itself establish
network destination trust, authenticate the CycleKernel service, or grant
capability authority. Its SHA-256 observation digest is content-integrity
metadata only; it is not a signature or proof of source identity.

Run the focused adapter tests from `bridge/` with:
`python -m unittest -v test_avrone_cyclekernel_link`


Transport boundary hardening also requires an exact JSON media type (or an application subtype ending in `+json`), rejects deceptive values such as `application/jsonp`, and suppresses chained transport exceptions so secret-bearing exception messages are not retained in the surfaced error context. Tests cover accepted parameterized `application/json` and rejected JSONP/XML/octet-stream content types.
