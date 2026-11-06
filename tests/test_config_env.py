"""Config-file behavior through real runtime consumers; no real credentials/API."""

import io
import json
import os
from unittest.mock import patch

from test_core_regressions import IsolatedCore, core


class ConfigFileTests(IsolatedCore):
    def setUp(self):
        super().setUp()
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))

    def config(self, text):
        (self.root / "config.env").write_text(text, encoding="utf-8")

    def test_file_values_are_literal_except_one_matching_quote_pair(self):
        self.config('''
            # A whole-line comment
            ignored line
            PLAIN = ab=c
            DOUBLE = "ab=c"
            SINGLE = 'ab=c'
            EMPTY_DOUBLE = ""
            EMPTY_SINGLE = ''
            UNMATCHED = "ab=c'
            NESTED = "'inner'"
            LITERAL = "${NAME} $(command) \\n # keep"
            SPACES = " padded "
            DUPLICATE = first
            DUPLICATE = last
        ''')
        values = core.cfg()
        self.assertEqual({key: values[key] for key in (
            "PLAIN", "DOUBLE", "SINGLE", "EMPTY_DOUBLE", "EMPTY_SINGLE", "UNMATCHED",
            "NESTED", "LITERAL", "SPACES", "DUPLICATE")}, {
            "PLAIN": "ab=c", "DOUBLE": "ab=c", "SINGLE": "ab=c",
            "EMPTY_DOUBLE": "", "EMPTY_SINGLE": "", "UNMATCHED": '\"ab=c\'',
            "NESTED": "'inner'", "LITERAL": "${NAME} $(command) \\n # keep",
            "SPACES": " padded ", "DUPLICATE": "last"})

    def test_quoted_identity_and_key_reach_request_without_delimiters(self):
        for quote in ('"', "'"):
            with self.subTest(quote=quote):
                self.config(f'EVEROS_USER_ID={quote}test-owner{quote}\n'
                            f'EVEROS_API_KEY={quote}test-key{quote}\n')
                self.assertTrue(core.ready())
                self.assertEqual(core.owner(), "test-owner")
                self.assertEqual(core.agent_id(), "test-owner_agent")
                self.net.side_effect = None
                self.net.return_value = io.BytesIO(b'{"data": {}}')
                self.real_api("/api/v2/memory/get", {"user_id": core.owner()})
                request = self.net.call_args.args[0]
                self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
                self.assertEqual(json.loads(request.data)["user_id"], "test-owner")

    def test_quoted_flags_disable_writes_and_select_configured_version(self):
        self.config('EVEROS_USER_ID="test-owner"\nEVEROS_API_KEY="test-key"\n'
                    'EVEROS_WRITE="0"\nEVEROS_READ_V1="1"\nEVEROS_API_VERSION="v1"\n')
        self.assertEqual(core.version(), "v1")
        self.assertTrue(core.read_v1())
        with self.assertRaisesRegex(RuntimeError, "EVEROS_WRITE=0"):
            core.remember("test", "config", "must not be sent")
        self.api.assert_not_called()
        self.net.assert_not_called()
        self.config('EVEROS_API_VERSION="v2"\nEVEROS_READ_V1="1"\n')
        self.assertEqual(core.version(), "v2")
        self.assertTrue(core.read_v1())

    def test_quoted_empty_credentials_are_missing(self):
        for quote in ('"', "'"):
            with self.subTest(quote=quote):
                self.config(f'EVEROS_USER_ID={quote}{quote}\nEVEROS_API_KEY={quote}{quote}\n')
                self.assertFalse(core.ready())
                with self.assertRaisesRegex(RuntimeError, "EVEROS_USER_ID"):
                    core.owner()
                with self.assertRaisesRegex(RuntimeError, "EVEROS_API_KEY"):
                    self.real_api("/api/v2/memory/get", {})
                self.net.assert_not_called()

    def test_nonempty_environment_overrides_remain_literal(self):
        self.config('EVEROS_USER_ID="file-owner"\nEVEROS_API_KEY="file-key"\nEVEROS_WRITE="0"\n')
        with patch.dict(os.environ, {"EVEROS_USER_ID": '"env-owner"',
                                     "EVEROS_API_KEY": "'env-key'", "EVEROS_WRITE": ""}):
            values = core.cfg()
            self.assertEqual(core.owner(), '"env-owner"')
            self.assertEqual(values["EVEROS_API_KEY"], "'env-key'")
            self.assertEqual(values["EVEROS_WRITE"], "0")
        self.assertEqual(core.owner(), "file-owner")
