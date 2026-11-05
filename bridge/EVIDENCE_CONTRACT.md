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
