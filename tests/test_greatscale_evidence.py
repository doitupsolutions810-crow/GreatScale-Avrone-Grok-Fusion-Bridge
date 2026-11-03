import math
import pathlib
import sys
import unittest

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
from bridge.greatscale_evidence import CONE, greatscale_event

SHA='a'*40
DIG='b'*64


class GreatScaleEvidenceTests(unittest.TestCase):
    def snapshot(self, chern=1, area=1.2):
        return {'chernNumber':chern,'metricAreaBudget':area,
                'topologicalMappingValid':True,
                'acceptedPsiTopological':CONE+(1-(chern/area)**2)/4,
                'signedTopologicalPolarization':chern/area}

    def event(self, **kw):
        return greatscale_event(self.snapshot(**kw),run_id='r1',source_commit=SHA,artifact_sha256=DIG)

    def test_signed_orientation_is_preserved(self):
        plus,minus=self.event(chern=1),self.event(chern=-1)
        self.assertAlmostEqual(plus.payload['psi_T'],minus.payload['psi_T'])
        self.assertAlmostEqual(plus.payload['polarization'],-minus.payload['polarization'])
        self.assertEqual(plus.payload['status'],'arithmetic_checked_only')
        self.assertFalse(plus.payload['execution_receipt_verified'])
        self.assertEqual(plus.source,'greatscale')
        self.assertEqual(plus.kind,'evidence')

    def test_fail_closed_on_bad_provenance_claim(self):
        for sha,dig in [(SHA[:39],DIG),(SHA,DIG[:63]),('z'*40,DIG)]:
            with self.assertRaises(ValueError):
                greatscale_event(self.snapshot(),run_id='r',source_commit=sha,artifact_sha256=dig)

    def test_reject_numerical_mismatch_and_unaccepted(self):
        for key,value in [('acceptedPsiTopological',CONE),('topologicalMappingValid',False),
                          ('chernNumber',True),('metricAreaBudget',0),('metricAreaBudget',float('nan')),
                          ('signedTopologicalPolarization',-1)]:
            snap=self.snapshot();snap[key]=value
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    greatscale_event(snap,run_id='r',source_commit=SHA,artifact_sha256=DIG)

    def test_reject_area_below_chern_bound(self):
        with self.assertRaises(ValueError):self.event(chern=1,area=.8)

    def test_deterministic_receipt_fingerprint_not_a_signature(self):
        self.assertEqual(self.event().id,self.event().id)
        self.assertNotEqual(self.event(chern=1).id,self.event(chern=-1).id)

if __name__=='__main__':unittest.main()
