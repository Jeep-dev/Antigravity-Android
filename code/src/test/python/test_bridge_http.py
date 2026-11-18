"""Characterization test for bridge.py's HTTP interface.

Starts the bridge's Handler on a Unix socket in a temporary HOME, with a fake `agy` on PATH that speaks
the Agent Client Protocol as `agy acp` does (what it says was taken from the real one: replies, thinking,
tool calls with a question whether they may run, a reply's error said in its text, a session told again
when loaded, a prompt sent while one runs taken into it), keeps its list of conversations in an SQLite
file as agy does, and signs in as `agy acp --login` does. It calls every route and compares status and
body, and the events of a few conversations, with bridge_http.golden.json. A refactoring of the bridge
must leave it as it is; a change of the interface is recorded with --record.

    python3 -I code/src/test/python/test_bridge_http.py [--record] [path/to/bridge.py]
"""
import http.client
import importlib.machinery
import importlib.util
import json
import os
import re
import socket
import stat
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GOLDEN = os.path.join(HERE, 'bridge_http.golden.json')

FAKE_AGY = r'''#!/usr/bin/env python3
import json, os, sqlite3, sys, threading, time, uuid
a = sys.argv[1:]
home = os.environ['HOME']
data = os.path.join(home, '.gemini', 'antigravity-cli')
token = os.path.join(data, 'antigravity-oauth-token')
os.makedirs(os.path.join(data, 'conversations'), exist_ok=True)
MODELS = [('gemini-4-argon-medium', 'Gemini 4 Argon (Medium)'), ('gemini-4-argon-high', 'Gemini 4 Argon (High)'),
          ('gemini-4-argon-low', 'Gemini 4 Argon (Low)'), ('gemini-4-hydrogen-xhigh', 'Gemini 4 Hydrogen (XHigh)'),
          ('gemini-4-hydrogen-high', 'Gemini 4 Hydrogen (High)'), ('claude-opus-5-5-high', 'Claude Opus 5.5 (High)'),
          ('claude-opus-5-5-low', 'Claude Opus 5.5 (Low)'), ('gpt-oss-120b-medium', 'GPT-OSS 120B (Medium)')]
if a[:1] == ['--version']:
    print('9.9.9'); sys.exit(0)
if a[:1] == ['models']:  # as the real one: it wants the terminal's own sign-in, which the app's is not
    sys.stderr.write('Fetching available models...\nError: Please sign in to view available models. Launch the CLI without arguments to sign in.\n')
    sys.exit(1)
if a[:2] == ['acp', '--login']:
    print('Open this link in a browser to log in with Google:\n\n  https://accounts.google.com/o/oauth2/auth?client_id=x&state=y\n')
    sys.stdout.write('Then paste the authorization code, or the full URL of the page you land on, and press Enter: ')
    sys.stdout.flush()
    code = sys.stdin.readline().strip()
    if code == 'good-code':
        open(token, 'w').write('{}')
        print('Logged in as someone@example.com.')
        sys.exit(0)
    print('Error: invalid authorization code')
    sys.exit(1)
assert a[:1] == ['acp'], a
db_path = os.path.join(data, 'conversation_summaries.db')
db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
db.execute('CREATE TABLE IF NOT EXISTS conversation_summaries (conversation_id text PRIMARY KEY, title text NOT NULL DEFAULT "", '
           'preview text NOT NULL DEFAULT "", step_count integer NOT NULL DEFAULT 0, last_modified_time datetime NOT NULL, '
           'workspace_uris text NOT NULL, parent_conversation_id text NOT NULL DEFAULT "", nesting_depth integer NOT NULL DEFAULT 0)')
lock = threading.Lock()
clock = [0]
def now():
    clock[0] += 1
    return '2026-01-01 00:%02d:%02d.000000000+00:00' % (clock[0] // 60 % 60, clock[0] % 60)
def out(o):
    with lock:
        sys.stdout.write(json.dumps(o) + '\n'); sys.stdout.flush()
def update(sid, u, keep=True, live=True):
    if live:
        out({'jsonrpc': '2.0', 'method': 'session/update', 'params': {'sessionId': sid, 'update': u}})
    if keep and u['sessionUpdate'] not in ('usage_update', 'available_commands_update', 'config_option_update', 'current_mode_update'):
        with open(os.path.join(data, 'conversations', sid + '.db'), 'a') as f:
            f.write(json.dumps(u) + '\n')
def signed_in():
    return os.path.exists(token) or os.environ.get('GEMINI_API_KEY')
sessions = {}
waiting = {}
next_id = [0]
def ask(method, params):
    next_id[0] += 1
    rid = str(next_id[0])
    ev = threading.Event()
    waiting[rid] = [ev, None]
    out({'jsonrpc': '2.0', 'id': rid, 'method': method, 'params': params})
    ev.wait(30)
    return waiting.pop(rid)[1]
def session_answer(sid):
    s = sessions[sid]
    return {'sessionId': sid, 'models': {'availableModels': [{'modelId': m, 'name': n} for m, n in MODELS], 'currentModelId': s['model']},
            'modes': {'currentModeId': s['mode'], 'availableModes': [{'id': i, 'name': i} for i in ('default', 'accept-edits', 'auto', 'plan')]}}
def new_session(cwd, meta):
    sid = str(uuid.uuid4())
    sessions[sid] = {'cwd': cwd, 'model': 'gemini-4-argon-medium', 'mode': 'default', 'run': None, 'cancel': False,
                     'extra': [], 'system': ((meta or {}).get('antigravity') or {}).get('systemPrompt', {}).get('append')}
    db.execute('INSERT INTO conversation_summaries (conversation_id, last_modified_time, workspace_uris) VALUES (?, ?, ?)',
               (sid, now(), json.dumps(['file://' + cwd])))
    return sid
def text(sid, t, kind='agent_message_chunk'):
    update(sid, {'sessionUpdate': kind, 'content': {'type': 'text', 'text': t}})
def run(sid, rid, prompt):
    s = sessions[sid]
    words = ' '.join(b.get('text', '') for b in prompt if b.get('type') == 'text')
    pictures = sum(1 for b in prompt if b.get('type') == 'image')
    update(sid, {'sessionUpdate': 'user_message_chunk', 'content': {'type': 'text', 'text': words}}, live=False)  # told when loaded only
    db.execute('UPDATE conversation_summaries SET step_count = step_count + 1, preview = CASE WHEN preview = "" THEN ? ELSE preview END, '
               'last_modified_time = ? WHERE conversation_id = ?', (words, now(), sid))
    update(sid, {'sessionUpdate': 'usage_update', 'used': 1000, 'size': 1000000}, keep=False)
    stop = 'end_turn'
    if 'tool' in words:
        text(sid, 'Look first.', 'agent_thought_chunk')
        call = {'toolCallId': 'call_1', 'name': 'run_command', 'args': {'CommandLine': 'ls', 'toolSummary': 'List files'},
                'kind': 'execute', 'title': 'ls', 'rawInput': {'CommandLine': 'ls', 'toolSummary': 'List files'}}
        update(sid, dict(call, sessionUpdate='tool_call', status='in_progress'))
        update(sid, dict(call, sessionUpdate='tool_call_update', status='pending'))
        answer = ask('session/request_permission', {'sessionId': sid, 'toolCall': dict(call, sessionUpdate='tool_call', status='pending'),
                                                    'options': [{'optionId': 'allow', 'name': 'Allow', 'kind': 'allow_once'},
                                                                {'optionId': 'deny', 'name': 'Deny', 'kind': 'reject_once'}]})
        ok = (answer or {}).get('outcome', {}).get('optionId') == 'allow'
        if ok:
            output = 'Created At: 2026-01-01T00:00:00+09:00\nCompleted At: 2026-01-01T00:00:01+09:00\nCommand exited with code 0.\n\nCommand output:\na.txt\r\n'
            update(sid, dict(call, sessionUpdate='tool_call_update', status='completed', rawOutput={'output': output, 'exit_code': 0},
                             content=[{'type': 'content', 'content': {'type': 'text', 'text': output}}]))
            edit = {'toolCallId': 'call_2', 'name': 'edit_file', 'kind': 'edit', 'title': 'File a.txt',
                    'locations': [{'path': s['cwd'] + '/a.txt'}], 'rawInput': {'Path': s['cwd'] + '/a.txt', 'Target': 'a', 'Replacement': 'b'}}
            update(sid, dict(edit, sessionUpdate='tool_call', status='in_progress'))
            update(sid, dict(edit, sessionUpdate='tool_call_update', status='completed', rawOutput={},
                             content=[{'type': 'diff', 'path': s['cwd'] + '/a.txt', 'oldText': 'a\n', 'newText': 'b\n'}]))
        else:
            update(sid, dict(call, sessionUpdate='tool_call_update', status='failed',
                             content=[{'type': 'content', 'content': {'type': 'text', 'text': 'denied by the user'}}]))
    if 'plan' in words:
        update(sid, {'sessionUpdate': 'plan', 'entries': [{'content': 'Read', 'status': 'completed', 'priority': 'high'},
                                                         {'content': 'Write', 'status': 'in_progress', 'priority': 'high'}]})
        update(sid, {'sessionUpdate': 'plan', 'entries': [{'content': 'Read', 'status': 'completed', 'priority': 'high'},
                                                         {'content': 'Write', 'status': 'completed', 'priority': 'high'}]})
    if 'slow' in words:  # 1.5 s, unless cancelled; a prompt sent meanwhile is taken into this turn
        for _ in range(30):
            if s['cancel']:
                break
            time.sleep(0.05)
    if s['cancel']:
        stop = 'cancelled'
    elif 'fail' in words:
        text(sid, '\n⚠ FAILED_PRECONDITION (code 400): User location is not supported for the API use.\n')
    else:
        with lock:
            extra, s['extra'] = s['extra'], []
        said = [words] + [w for w, r in extra]
        text(sid, 'ok: ')
        text(sid, ' + '.join(said) + (' (%d picture)' % pictures if pictures else '') + (' [%s]' % s['model']))
        if s['system']:
            text(sid, ' / ' + s['system'])
    with lock:
        rid, s['run'], s['cancel'] = s['run'] or rid, None, False
    usage = {'inputTokens': 100, 'outputTokens': 10, 'cachedReadTokens': 50, 'cachedWriteTokens': 0, 'thoughtTokens': 5, 'totalTokens': 165}
    out({'jsonrpc': '2.0', 'id': rid, 'result': {'stopReason': stop, 'usage': usage}})
def handle(m):
    method, p, rid = m.get('method'), m.get('params') or {}, m.get('id')
    def ok(result):
        out({'jsonrpc': '2.0', 'id': rid, 'result': result})
    def err(code, message):
        out({'jsonrpc': '2.0', 'id': rid, 'error': {'code': code, 'message': message}})
    if method == 'initialize':
        ok({'protocolVersion': 1, 'agentCapabilities': {'loadSession': True, 'promptCapabilities': {'image': True}},
            'agentInfo': {'name': 'Antigravity ACP Server', 'version': '9.9.9'}, 'authMethods': [{'id': 'oauth-personal'}]})
    elif method in ('session/new', 'session/load', 'session/prompt') and not signed_in():
        err(-32000, 'Authentication required')
    elif method == 'session/new':
        sid = new_session(p['cwd'], p.get('_meta'))
        ok(session_answer(sid))
        update(sid, {'sessionUpdate': 'available_commands_update', 'availableCommands': [{'name': 'goal'}, {'name': 'compact'}]}, keep=False)
    elif method == 'session/load':
        sid = p['sessionId']
        path = os.path.join(data, 'conversations', sid + '.db')
        if not os.path.exists(path):
            return err(-32602, 'conversation not found: ' + sid)
        sessions.setdefault(sid, {'cwd': p['cwd'], 'model': 'gemini-4-argon-medium', 'mode': 'default', 'run': None,
                                  'cancel': False, 'extra': [], 'system': None})
        with open(path) as f:
            for line in f:
                update(sid, json.loads(line), keep=False)
        update(sid, {'sessionUpdate': 'usage_update', 'used': 1000, 'size': 1000000}, keep=False)
        ok(session_answer(sid))
    elif method == 'session/prompt':
        s = sessions.get(p['sessionId'])
        if s is None:
            return err(-32602, 'session not loaded')
        if s['run'] is not None:  # taken into the running turn; the prompt before it ends at once
            with lock:
                old, s['run'] = s['run'], rid
                s['extra'].append((' '.join(b.get('text', '') for b in p['prompt'] if b.get('type') == 'text'), rid))
            update(p['sessionId'], {'sessionUpdate': 'user_message_chunk', 'content': {'type': 'text', 'text': s['extra'][-1][0]}}, live=False)
            out({'jsonrpc': '2.0', 'id': old, 'result': {'stopReason': 'end_turn'}})
            return
        s['run'] = rid
        threading.Thread(target=run, args=(p['sessionId'], rid, p['prompt']), daemon=True).start()
    elif method == 'session/set_model':
        if p.get('modelId') == 'nope':
            return err(-32602, 'model nope is not recognized')
        sessions[p['sessionId']]['model'] = p['modelId']
        ok({})
    elif method == 'session/set_mode':
        sessions[p['sessionId']]['mode'] = p['modeId']
        ok({})
        update(p['sessionId'], {'sessionUpdate': 'current_mode_update', 'currentModeId': p['modeId']}, keep=False)
    elif method == 'session/close':
        sessions.pop(p['sessionId'], None)
        ok({})
    elif method == 'logout':
        if os.path.exists(token):
            os.remove(token)
        ok({})
    elif method == 'session/cancel':
        s = sessions.get(p['sessionId'])
        if s:
            s['cancel'] = True
    elif rid is not None and method is None:
        slot = waiting.get(str(rid))
        if slot:
            slot[1] = m.get('result')
            slot[0].set()
    elif rid is not None:
        err(-32601, 'method not found: %s' % method)
for line in sys.stdin:
    try:
        m = json.loads(line)
    except ValueError:
        continue
    handle(m)
'''


class Client:
    def __init__(self, sock):
        self.sock = sock

    def call(self, method, path, body=None, raw=None):
        class Conn(http.client.HTTPConnection):
            def connect(inner):
                inner.sock = socket.socket(socket.AF_UNIX)
                inner.sock.settimeout(60)
                inner.sock.connect(self.sock)
        c = Conn('bridge')
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        c.request(method, path, body=data)
        r = c.getresponse()
        text = r.read().decode('utf-8', 'replace')
        c.close()
        try:
            return r.status, json.loads(text)
        except ValueError:
            return r.status, text


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    record = '--record' in sys.argv
    bridge_path = os.path.abspath(args[0] if args else os.path.join(HERE, '..', '..', 'main', 'assets', 'bridge.py'))
    tmp = tempfile.mkdtemp(prefix='bridgetest')
    home = os.path.join(tmp, 'home')
    os.makedirs(os.path.join(tmp, 'bin'))
    os.makedirs(home)
    agy_bin = os.path.join(tmp, 'bin', 'agy')
    with open(agy_bin, 'w') as f:
        f.write(FAKE_AGY)
    os.chmod(agy_bin, os.stat(agy_bin).st_mode | stat.S_IXUSR)
    os.environ['HOME'] = home
    os.environ.pop('GEMINI_API_KEY', None)
    os.environ.pop('AGY_DATA_DIR', None)
    os.environ['PATH'] = os.path.join(tmp, 'bin') + os.pathsep + os.environ['PATH']

    spec = importlib.util.spec_from_file_location('bridge', bridge_path,
                                                  loader=importlib.machinery.SourceFileLoader('bridge', bridge_path))
    bridge = importlib.util.module_from_spec(spec)
    sys.modules['bridge'] = bridge
    spec.loader.exec_module(bridge)
    sock = os.path.join(tmp, 's.sock')
    server = bridge.Server(sock, bridge.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    bridge.find_agy_info()
    client = Client(sock)
    records = []

    def norm(v):
        text = json.dumps(v, ensure_ascii=False).replace(tmp, '<T>')
        text = re.sub(r'[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}', '<uuid>', text)
        text = re.sub(r'"mtime": [0-9.]+', '"mtime": 0', text)
        text = re.sub(r'perm-[0-9a-f]{12}', 'perm-<id>', text)
        text = re.sub(r'agy-[0-9a-f-]{6}-', 'agy-<sid>-', text)
        return json.loads(text)

    def call(method, path, body=None, raw=None):
        status, data = client.call(method, path, body, raw)
        records.append({'req': norm('%s %s %s' % (method, path, '' if body is None else json.dumps(body, sort_keys=True))),
                        'status': status, 'body': norm(data)})
        return status, data

    def note(what, value):
        records.append({'req': what, 'status': 0, 'body': norm(value)})

    def events(chat):
        return [e for e in bridge.hub.events if e['chat'] == chat]

    def results(chat):
        return sum(1 for e in events(chat) if e['kind'] == 'cc' and (e['data'] or {}).get('type') == 'result')

    def wait_for(test, timeout=20):
        end = time.time() + timeout
        while not test() and time.time() < end:
            time.sleep(0.05)
        time.sleep(0.2)

    def settle(chat, n, timeout=20):
        wait_for(lambda: results(chat) >= n, timeout)

    def asked(chat):
        found = [e['data'] for e in events(chat) if e['kind'] == 'cc' and (e['data'] or {}).get('type') == 'control_request']
        return found[-1]['request_id'] if found else None

    def keep_events(chat, what):
        out = []
        for e in events(chat):
            d = dict(e['data'] or {})
            if e['kind'] == 'cc' and d.get('type') == 'stream_event':
                ev = d['event']
                out.append('stream %s %s %s' % (ev['type'], ev.get('index', ''), json.dumps(
                    ev.get('content_block') or ev.get('delta') or ev.get('message') or '', sort_keys=True, ensure_ascii=False)))
            else:
                out.append('%s %s' % (e['kind'], json.dumps(norm(d), sort_keys=True, ensure_ascii=False)))
        note('events of %s' % what, out)

    w = os.path.join(tmp, 'w')
    # ---- GET, nothing yet ----
    call('GET', '/health')
    records[-1]['body'].pop('started', None)
    call('GET', '/config')
    call('GET', '/account')
    call('GET', '/launch')
    call('GET', '/memory?scope=global')
    call('GET', '/memory?scope=nope')
    call('GET', '/memory?scope=folder&cwd=' + os.path.join(tmp, 'f'))
    call('GET', '/dirs?path=' + tmp)
    call('GET', '/dirs?path=/definitely/not/here')
    call('GET', '/image?id=none&at=0&n=0')
    call('GET', '/history?id=none')
    call('GET', '/history?id=../../etc')
    call('GET', '/sessions')
    call('GET', '/nothing')
    call('GET', '/events?since=abc')  # int() fails: ValueError

    # ---- not signed in: a message says so ----
    call('POST', '/send', {'chat': 'c0', 'cwd': w, 'content': [{'type': 'text', 'text': 'hello'}], 'mid': 'z1'})
    settle('c0', 1)
    keep_events('c0', 'a message before signing in')

    # ---- signing in with Google: a link, then the code the page shows ----
    call('POST', '/login', {})
    call('POST', '/login-code', {'code': 'bad-code'})
    call('POST', '/login-code', {'code': 'late'})  # that sign-in is over
    call('POST', '/login', {})
    call('POST', '/login-code', {'code': 'good-code'})
    call('GET', '/account')
    wait_for(lambda: bridge.read_json(bridge.MODELS_FILE, {}).get('models') and not bridge.models_lock.locked())
    call('GET', '/config')
    # the session made to read them is gone again: no file of it, not in the list
    note('conversations after reading the models', sorted(os.listdir(os.path.join(home, '.gemini', 'antigravity-cli', 'conversations'))))
    note('model ids', [bridge.model_id_for(b, e) for b, e in (('gemini-4-argon', 'high'), ('gemini-4-argon', 'xhigh'),
                                                              ('gemini-4-hydrogen', 'low'), ('gpt-oss-120b-medium', 'high'),
                                                              ('claude-opus-5-5', None), ('unknown-model', 'high'), (None, 'high'))])

    # ---- POST: settings ----
    call('POST', '/launch', {'appendSystemPrompt': 'be brief'})
    call('POST', '/launch', {'extraArgs': "'unclosed"})
    call('POST', '/launch', {'extraArgs': '--add-dir ~/notes --conversation x'})
    call('POST', '/launch', {'extraArgs': "- You run inside the app; use `rish -c '<command>'`"})
    call('POST', '/launch', {'extraArgs': '--add-dir ~/notes --sandbox'})
    note('launch.args()', bridge.launch.args())
    call('POST', '/launch', {'extraArgs': ''})
    call('POST', '/memory', {'scope': 'global', 'text': 'remember me'})
    call('POST', '/memory', {'scope': 'folder', 'cwd': os.path.join(tmp, 'f'), 'text': 'folder text'})
    call('POST', '/memory', {'scope': 'bogus', 'text': 'x'})
    call('GET', '/memory?scope=global')
    call('POST', '/mkdir', {'path': os.path.join(tmp, 'new', 'dir')})
    call('POST', '/mkdir', {'path': os.path.join(tmp, 'bin', 'agy', 'x')})
    call('POST', '/mkdir', {})
    call('POST', '/nothing', {})
    call('POST', '/send', raw=b'{not json')
    call('POST', '/send', {})
    call('POST', '/stop', {'chat': 'unknown'})
    call('POST', '/cancel', {'chat': 'unknown', 'uuid': 'x'})
    call('POST', '/permission', {'chat': 'unknown', 'requestId': 'r1', 'allow': True})
    call('POST', '/context', {'chat': 'unknown'})
    call('POST', '/open', {'cwd': os.path.join(tmp, 'work2')})
    call('POST', '/rewind', {'chat': 'c1', 'sessionId': 'x', 'uuid': 'y'})
    call('POST', '/delete-message', {'chat': 'c1', 'sessionId': 'x', 'uuid': 'y'})

    # ---- upload ----
    for name in ('doc.txt', 'doc.txt', '../evil.txt'):
        status, data = client.call('POST', '/upload?cwd=%s&name=%s' % (os.path.join(tmp, 'up'), name), raw=b'hello')
        records.append({'req': 'POST /upload name=%s' % name, 'status': status, 'body': norm(data)})
    note('upload files', sorted(os.listdir(os.path.join(tmp, 'up'))))

    # ---- a conversation: a tool asked about and allowed, a picture; another turn on another model ----
    call('POST', '/send', {'chat': 'c1', 'cwd': w, 'content': [
        {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'iVBORw0KGgo='}},
        {'type': 'text', 'text': 'use a tool'}], 'mid': 'm1'})
    call('POST', '/send', {'chat': 'c1', 'content': 'use a tool', 'mid': 'm1'})  # repeated
    wait_for(lambda: asked('c1'))
    sid = bridge.chats['c1'].session_id
    call('POST', '/permission', {'chat': 'c1', 'sessionId': sid, 'requestId': asked('c1'), 'allow': True})
    call('POST', '/permission', {'chat': 'c1', 'sessionId': sid, 'requestId': asked('c1'), 'allow': True})  # answered already
    settle('c1', 1)
    call('POST', '/send', {'chat': 'c1', 'sessionId': sid, 'cwd': w, 'content': [{'type': 'text', 'text': 'make a plan'}], 'mid': 'm2',
                           'model': 'claude-opus-5-5', 'effort': 'medium', 'mode': 'plan'})
    settle('c1', 2)
    call('POST', '/context', {'chat': 'c1', 'sessionId': sid})
    call('POST', '/send', {'chat': 'c1', 'sessionId': sid, 'content': [{'type': 'text', 'text': '/name Tools talk'}], 'mid': 'm3'})
    settle('c1', 3)
    keep_events('c1', 'a conversation')
    call('GET', '/history?id=' + sid)
    call('GET', '/image?id=%s&at=0&n=0' % sid)
    call('GET', '/sessions')
    call('GET', '/search?q=plan')
    call('GET', '/search?q=')

    # ---- written while a reply runs: taken into it; then a stop ----
    call('POST', '/send', {'chat': 'c2', 'cwd': w, 'content': [{'type': 'text', 'text': 'slow'}], 'mid': 'n1'})
    wait_for(lambda: bridge.chats['c2'].prompts, 10)
    call('POST', '/send', {'chat': 'c2', 'cwd': w, 'content': [{'type': 'text', 'text': 'inserted one'}], 'mid': 'n2', 'uuid': 'U1'})
    time.sleep(0.3)
    call('POST', '/cancel', {'chat': 'c2', 'uuid': 'U1'})
    settle('c2', 1)
    sid2 = bridge.chats['c2'].session_id
    call('POST', '/send', {'chat': 'c2', 'sessionId': sid2, 'content': [{'type': 'text', 'text': 'slow again'}], 'mid': 'n4'})
    wait_for(lambda: bridge.chats['c2'].prompts, 10)
    call('POST', '/stop', {'chat': 'c2', 'sessionId': sid2})
    settle('c2', 2)
    call('POST', '/stop', {'chat': 'c2', 'sessionId': sid2})  # nothing runs
    keep_events('c2', 'inserting, stopping')

    # ---- what fails: the model's error said in the reply, a model agy does not know, a tool denied ----
    call('POST', '/send', {'chat': 'c3', 'cwd': w, 'content': [{'type': 'text', 'text': 'fail'}], 'mid': 'f1'})
    settle('c3', 1)
    sid3 = bridge.chats['c3'].session_id
    call('POST', '/send', {'chat': 'c3', 'sessionId': sid3, 'content': [{'type': 'text', 'text': 'hi'}], 'mid': 'f2', 'model': 'nope'})
    settle('c3', 2)
    call('POST', '/send', {'chat': 'c3', 'sessionId': sid3, 'content': [{'type': 'text', 'text': 'tool please'}], 'mid': 'f3'})
    wait_for(lambda: asked('c3'))
    call('POST', '/permission', {'chat': 'c3', 'sessionId': sid3, 'requestId': asked('c3'), 'allow': False})
    settle('c3', 3)
    keep_events('c3', 'failures')

    # ---- 全部允许: the bridge says yes itself ----
    call('POST', '/send', {'chat': 'c4', 'cwd': w, 'content': [{'type': 'text', 'text': 'tool, no questions'}], 'mid': 'b1',
                           'mode': 'bypassPermissions'})
    settle('c4', 1)
    keep_events('c4', 'everything allowed')

    # ---- agy ends: a conversation is read again (its copy kept here, then agy's replay of it) ----
    bridge.agy.stop('test')
    time.sleep(0.3)
    call('GET', '/history?id=' + sid)
    os.remove(bridge.history_path(sid))
    call('GET', '/history?id=' + sid)
    call('POST', '/send', {'chat': 'c1b', 'sessionId': sid, 'content': [{'type': 'text', 'text': 'again'}], 'mid': 'm9'})
    settle('c1b', 1)
    keep_events('c1b', 'a conversation opened again')

    # ---- names, deleting ----
    call('POST', '/title', {'id': sid2, 'title': '  A   new   name '})
    call('POST', '/title', {'id': sid2, 'title': '   '})
    call('POST', '/title', {'id': '../x', 'title': 'x'})
    call('GET', '/sessions')
    call('POST', '/delete', {'id': sid2})
    call('POST', '/delete', {'id': 'nonexistent'})
    call('POST', '/delete', {})
    call('GET', '/sessions')
    note('files of the deleted conversation', sorted(n for n in os.listdir(os.path.join(home, '.gemini', 'antigravity-cli', 'conversations'))
                                                     if sid2 in n))

    # ---- a Gemini API key; signing out ----
    call('POST', '/auth', {'provider': 'gemini', 'key': 'AIza-1'})
    note('key file', [oct(os.stat(bridge.KEY_FILE).st_mode & 0o777), bridge.read_json(bridge.AGY_SETTINGS, {})])
    call('POST', '/auth', {'provider': 'openai', 'key': 'x'})
    call('POST', '/auth', {'provider': 'gemini', 'key': ''})
    note('settings after the key', bridge.read_json(bridge.AGY_SETTINGS, {}))
    call('POST', '/logout', {})
    call('GET', '/account')

    # ---- end ----
    bridge.agy.stop('end of test')
    server.shutdown()

    result_ = {'records': records}
    if record:
        with open(GOLDEN, 'w', encoding='utf-8') as f:
            json.dump(result_, f, ensure_ascii=False, indent=1, sort_keys=True)
        print('recorded %d requests' % len(records))
        return 0
    with open(GOLDEN, encoding='utf-8') as f:
        golden = json.load(f)
    got = json.loads(json.dumps(result_, sort_keys=True))
    bad = 0
    for i, (g, r) in enumerate(zip(golden['records'], got['records'])):
        if g != r:
            bad += 1
            print('DIFF #%d %s\n  golden: %s\n  now:    %s' % (i, g['req'], json.dumps(g, ensure_ascii=False)[:600],
                                                             json.dumps(r, ensure_ascii=False)[:600]))
    if len(golden['records']) != len(got['records']):
        bad += 1
        print('DIFF: %d records, golden has %d' % (len(got['records']), len(golden['records'])))
    print('%d requests, %d different' % (len(got['records']), bad))
    return 1 if bad else 0


if __name__ == '__main__':
    code = main()
    sys.stdout.flush()
    os._exit(code)
