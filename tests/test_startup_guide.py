"""User-visible startup guidance with temporary state and no external services."""
import contextlib
import io
import json
import os
from unittest.mock import patch

from test_core_regressions import IsolatedCore, core, hook
import recall

GUIDE = '【EverOS 使用说明】'
PROFILE = 'a frozen profile'


class StartupGuideTests(IsolatedCore):
    def setUp(self):
        super().setUp()
        self.profile = self.stack.enter_context(patch.object(core, 'profile_text', return_value=PROFILE))
        self.search = self.stack.enter_context(patch.object(core, 'search_items', return_value=[]))
        self.stack.enter_context(patch.object(hook, 'antigravity_checkpoint_step', return_value=-1))

    def invoke(self, host, event='SessionStart', **extra):
        payload = {'session_id':host or 'standard', 'hook_event_name':event, **extra}
        args = ['hook.py']
        if host == 'mimocode':
            args += ['--mimo-recall']
            payload.update(profile_only=event=='SessionStart', first=False, query=extra.get('prompt','question'))
        if host in ('kimi','kimi_desktop'):
            payload['client_type'] = 'kimi'
        out = io.StringIO()
        with patch.dict(os.environ, {'EVEROS_HOST':host,'EVEROS_WRITE':'0'}), \
                patch.object(hook,'HOST_OVERRIDE',host), patch.object(hook.sys,'argv',args), \
                patch.object(hook.sys,'stdin',io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(out):
            self.assertEqual(hook.main(),0)
        raw=out.getvalue()
        if not raw:return ''
        if host in ('kimi','kimi_desktop','mimocode'):return raw
        value=json.loads(raw)
        if host=='antigravity':
            return '\n'.join(step.get('ephemeralMessage') or (step.get('systemMessage') or {}).get('systemMessage','')
                             for step in value.get('injectSteps',[]))
        return value['hookSpecificOutput']['additionalContext']

    def test_session_start_guide_precedes_profile_in_all_startup_emitters(self):
        for host in ('','trae','antigravity','mimocode'):
            with self.subTest(host=host):
                text=self.invoke(host)
                self.assertTrue(text.startswith(GUIDE), text)
                self.assertLess(text.index(GUIDE),text.index(PROFILE))
        self.search.assert_not_called()
        self.api.assert_not_called()

    def test_guide_is_available_even_for_a_valid_empty_profile(self):
        self.profile.return_value=''
        for host in ('','trae','antigravity','mimocode'):
            with self.subTest(host=host):self.assertTrue(self.invoke(host).startswith(GUIDE))
        self.search.assert_not_called()

    def test_profile_failure_still_explains_usage_without_exposing_error_body(self):
        self.profile.side_effect=RuntimeError('do-not-print-provider-payload')
        for host in ('','trae','antigravity','mimocode'):
            with self.subTest(host=host):
                text=self.invoke(host)
                self.assertTrue(text.startswith(GUIDE))
                self.assertIn('读取',text)
                self.assertNotIn('do-not-print-provider-payload',text)
        self.search.assert_not_called()

    def test_kimi_defers_guide_to_first_prompt_and_does_not_repeat_it(self):
        for host in ('kimi','kimi_desktop'):
            with self.subTest(host=host):
                self.assertEqual(self.invoke(host),'')
                first=self.invoke(host,'UserPromptSubmit',prompt='first question')
                second=self.invoke(host,'UserPromptSubmit',prompt='next question')
                self.assertTrue(first.startswith(GUIDE))
                self.assertIn(PROFILE,first)
                self.assertNotIn(GUIDE,second)
        self.assertEqual(self.profile.call_count,2)
        self.assertEqual(self.search.call_count,4)

    def test_prompt_fallback_covers_missed_session_start_but_uses_no_extra_search(self):
        first=self.invoke('','UserPromptSubmit',prompt='question')
        second=self.invoke('','UserPromptSubmit',prompt='next question')
        self.assertTrue(first.startswith(GUIDE))
        self.assertNotIn(GUIDE,second)
        self.assertEqual(self.search.call_count,2)
        self.profile.assert_not_called()

    def test_startup_and_first_prompt_share_guide_delivery_state(self):
        for host in ('','trae','antigravity','mimocode'):
            with self.subTest(host=host):
                self.assertIn(GUIDE,self.invoke(host))
                prompt_event='PreInvocation' if host=='antigravity' else 'UserPromptSubmit'
                self.assertNotIn(GUIDE,self.invoke(host,prompt_event,prompt='question',**({'invocationNum':1} if host=='antigravity' else {})))

    def test_failed_first_prompt_still_delivers_guide_and_does_not_mark_query_covered(self):
        self.search.side_effect=RuntimeError('do-not-print-provider-payload')
        text=self.invoke('kimi','UserPromptSubmit',prompt='unknown concept')
        self.assertTrue(text.startswith(GUIDE))
        self.assertNotIn('do-not-print-provider-payload',text)
        self.assertFalse(core.load_state(recall.state_path('kimi')).get('coverage'))

    def test_injection_off_emits_neither_guide_nor_profile_or_queries(self):
        for host in ('','kimi','mimocode','antigravity'):
            with self.subTest(host=host), patch.dict(os.environ,{'EVEROS_INJECT':'0'}):
                self.assertEqual(self.invoke(host),'')
        self.profile.assert_not_called();self.search.assert_not_called()

    def test_context_recovery_reissues_guide_then_returns_to_normal_dedup(self):
        for host in ('','trae','antigravity','kimi','mimocode'):
            with self.subTest(host=host):
                first=self.invoke(host,'UserPromptSubmit',prompt='first question')
                self.assertIn(GUIDE,first)
                with recall.locked(host or 'standard') as state:recall.reset_context(state)
                after=self.invoke(host,'UserPromptSubmit',prompt='continue')
                again=self.invoke(host,'UserPromptSubmit',prompt='continue')
                self.assertTrue(after.startswith(GUIDE))
                self.assertNotIn(GUIDE,again)

    def test_identity_switch_reissues_the_current_guide(self):
        self.assertIn(GUIDE,self.invoke('','UserPromptSubmit',prompt='question'))
        with patch.dict(os.environ,{'EVEROS_USER_ID':'other-owner'}):
            self.assertIn(GUIDE,self.invoke('','UserPromptSubmit',prompt='question'))

    def test_updated_guide_is_delivered_once_in_an_existing_context(self):
        self.assertIn(GUIDE,self.invoke('','UserPromptSubmit',prompt='question'))
        with patch.object(recall,'STARTUP_GUIDE',recall.STARTUP_GUIDE+' Updated usage.'):
            changed=self.invoke('','UserPromptSubmit',prompt='next question')
            self.assertIn('Updated usage.',changed)
            self.assertNotIn(GUIDE,self.invoke('','UserPromptSubmit',prompt='another question'))
