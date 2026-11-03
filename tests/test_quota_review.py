"""End-to-end regressions for quota failures raised in less common paths."""
from unittest.mock import patch

from test_core_regressions import IsolatedCore, core, response
from test_read_resilience import http_error
import mcp_server
import quota

REAL_SWEEP = core.sweep


def steps():
    return [{'action':f'synthetic step {i}','result':'synthetic observation'} for i in range(3)]


class QuotaReviewTests(IsolatedCore):
    def call(self, name, arguments):
        return mcp_server.Server().handle({'method':'tools/call','params':{
            'name':name,'arguments':arguments}})

    def assert_quota_error(self, result):
        self.assertTrue(result['isError'])
        self.assertIn('quota_exceeded',result['content'][0]['text'])

    def test_cached_profile_quota_is_an_explicit_mcp_error(self):
        quota.Gate(core.STATE,core.cfg(),'retrieval').block(0)
        with patch.object(core,'api',self.real_api):
            self.assert_quota_error(self.call('memory_profile',{}))
        self.net.assert_not_called()

    def test_successful_empty_profile_is_still_a_success(self):
        self.api.return_value={'data':{'profiles':[]}}
        result=self.call('memory_profile',{})
        self.assertFalse(result['isError'])
        self.assertIn('画像为空',result['content'][0]['text'])

    def test_profile_keeps_successful_empty_source_when_another_source_hits_quota(self):
        def api(path,body,timeout):
            if path.startswith('/api/v2/'):
                raise quota.QuotaError('retrieval',0,cached=True)
            return {'data':{'profiles':[]}}
        self.api.side_effect=api
        with patch.dict(core.os.environ,{'EVEROS_READ_V1':'1'}):
            result=self.call('memory_profile',{})
        self.assertFalse(result['isError'])

    def test_fallback_quota_replaces_failed_primary_and_keeps_cached_flag(self):
        for cached in (False,True):
            with self.subTest(cached=cached):
                self.api.side_effect=[http_error(422),quota.QuotaError('retrieval',0,cached=cached)]
                with self.assertRaises(quota.QuotaError) as caught:
                    core.search_items('synthetic',kind='episodes',method='agentic',budget=1,strict=True)
                self.assertEqual(caught.exception.cached,cached)

    def test_successful_empty_primary_survives_quota_in_optional_fallback(self):
        self.api.side_effect=[{'data':{'episodes':[]}},quota.QuotaError('retrieval',0,cached=True)]
        notices=[]
        found=core.search_items('synthetic',kind='episodes',method='agentic',budget=1,strict=True,notices=notices)
        self.assertEqual(found,[])
        self.assertTrue(any('quota_exceeded' in notice for notice in notices))

    def test_hybrid_mcp_quota_is_an_error_not_a_successful_miss(self):
        self.api.side_effect=quota.QuotaError('retrieval',0,cached=True)
        self.assert_quota_error(self.call('memory_search',{'query':'synthetic','kind':'episodes','method':'hybrid'}))

    def test_successful_empty_hybrid_is_still_a_success(self):
        self.api.return_value={'data':{'episodes':[]}}
        result=self.call('memory_search',{'query':'synthetic','kind':'episodes','method':'hybrid'})
        self.assertFalse(result['isError'])
        self.assertIn('没有相关记忆',result['content'][0]['text'])

    def test_cached_session_end_and_sweep_flushes_are_silent_and_keep_pending(self):
        payload=self.transcript();core.write_turns(payload)
        offset=self.ledger()['offset'];core.LOG.write_text('')
        self.api.side_effect=quota.QuotaError('mcu',0,cached=True)
        payload['hook_event_name']='SessionEnd';core.write_turns(payload)
        self.assertTrue(self.ledger()['pending']);self.assertEqual(self.ledger()['offset'],offset)
        self.assertEqual(core.LOG.read_text(),'')
        with patch.object(core,'sync_local_memory'):
            REAL_SWEEP(force=True)
        self.assertTrue(self.ledger()['pending']);self.assertEqual(self.ledger()['offset'],offset)
        self.assertEqual(core.LOG.read_text(),'')

    def test_real_flush_failure_is_still_logged_and_pending_is_kept(self):
        payload=self.transcript();core.write_turns(payload);core.LOG.write_text('')
        self.api.side_effect=RuntimeError('synthetic offline')
        payload['hook_event_name']='SessionEnd';core.write_turns(payload)
        self.assertIn('flush failed',core.LOG.read_text())
        self.assertTrue(self.ledger()['pending'])

    def test_cached_practice_failures_do_not_log_inside_core_or_mcp(self):
        self.api.side_effect=quota.QuotaError('mcu',0,cached=True)
        with patch.dict(core.os.environ,{'EVEROS_READ_V1':'1'}):
            result=self.call('memory_record_practice',{
                'topic':'synthetic','task':'synthetic','steps':steps(),'conclusion':'synthetic'})
        self.assert_quota_error(result)
        self.assertEqual(self.api.call_count,2)
        self.assertFalse(core.LOG.exists())

    def test_mixed_cached_and_real_practice_failures_remain_logged(self):
        self.api.side_effect=[RuntimeError('synthetic offline'),quota.QuotaError('mcu',0,cached=True)]
        with patch.dict(core.os.environ,{'EVEROS_READ_V1':'1'}):
            result=self.call('memory_record_practice',{
                'topic':'synthetic','task':'synthetic','steps':steps(),'conclusion':'synthetic'})
        self.assertTrue(result['isError'])
        self.assertIn('practice incomplete',core.LOG.read_text())
        self.assertIn('mcp memory_record_practice failed',core.LOG.read_text())

    def test_partial_success_practice_is_not_mislabeled_as_cached_only(self):
        self.api.side_effect=[response('accumulated'),response('extracted'),quota.QuotaError('mcu',0,cached=True)]
        with patch.dict(core.os.environ,{'EVEROS_READ_V1':'1'}),patch.object(core.time,'sleep'):
            result=self.call('memory_record_practice',{
                'topic':'synthetic','task':'synthetic','steps':steps(),'conclusion':'synthetic'})
        self.assertTrue(result['isError'])
        self.assertIn('v2 accumulated/extracted',result['content'][0]['text'])
        self.assertIn('practice incomplete',core.LOG.read_text())

    def test_acknowledged_add_then_cached_flush_remains_visible(self):
        self.api.side_effect=[response('accumulated'),quota.QuotaError('mcu',0,cached=True)]
        with patch.object(core.time,'sleep'):
            result=self.call('memory_record_practice',{
                'topic':'synthetic','task':'synthetic','steps':steps(),'conclusion':'synthetic'})
        self.assertTrue(result['isError'])
        self.assertIn('add=accumulated',result['content'][0]['text'])
        self.assertIn('flush 未完成',result['content'][0]['text'])
        self.assertIn('practice incomplete',core.LOG.read_text())

    def test_cached_failure_after_acknowledged_batch_is_not_cached_only(self):
        messages=[{'role':'user','content':f'synthetic {i}'} for i in range(401)]
        self.api.side_effect=[response('accumulated'),quota.QuotaError('mcu',0,cached=True)]
        with self.assertRaises(RuntimeError) as caught:
            core.post_messages('synthetic',messages,ver='v2')
        self.assertFalse(quota.is_cached_refusal(caught.exception))
        self.assertEqual(self.api.call_count,2)
