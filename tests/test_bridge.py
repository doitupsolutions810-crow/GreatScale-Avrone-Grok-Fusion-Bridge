"""Legacy API smoke tests updated to the authenticated v1 fusion contract."""
import os
from unittest import mock
import pytest

from bridge import avrone_cyclekernal_link as link
from bridge.avrone_cyclekernal_link import FusionEvent, post_to_avrone_bridge, post_to_cyclekernal, relay_loop


def test_fusion_event_auto_timestamp():
    event = FusionEvent(source='avrone', kind='finding', run_id='r1', payload={'x':1})
    assert event.ts > 0
    assert '"x":1' in event.to_json()


def test_post_to_cyclekernal_success():
    event = FusionEvent(source='avrone', kind='finding', run_id='r1', payload={'ok':True})
    with mock.patch.dict(os.environ, {'CK_FUSION_TOKEN':'test-only'}):
        with mock.patch.object(link,'_exchange',return_value={'accepted':True,'id':event.id}) as send:
            assert post_to_cyclekernal(event) is True
            assert send.call_args.args[1] == '/v1/events'
            assert send.call_args.kwargs['payload']['id'] == event.id


def test_post_to_cyclekernal_soft_fail():
    event=FusionEvent('avrone','finding','r1',{})
    with mock.patch.dict(os.environ, {'CK_FUSION_TOKEN':'test-only'}):
        with mock.patch.object(link,'_exchange',side_effect=ConnectionError('offline')):
            assert post_to_cyclekernal(event) is False


def test_post_to_avrone_bridge_mood_kind():
    event=FusionEvent('cyclekernal','mood','r2',{'valence':0.7})
    with mock.patch.dict(os.environ, {'AVRONE_ADMIN_TOKEN':'test-only'}):
        with mock.patch.object(link,'_exchange',return_value={'posted':True,'id':event.id}) as send:
            assert post_to_avrone_bridge(event) is True
            assert send.call_args.kwargs['payload']['kind'] == 'conclusion'
            assert send.call_args.kwargs['payload']['role'] == 'cyclekernal-relay'


def test_relay_loop_deduplicates_and_never_mirrors_health_implicitly():
    blackboard={'boot':'avrone-boot','posts':[
        {'id':'e1','kind':'finding','run_id':'r1','text':'hello'},
        {'id':'e1','kind':'finding','run_id':'r1','text':'hello'},
        {'id':'e2','kind':'answer','run_id':'r2','text':'world'}]}
    with mock.patch.dict(os.environ, {'AVRONE_ADMIN_TOKEN':'a','CK_FUSION_TOKEN':'c'}):
        with mock.patch.object(link,'_exchange',return_value=blackboard):
            with mock.patch.object(link,'post_to_cyclekernal',return_value=True):
                stats=relay_loop(interval=0,max_cycles=1)
    assert stats['forwarded']==2
    assert stats['duplicates']==1
    assert stats['health_mirrored']==0
    assert stats['errors']==0


def test_relay_loop_soft_fail_on_blackboard_error():
    with mock.patch.dict(os.environ, {'AVRONE_ADMIN_TOKEN':'a','CK_FUSION_TOKEN':'c'}):
        with mock.patch.object(link,'_exchange',side_effect=ConnectionError('offline')):
            with mock.patch.object(link.time,'sleep'):
                stats=relay_loop(interval=0,max_cycles=2)
    assert stats['forwarded']==0
    assert stats['errors']==2
