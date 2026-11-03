import json
import math
import os
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
from bridge import avrone_cyclekernal_link as link


class TestRelay(unittest.TestCase):
    def test_event_stable_id_and_json(self):
        a=link.FusionEvent('avrone','finding','r1',{'text':'a'})
        b=link.FusionEvent('avrone','finding','r1',{'text':'a'})
        self.assertEqual(a.id,b.id)
        self.assertEqual(json.loads(a.to_json())['payload']['text'],'a')

    def test_event_invalid_types(self):
        for payload in ({'x':float('nan')},{'x':float('inf')}):
            with self.assertRaises(link.BridgeConfigError):
                link.FusionEvent('avrone','finding','r',payload)
        with self.assertRaises(ValueError):link.FusionEvent('unknown','finding','r',{})
        with self.assertRaises(ValueError):link.FusionEvent('avrone','finding','r',{},id='../escape')
        with self.assertRaises(ValueError):link.FusionEvent('avrone','finding','r',{},ts=float('nan'))

    def test_origin_rejects_remote_plaintext_and_embedded_auth(self):
        for url in ['http://host.example:80','http://127.0.0.1:8090/admin',
                    'https://x:y@host.example:443','file:///tmp/out','http://127.0.0.1:8090?x=1']:
            with patch.dict(os.environ,{'CK_BASE':url}):
                with self.assertRaises(link.BridgeConfigError):link._origin('CK_BASE',link.CK_DEFAULT)
        with patch.dict(os.environ,{'CK_BASE':'https://example.org:443'}):
            self.assertEqual(link._origin('CK_BASE',link.CK_DEFAULT),'https://example.org:443')

    def test_missing_credentials_fail_before_network(self):
        with patch.dict(os.environ,{'AVRONE_ADMIN_TOKEN':'','CK_FUSION_TOKEN':''}):
            with patch.object(link,'_exchange',side_effect=AssertionError('network used')):
                self.assertEqual(link.relay_loop()['errors'],1)
                self.assertFalse(link.post_to_cyclekernal(link.FusionEvent('avrone','finding','r',{})))

    def test_accepted_ack_required_and_matching_id(self):
        e=link.FusionEvent('avrone','finding','r',{'text':'x'},id='e1')
        with patch.dict(os.environ,{'CK_FUSION_TOKEN':'sample-value'}):
            for reply in ({'accepted':False,'id':'e1'},{'accepted':True,'id':'wrong'},{'posted':True,'id':'e1'}):
                with patch.object(link,'_exchange',return_value=reply):
                    self.assertFalse(link.post_to_cyclekernal(e))
            with patch.object(link,'_exchange',return_value={'accepted':True,'id':'e1'}) as mock:
                self.assertTrue(link.post_to_cyclekernal(e))
                self.assertEqual(mock.call_args.args[1],'/v1/events')

    def test_reverse_feedback_explicit_not_automatic(self):
        e=link.FusionEvent('cyclekernal','mood','run1',{'valence':0.7},id='mood1')
        with patch.dict(os.environ,{'AVRONE_ADMIN_TOKEN':'test-only'}):
            with patch.object(link,'_exchange',return_value={'posted':True,'id':'mood1'}) as mock:
                self.assertTrue(link.post_to_avrone_bridge(e))
                self.assertEqual(mock.call_args.kwargs['payload']['kind'],'conclusion')
                self.assertEqual(mock.call_args.args[1],'/api/blackboard')

    def test_relay_retry_after_negative_ack_and_dedup(self):
        posts={'boot':'node-one','posts':[{'id':'p1','kind':'finding','run_id':'run','text':'hello'},
                                          {'id':'p1','kind':'finding','run_id':'run','text':'hello'},
                                          {'id':'p2','kind':'conclusion','run_id':'run','text':'world'}]}
        with patch.dict(os.environ,{'AVRONE_ADMIN_TOKEN':'a','CK_FUSION_TOKEN':'c'}):
            with patch.object(link,'_exchange',return_value=posts):
                with patch.object(link,'post_to_cyclekernal',side_effect=[False,True,True]) as send:
                    with patch.object(link.time,'sleep'):
                        stats=link.relay_loop(interval=0,max_cycles=2)
        self.assertEqual(stats['errors'],1)
        self.assertEqual(stats['forwarded'],2)
        self.assertEqual(stats['duplicates'],3)
        self.assertEqual(send.call_count,3)
        self.assertEqual(stats['health_mirrored'],0)

    def test_boot_change_retries_ids_again(self):
        vals=[{'boot':'old','posts':[{'id':'a','run_id':'r','kind':'finding','text':'x'}]},
              {'boot':'new','posts':[{'id':'a','run_id':'r','kind':'finding','text':'x'}]}]
        with patch.dict(os.environ,{'AVRONE_ADMIN_TOKEN':'a','CK_FUSION_TOKEN':'c'}):
            with patch.object(link,'_exchange',side_effect=vals):
                with patch.object(link,'post_to_cyclekernal',return_value=True) as send:
                    with patch.object(link.time,'sleep'):
                        stats=link.relay_loop(interval=0,max_cycles=2)
        self.assertEqual(stats['forwarded'],2)
        self.assertEqual(send.call_count,2)

    def test_malformed_post_rejected_without_loss_of_valid(self):
        result={'boot':'b','posts':[{'id':'../x','kind':'finding','run_id':'r','text':'x'},
                                    {'id':'ok1','kind':'finding','run_id':'r','text':'ok'}]}
        with patch.dict(os.environ,{'AVRONE_ADMIN_TOKEN':'a','CK_FUSION_TOKEN':'c'}):
            with patch.object(link,'_exchange',return_value=result):
                with patch.object(link,'post_to_cyclekernal',return_value=True):
                    stats=link.relay_loop(interval=0,max_cycles=1)
        self.assertEqual(stats['rejected'],1)
        self.assertEqual(stats['forwarded'],1)

    def test_response_parser_no_duplicate_or_nonfinite(self):
        for data in [b'{"accepted":true,"accepted":false}',b'{"value":NaN}',b'[]',b'{"x":1e999}']:
            with self.assertRaises(ValueError):link._parse_json(data)

    def test_relay_inputs_rejected(self):
        for kwargs in [{'interval':float('nan')},{'max_cycles':0},{'max_cycles':True},{'interval':-1}]:
            with self.assertRaises(ValueError):link.relay_loop(**kwargs)

if __name__=='__main__':unittest.main()
