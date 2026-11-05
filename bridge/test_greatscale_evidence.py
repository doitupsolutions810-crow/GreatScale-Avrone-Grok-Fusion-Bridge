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

if __name__=="__main__": unittest.main()
