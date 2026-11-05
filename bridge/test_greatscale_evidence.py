import copy, math, unittest
from greatscale_evidence import EvidenceContractError, MAX_DEPTH, MAX_RUNS, build_evidence_event, evidence_snapshot, verify_evidence_event

class ContractEdges(unittest.TestCase):
 def test_roundtrip_digest_and_ref_dedupe(self):
  e=build_evidence_event(source="ci",run_id="r1",failures=1,runs=200,refs=["a","a"])
  self.assertEqual(e["refs"],["a"]); self.assertEqual(verify_evidence_event(e),(True,""))
  self.assertEqual(e["digest"],build_evidence_event(source="ci",run_id="r1",failures=1,runs=200,refs=["a"])["digest"])
 def test_orientation_and_symmetry(self):
  self.assertEqual(evidence_snapshot(0,7)["orientation"],"pass")
  self.assertEqual(evidence_snapshot(7,7)["orientation"],"fail")
  self.assertEqual(evidence_snapshot(0,7)["S"],evidence_snapshot(7,7)["S"])
  self.assertEqual(evidence_snapshot(3,7)["orientation"],"mixed")
 def test_count_types_and_bounds(self):
  for f,r in [(0,1),(1,1),(0,MAX_RUNS),(MAX_RUNS,MAX_RUNS)]: evidence_snapshot(f,r)
  for f,r in [(-1,1),(2,1),(0,0),(0,MAX_RUNS+1),(True,1),(1,False),(1.0,2),("1",2)]:
   with self.assertRaises(EvidenceContractError): evidence_snapshot(f,r)
 def test_provenance_edges(self):
  e=build_evidence_event(source="s",run_id="r",failures=0,runs=1,provenance_depth=MAX_DEPTH,human_root="root")
  self.assertTrue(verify_evidence_event(e)[0])
  for d in [-1,MAX_DEPTH+1,True,1.0]:
   with self.assertRaises(EvidenceContractError): build_evidence_event(source="s",run_id="r",failures=0,runs=1,provenance_depth=d,human_root="root")
  with self.assertRaises(EvidenceContractError): build_evidence_event(source="s",run_id="r",failures=0,runs=1,provenance_depth=1)
 def test_identifier_spoofing(self):
  for v in [" x","x ","x\n","x\u202e","x\u2066","x\u200b","\ud800"]:
   with self.assertRaises(EvidenceContractError): build_evidence_event(source=v,run_id="r",failures=0,runs=1)
 def test_refs_and_unknown_fields(self):
  with self.assertRaises(EvidenceContractError): build_evidence_event(source="s",run_id="r",failures=0,runs=1,refs=["x"]*33)
  e=build_evidence_event(source="s",run_id="r",failures=0,runs=1); bad=copy.deepcopy(e); bad["extra"]=1
  self.assertFalse(verify_evidence_event(bad)[0])
 def test_tampering_and_malformed(self):
  e=build_evidence_event(source="s",run_id="r",failures=1,runs=3)
  for change in [lambda x:x.update(source="other"),lambda x:x.update(digest="A"*64),lambda x:x.update(schema="bad"),lambda x:x.update(provenance_depth=True)]:
   b=copy.deepcopy(e); change(b); self.assertFalse(verify_evidence_event(b)[0])
  b=copy.deepcopy(e); b["evidence"]["p"]=math.nan; self.assertFalse(verify_evidence_event(b)[0])
  for x in [None,[],1,"event",{}]: self.assertFalse(verify_evidence_event(x)[0])



def _daemon_snapshot_reference(failures, runs):
    """Independent transcription of avrone-daemon's Great Scale snapshot math."""
    phi = (1 + math.sqrt(5)) / 2
    cone = (phi - 1) * (phi - 1)
    p = failures / runs
    s = p * (1 - p)
    n = 8 - 4 * s
    n95 = None if p == 0 else (1 if p == 1 else max(1, math.ceil(math.log(.05) / math.log(1-p) - 1e-12)))
    return {
        "failures": failures, "runs": runs, "p": p, "S": s, "variance": s,
        "logCoord": 4*s, "psi": cone+s, "cone": cone, "coneOk": cone+s >= cone-1e-12,
        "onConeFloor": s == 0.0,
        "orientation": "pass" if failures == 0 else ("fail" if failures == runs else "mixed"),
        "n": n, "lambda": phi**(-n), "singleRunDetect": p,
        "detectInRuns": 1-(1-p)**runs, "runsFor95": n95,
    }



class ExtendedPropertyTests(unittest.TestCase):
    def test_matches_daemon_snapshot_for_exhaustive_small_domains_and_seeded_large_samples(self):
        import random
        rng = random.Random(20261105)
        pairs = [(f, r) for r in range(1, 101) for f in range(r+1)]
        pairs += [(rng.randrange(r+1), r) for r in [101, 997, 10_000, 1_000_000, 10**9] for _ in range(100)]
        for failures, runs in pairs:
            assert evidence_snapshot(failures, runs) == _daemon_snapshot_reference(failures, runs)


    def test_daemon_canonical_sha256_golden_vector(self):
        event = build_evidence_event(source="property-suite", run_id="property-run",
                                     failures=0, runs=100)
        assert event["digest"] == "bb6bd562eeb56c60bc8a619ec427c306fff390e911be4627f9ead0848849b85a"
        assert verify_evidence_event(event) == (True, "")


    def test_daemon_security_suite_golden_vector(self):
        event = build_evidence_event(source="golden", run_id="golden-1",
                                     check_id="check-1", failures=1, runs=4)
        assert event["digest"] == "756e0fe4737fb0820345ea265960740f4ec9c44df8eaaf8ebac8d81aa8886b5e"
        assert verify_evidence_event(event) == (True, "")


    def test_rehashed_forged_summary_and_digest_types_are_rejected(self):
        import hashlib
        event = build_evidence_event(source="s", run_id="r", failures=1, runs=10)
        forged = copy.deepcopy(event)
        forged["evidence"]["p"] = 0.9
        body = {key: value for key, value in forged.items() if key != "digest"}
        forged["digest"] = hashlib.sha256(
            __import__("json").dumps(body, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False).encode("utf-8")
        ).hexdigest()
        assert verify_evidence_event(forged)[0] is False
        for bad_digest in ["", "A"*64, "g"*64, "a"*63, "a"*65, None, 7, b"0"*64, "é"*64]:
            changed = copy.deepcopy(event)
            changed["digest"] = bad_digest
            assert verify_evidence_event(changed)[0] is False


    def test_valid_unicode_and_exact_identifier_length_boundaries(self):
        event = build_evidence_event(source="sourcé", run_id="rún-⚛",
                                     check_id="c"*MAX_REF, failures=0, runs=1,
                                     refs=["é"*MAX_REF])
        assert verify_evidence_event(event) == (True, "")
        for field, value in [("source", "s"*81), ("run_id", "r"*129),
                             ("check_id", "c"*(MAX_REF+1))]:
            args = dict(source="s", run_id="r", check_id="c", failures=0, runs=1)
            args[field] = value
            with unittest.TestCase().assertRaises(EvidenceContractError):
                build_evidence_event(**args)


    def test_seeded_randomized_identity_and_metric_mutation_sweeps(self):
        import random
        rng = random.Random(0xA7F00)
        fields = ["source", "run_id", "check_id", "kind", "schema",
                  "capability_id", "human_root", "refs", "provenance_depth"]
        for _ in range(1000):
            event = build_evidence_event(source="fuzz", run_id="run",
                                         failures=rng.randrange(10), runs=10)
            field = rng.choice(fields)
            replacements = {
                "source": "spoof", "run_id": "spoof", "check_id": "spoof",
                "kind": "wrong-kind", "schema": "wrong-schema",
                "capability_id": "cap:spoof", "human_root": "root:spoof",
                "refs": ["spoof"], "provenance_depth": 4096,
            }
            event[field] = replacements[field]
            assert verify_evidence_event(event)[0] is False
        for _ in range(1000):
            event = build_evidence_event(source="fuzz", run_id="run",
                                         failures=rng.randrange(10), runs=10)
            key = rng.choice(["failures", "runs", "p", "S", "variance", "orientation",
                              "logCoord", "psi", "cone", "n", "lambda",
                              "singleRunDetect", "detectInRuns", "runsFor95"])
            event["evidence"][key] = "mutated"
            assert verify_evidence_event(event)[0] is False


    def test_large_untrusted_extra_payload_is_rejected_before_digest_validation(self):
        event = build_evidence_event(source="s", run_id="r", failures=0, runs=1)
        event["unexpected"] = "x" * 2_000_000
        assert verify_evidence_event(event) == (False, "event fields do not match schema v1")


if __name__ == "__main__":
    unittest.main()
