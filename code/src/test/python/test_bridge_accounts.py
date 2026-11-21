"""Tests for multi-account saving, one-tap switching, and removal in bridge.py."""
import importlib.util
import json
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

BRIDGE_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../main/assets/bridge.py'))


def load_bridge(home):
    env_old = {k: os.environ.get(k) for k in ('HOME', 'AGY_DATA_DIR')}
    os.environ['HOME'] = home
    os.environ.pop('AGY_DATA_DIR', None)
    try:
        spec = importlib.util.spec_from_file_location('bridge_accounts_test', BRIDGE_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in env_old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class MultiAccountTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, 'home')
        os.makedirs(self.home, exist_ok=True)
        self.b = load_bridge(self.home)
        os.makedirs(self.b.AGY_DIR, exist_ok=True)
        os.makedirs(self.b.STATE_DIR, exist_ok=True)
        # Avoid spawning real agy during unit tests.
        self.b.find_models = lambda: None
        self.b.agy.restart_when_idle = mock.MagicMock()
        self.b.voice_dictator.stop = mock.MagicMock()

    def tearDown(self):
        self.tmp.cleanup()

    def write_token(self, email, access='tok-1'):
        payload = {'token': {'access_token': access, 'refresh_token': 'ref-' + email}, 'id_token_email': email}
        self.b.write_secret_bytes(self.b.TOKEN_FILE, json.dumps(payload).encode('utf-8'))

    def test_auto_migrates_existing_signed_in_account(self):
        self.write_token('alice@gmail.com', 'a1')
        self.b.write_json(self.b.ACCOUNT_FILE, {'email': 'alice@gmail.com'})

        info = self.b.account()
        self.assertEqual(info, {
            'signedIn': True,
            'method': 'google',
            'email': 'alice@gmail.com',
            'accounts': ['alice@gmail.com'],
        })
        slot = self.b.token_slot('alice@gmail.com')
        self.assertTrue(os.path.exists(slot))
        self.assertEqual(stat.S_IMODE(os.stat(slot).st_mode), 0o600)

    def test_switch_preserves_refreshed_token_and_swaps_active_token(self):
        # Seed alice then bob.
        self.write_token('alice@gmail.com', 'alice-v1')
        self.b.write_json(self.b.ACCOUNT_FILE, {'email': 'alice@gmail.com'})
        self.b.account()

        # Simulate agy refreshing alice's token before adding bob.
        self.write_token('alice@gmail.com', 'alice-v2-refreshed')
        with self.b.state_lock:
            self.b._save_current_token_locked()

        # Simulate bob signing in.
        self.write_token('bob@gmail.com', 'bob-v1')
        with self.b.state_lock:
            self.b.write_json(self.b.ACCOUNT_FILE, {'email': 'bob@gmail.com'})
            self.b._save_current_token_locked('bob@gmail.com')

        info = self.b.account()
        self.assertEqual(info['email'], 'bob@gmail.com')
        self.assertEqual(info['accounts'], ['alice@gmail.com', 'bob@gmail.com'])

        # Simulate agy refreshing bob's token while bob is active.
        self.write_token('bob@gmail.com', 'bob-v2-refreshed')

        # Switch back to alice (case-insensitive).
        switched = self.b.switch_account('ALICE@GMAIL.COM')
        self.assertEqual(switched['email'], 'alice@gmail.com')
        self.assertEqual(switched['accounts'], ['alice@gmail.com', 'bob@gmail.com'])
        with open(self.b.TOKEN_FILE, encoding='utf-8') as f:
            active_tok = json.load(f)
        self.assertEqual(active_tok['token']['access_token'], 'alice-v2-refreshed')
        self.b.agy.restart_when_idle.assert_called_with('account switched')

        # Switching to already-active account is a safe no-op (no deadlock, no extra restart).
        self.b.agy.restart_when_idle.reset_mock()
        same = self.b.switch_account('alice@gmail.com')
        self.assertEqual(same['email'], 'alice@gmail.com')
        self.b.agy.restart_when_idle.assert_not_called()

        # Switch back to bob and verify bob's refreshed token survived.
        switched_bob = self.b.switch_account('bob@gmail.com')
        self.assertEqual(switched_bob['email'], 'bob@gmail.com')
        with open(self.b.TOKEN_FILE, encoding='utf-8') as f:
            bob_tok = json.load(f)
        self.assertEqual(bob_tok['token']['access_token'], 'bob-v2-refreshed')

    def test_switch_from_api_key_mode_restores_google_auth(self):
        self.write_token('alice@gmail.com', 'alice-v1')
        self.b.write_json(self.b.ACCOUNT_FILE, {'email': 'alice@gmail.com'})
        self.b.account()

        self.b.set_api_key('AIza-test-key')
        self.assertEqual(self.b.account()['method'], 'apiKey')
        self.assertEqual(self.b.account()['accounts'], ['alice@gmail.com'])

        switched = self.b.switch_account('alice@gmail.com')
        self.assertEqual(switched['method'], 'google')
        self.assertEqual(switched['email'], 'alice@gmail.com')
        self.assertNotIn('modelProvider', self.b.agy_settings())

    def test_logout_keeps_other_saved_accounts_and_allows_one_tap_return(self):
        self.write_token('alice@gmail.com', 'alice-v1')
        self.b.write_json(self.b.ACCOUNT_FILE, {'email': 'alice@gmail.com'})
        self.b.account()

        self.write_token('bob@gmail.com', 'bob-v1')
        with self.b.state_lock:
            self.b.write_json(self.b.ACCOUNT_FILE, {'email': 'bob@gmail.com'})
            self.b._save_current_token_locked('bob@gmail.com')

        # Mock agy logout to delete TOKEN_FILE just like real agy.
        self.b.agy.ensure = mock.MagicMock()
        self.b.agy.request = mock.MagicMock(side_effect=lambda m, p, **kw: os.remove(self.b.TOKEN_FILE))
        self.b.logout()

        after = self.b.account()
        self.assertFalse(after['signedIn'])
        self.assertEqual(after['accounts'], ['alice@gmail.com'])

        # One-tap switch to remaining saved account works cleanly.
        back = self.b.switch_account('alice@gmail.com')
        self.assertTrue(back['signedIn'])
        self.assertEqual(back['email'], 'alice@gmail.com')

    def test_remove_inactive_and_active_accounts_and_errors(self):
        self.write_token('alice@gmail.com', 'alice-v1')
        self.b.write_json(self.b.ACCOUNT_FILE, {'email': 'alice@gmail.com'})
        self.b.account()
        self.write_token('bob@gmail.com', 'bob-v1')
        with self.b.state_lock:
            self.b.write_json(self.b.ACCOUNT_FILE, {'email': 'bob@gmail.com'})
            self.b._save_current_token_locked('bob@gmail.com')

        # Remove inactive alice: bob stays active.
        res = self.b.remove_account('alice@gmail.com')
        self.assertEqual(res['email'], 'bob@gmail.com')
        self.assertEqual(res['accounts'], ['bob@gmail.com'])
        self.assertFalse(os.path.exists(self.b.token_slot('alice@gmail.com')))

        # Missing / unknown / path-traversal-like emails fail cleanly.
        with self.assertRaises(self.b.AgyError):
            self.b.switch_account('   ')
        with self.assertRaises(LookupError):
            self.b.switch_account('../../etc/passwd')
        with self.assertRaises(LookupError):
            self.b.remove_account('nobody@gmail.com')

        # Remove active bob: signs out too.
        self.b.agy.ensure = mock.MagicMock()
        self.b.agy.request = mock.MagicMock(side_effect=lambda m, p, **kw: os.remove(self.b.TOKEN_FILE))
        res2 = self.b.remove_account('bob@gmail.com')
        self.assertFalse(res2['signedIn'])
        self.assertEqual(res2['accounts'], [])

    def test_terminal_login_detected_from_id_token_email(self):
        # User signs in via terminal without touching ACCOUNT_FILE.
        self.write_token('terminal@gmail.com', 'term-1')
        info = self.b.account()
        self.assertEqual(info['email'], 'terminal@gmail.com')
        self.assertEqual(info['accounts'], ['terminal@gmail.com'])


if __name__ == '__main__':
    unittest.main()
