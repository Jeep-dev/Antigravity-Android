"""Tests for reactive wakeup (background tasks, schedule timers, subagents) in bridge.py."""
import importlib.machinery
import importlib.util
import os
import shutil
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE_PATH = os.path.abspath(os.path.join(HERE, '..', '..', 'main', 'assets', 'bridge.py'))


def load_bridge(home):
    os.environ['HOME'] = home
    spec = importlib.util.spec_from_loader('bridge_wakeup_under_test', importlib.machinery.SourceFileLoader('bridge_wakeup_under_test', BRIDGE_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ReactiveWakeupTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='agy_wakeup_')
        self.b = load_bridge(self.home)
        os.makedirs(self.b.STATE_DIR, exist_ok=True)
        os.makedirs(self.b.HISTORY_DIR, exist_ok=True)
        self.b.AUTO_SETTLE_TEXT = 0.15
        self.b.AUTO_SETTLE_IDLE = 0.5
        self.events = []
        self.orig_emit = self.b.hub.emit
        self.b.hub.emit = lambda chat, kind, data=None: self.events.append((chat, kind, data))
        self.responses = []
        self.b.agy.respond = lambda rid, res: self.responses.append((rid, res))
        self.notifies = []
        self.b.agy.notify = lambda method, params: self.notifies.append((method, params))

    def tearDown(self):
        self.b.hub.emit = self.orig_emit
        shutil.rmtree(self.home, ignore_errors=True)

    def make_idle_chat(self):
        c = self.b.Chat('chat-1', 'sess-1')
        c.transcript = self.b.Transcript()
        c.transcript.user('start task')
        c.transcript.apply({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'working...'}})
        c.running = False
        return c

    def test_wakeup_streams_and_saves_history(self):
        c = self.make_idle_chat()
        c.update({'sessionUpdate': 'agent_thought_chunk', 'content': {'type': 'text', 'text': 'Task finished.'}})
        self.assertTrue(c.running)
        self.assertTrue(c.autonomous)

        call = {'toolCallId': 't1', 'name': 'run_command', 'kind': 'execute', 'title': 'ls', 'rawInput': {'CommandLine': 'ls'}}
        c.update(dict(call, sessionUpdate='tool_call', status='in_progress'))
        # Must not auto-finish while tool is running
        time.sleep(0.25)
        self.assertTrue(c.running)

        c.update(dict(call, sessionUpdate='tool_call_update', status='completed',
                      content=[{'type': 'content', 'content': {'type': 'text', 'text': 'ok'}}]))
        c.update({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'Done! Here is the result.'}})
        c.update({'sessionUpdate': 'usage_update', 'used': 2000, 'size': 1000000})

        # Wait for AUTO_SETTLE_TEXT to finish the reactive wakeup (and emit result after saving history)
        deadline = time.time() + 1.0
        while time.time() < deadline:
            if not c.running and any(k == 'cc' and isinstance(d, dict) and d.get('type') == 'result' for _, k, d in self.events):
                break
            time.sleep(0.02)

        self.assertFalse(c.running)
        self.assertFalse(c.autonomous)

        kinds = [(k, d.get('type') if isinstance(d, dict) else None) for _, k, d in self.events]
        self.assertIn(('state', None), kinds)
        self.assertIn(('cc', 'system'), kinds)
        self.assertIn(('cc', 'assistant'), kinds)
        self.assertIn(('cc', 'user'), kinds)
        self.assertIn(('cc', 'result'), kinds)

        saved = self.b.read_json(self.b.history_path('sess-1'), {})
        msgs = saved.get('messages') or []
        all_texts = [blk.get('text', '') for m in msgs for blk in m.get('blocks', []) if blk.get('type') == 'text']
        self.assertTrue(any('Done! Here is the result.' in t for t in all_texts), all_texts)

    def test_wakeup_allows_permission_prompt(self):
        c = self.make_idle_chat()
        call = {'toolCallId': 't2', 'name': 'run_command', 'kind': 'execute', 'title': 'echo hi', 'rawInput': {'CommandLine': 'echo hi'}}
        c.update(dict(call, sessionUpdate='tool_call', status='in_progress'))
        c.permission('rpc-99', {
            'toolCall': dict(call, sessionUpdate='tool_call', status='pending'),
            'options': [{'optionId': 'allow', 'name': 'Allow', 'kind': 'allow_once'}],
        })
        # Should NOT be auto-cancelled
        self.assertEqual(self.responses, [])
        self.assertEqual(len(c.perms), 1)
        req_id = next(iter(c.perms))
        time.sleep(0.25)
        self.assertTrue(c.running)
        self.assertTrue(c.answer(req_id, True))
        self.assertEqual(self.responses, [('rpc-99', {'outcome': {'outcome': 'selected', 'optionId': 'allow'}})])
        c.update(dict(call, sessionUpdate='tool_call_update', status='completed',
                      content=[{'type': 'content', 'content': {'type': 'text', 'text': 'hi'}}]))
        c.update({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'Ran echo.'}})
        deadline = time.time() + 1.0
        while c.running and time.time() < deadline:
            time.sleep(0.02)
        self.assertFalse(c.running)

    def test_stop_cancels_reactive_wakeup(self):
        c = self.make_idle_chat()
        c.update({'sessionUpdate': 'agent_thought_chunk', 'content': {'type': 'text', 'text': 'Still thinking...'}})
        self.assertTrue(c.running)
        self.assertTrue(c.stop())
        self.assertEqual(self.notifies, [('session/cancel', {'sessionId': 'sess-1'})])
        deadline = time.time() + 1.5
        while c.running and time.time() < deadline:
            time.sleep(0.05)
        self.assertFalse(c.running)

    def test_trailing_replay_never_leaks_before_prompt_or_starts_wakeup(self):
        c = self.make_idle_chat()
        # Case 1: send() marked running=True while session/load is still finishing, before prompt() sends session/prompt.
        c.running = True
        c.autonomous = False
        c.prompts = []
        c.timing = {'sent': time.time()}
        c.update({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'old replayed reply'}})
        # Must update transcript, but must NOT emit live stream events or consume first_output timing.
        self.assertEqual(self.events, [])
        self.assertIsNotNone(c.timing)
        self.assertIn('old replayed reply', c.transcript.messages[-1]['blocks'][-1]['text'])

        # Case 2: right after session/load finishes on an idle chat, trailing replay must not start autonomous wakeup.
        c.running = False
        c.replay_quiet_until = time.time() + 1.0
        c.update({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'trailing replay'}})
        self.assertFalse(c.running)
        self.assertFalse(c.autonomous)
        self.assertEqual(self.events, [])


if __name__ == '__main__':
    unittest.main()
