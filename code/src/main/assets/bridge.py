"""Antigravity bridge for the Antigravity app.

Runs inside the app's own Linux userland (Alpine under proot, started by the app's foreground
service) and lets the app talk to Google's Antigravity CLI (`agy`): one `agy acp` process (the Agent
Client Protocol, JSON-RPC over its stdin and stdout) holds every conversation as one of its sessions.
What agy says of a session (session/update) is handed to the app as the stream-json messages its
conversation view reads (Chat.apply), so the app shows agy's replies, thinking and tool steps as it
did Claude Code's; agy asking whether a tool may run comes to the app as Claude Code's control_request.
Listens on a Unix socket in the app's own files (SOCKET_FILE): no other app can reach it, so
requests need no token.

Standard library only.
"""
import argparse
import hashlib
import json
import os
import queue
import re
import select
import shlex
import shutil
import signal
import socket
import socketserver
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, unquote, urlparse

VERSION = 11  # Bridge VERSION in the app: an app update replaces an older running bridge
HOME = os.path.expanduser('~')
GEMINI_DIR = os.path.join(HOME, '.gemini')
AGY_DIR = os.environ.get('AGY_DATA_DIR') or os.path.join(GEMINI_DIR, 'antigravity-cli')
STATE_DIR = os.path.join(HOME, '.agyapp')
PID_FILE = os.path.join(STATE_DIR, 'bridge.pid')
LOCK_FILE = os.path.join(STATE_DIR, 'bridge.lock')
SOCKET_FILE = os.path.join(STATE_DIR, 'bridge.sock')  # Bridge.SOCKET in the app
DEFAULT_CWD = os.path.join(HOME, 'work')
LOG_FILE = os.path.join(STATE_DIR, 'bridge.log')
LAUNCH_FILE = os.path.join(STATE_DIR, 'launch.json')
TZ_FILE = os.path.join(STATE_DIR, 'timezone')  # the phone's time zone, kept by the app (BridgeService)
MODELS_FILE = os.path.join(STATE_DIR, 'models.json')  # agy's models as it last listed them
INFO_FILE = os.path.join(STATE_DIR, 'agy-info.json')
TITLES_FILE = os.path.join(STATE_DIR, 'titles.json')  # names given in the app: session id -> name
HIDDEN_FILE = os.path.join(STATE_DIR, 'deleted.json')  # sessions deleted in the app
ACCOUNT_FILE = os.path.join(STATE_DIR, 'account.json')  # who signed in from the app
ACCOUNTS_DIR = os.path.join(STATE_DIR, 'accounts')  # saved Google tokens for one-tap switching (mode 0700)
ACCOUNTS_INDEX = os.path.join(ACCOUNTS_DIR, 'index.json')  # saved Google account emails in order (mode 0600)
KEY_FILE = os.path.join(STATE_DIR, 'gemini-api-key')  # a Gemini API key given in the app (mode 0600)
HISTORY_DIR = os.path.join(STATE_DIR, 'history')  # each session as the app shows it, kept for the next read
IMAGES_DIR = os.path.join(STATE_DIR, 'images')  # pictures sent from the app (agy replays no pictures)
VOICE_DIR = os.path.join(STATE_DIR, 'voice')  # isolated working folder for agy's dictation TUI
TOKEN_FILE = os.path.join(AGY_DIR, 'antigravity-oauth-token')
SUMMARIES_DB = os.path.join(AGY_DIR, 'conversation_summaries.db')
AGY_SETTINGS = os.path.join(AGY_DIR, 'settings.json')
STARTED = time.time()  # tells the app a new bridge took over (turns of the old one are gone)
EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max']
# The app's permission modes: agy's own (session/set_mode), and 全部允许, the bridge answering yes itself.
BYPASS = 'bypassPermissions'
APP_MODES = {'default': 'default', 'acceptEdits': 'accept-edits', 'auto': 'auto', 'plan': 'plan'}
IDLE_STOP = 3 * 60 * 60  # agy (a few hundred MB) is stopped after this long unused; started again when needed
VOICE_IDLE_STOP = 15 * 60  # dictation agy TUI is stopped after this long unused
AUTO_SETTLE_TEXT = 3.0  # quiet seconds after reply text before a reactive wakeup turn ends
AUTO_SETTLE_IDLE = 45.0  # fallback quiet seconds when a reactive wakeup has no reply text yet
SHORT_TITLE = 60  # characters of a first message shown as its conversation's name
ERROR_MARK = '\u26a0'  # agy says a failed request in the reply's text, after this sign


_json_cache = {}  # path -> (mtime_ns, size, data)
_json_cache_lock = threading.Lock()


def read_json(path, default):
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


def read_json_cached(path, default):
    """Cached read_json for small read-only state files; keyed by (mtime_ns, size)."""
    try:
        st = os.stat(path)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        with _json_cache_lock:
            _json_cache.pop(path, None)
        return default
    with _json_cache_lock:
        hit = _json_cache.get(path)
        if hit is not None and hit[0] == key:
            data = hit[1]
            return data if isinstance(data, type(default)) else default
    data = read_json(path, default)
    with _json_cache_lock:
        _json_cache[path] = (key, data)
    return data


state_lock = threading.Lock()
log_lock = threading.Lock()


def write_json(path, data, mode=None, indent=2):
    """Written aside, then moved in: a write cut short leaves the last one whole."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    part = '%s.%d.%s.tmp' % (path, os.getpid(), uuid.uuid4().hex[:8])
    try:
        with open(part, 'w', encoding='utf-8', errors='replace') as f:
            if indent is None:
                json.dump(data, f, ensure_ascii=False, separators=(',', ':'))
            else:
                json.dump(data, f, ensure_ascii=False, indent=indent)
        if mode is not None:
            os.chmod(part, mode)
        os.replace(part, path)
        with _json_cache_lock:
            _json_cache.pop(path, None)
    except Exception:
        try:
            os.remove(part)
        except OSError:
            pass
        raise


def safe_id(session_id):
    return isinstance(session_id, str) and session_id not in ('.', '..') \
        and not session_id.startswith('.') and re.fullmatch(r'[\w.-]{1,80}', session_id) is not None


class Hub:
    """Numbered events for the app; a reconnecting client asks for those after its last one."""

    def __init__(self, keep=4000):
        self.events = []
        self.seq = 0
        self.keep = keep
        self.cond = threading.Condition()

    def emit(self, chat, kind, data=None):
        with self.cond:
            self.seq += 1
            self.events.append({'seq': self.seq, 'chat': chat, 'kind': kind, 'data': data})
            if len(self.events) > self.keep + 512:
                del self.events[:len(self.events) - self.keep]
            self.cond.notify_all()

    def oldest(self):
        """The first event still kept; seq + 1 when none is."""
        with self.cond:
            return self.events[0]['seq'] if self.events else self.seq + 1

    def after(self, seq, timeout):
        with self.cond:
            if self.seq <= seq:
                self.cond.wait(timeout)
            if not self.events:
                return []
            # Numbers follow one another: the first one after seq is found by its place.
            return self.events[max(0, seq + 1 - self.events[0]['seq']):]


hub = Hub()


# agy's options that say how it talks or which conversation runs: the bridge's own to give.
OWN_OPTIONS = ('-p', '--print', '--prompt', '-i', '--prompt-interactive', '-c', '--continue', '--conversation',
               '--input-format', '--output-format', '--login', '--stdio', '--process-per-session', '-h', '--help',
               '--app-data-dir', '--app_data_dir', '--gemini-dir', '--gemini_dir', '--cli_dir_name')


def extra_args(text):
    """The extra arguments set in the app, as `agy acp` gets them. ValueError, in words for the user,
    when they are not arguments (bad quoting, or text beginning with no option: instructions for the
    model written here) or say how agy talks to the bridge."""
    args = shlex.split(text)
    if args and not re.match(r'^--?[A-Za-z]', args[0]):
        raise ValueError('启动参数要以选项开头（如 --add-dir ~/notes）；给模型的说明请写在「系统提示」里')
    for arg in args:
        if arg.split('=', 1)[0] in OWN_OPTIONS:
            raise ValueError('启动参数里不能有 %s：怎么和 agy 交流由 App 决定' % arg.split('=', 1)[0])
    return [os.path.expanduser(a) for a in args]


class Launch:
    """How agy is started, set in the app: text appended to the system prompt (each new session gets
    it) and extra arguments of `agy acp` (agy is started again with them once no reply runs)."""

    def __init__(self):
        self.gen = 0
        self.data = {'appendSystemPrompt': '', 'extraArgs': ''}
        self.data.update({k: v for k, v in read_json(LAUNCH_FILE, {}).items() if k in self.data and isinstance(v, str)})

    def set(self, data):
        new = dict(self.data)
        for key in self.data:
            if key in data:
                new[key] = str(data[key] or '')
        if new['extraArgs'] != self.data['extraArgs']:
            extra_args(new['extraArgs'])
        write_json(LAUNCH_FILE, new)
        self.data = new
        self.gen += 1
        agy.restart_when_idle('launch settings changed')

    def args(self):
        try:
            return extra_args(self.data['extraArgs'])
        except ValueError:
            return []  # saved before they were checked: left out whole (view says so)

    def view(self):
        try:
            extra_args(self.data['extraArgs'])
        except ValueError as e:
            return dict(self.data, extraArgsUnused=str(e))
        return self.data


def memory_path(scope, cwd=None):
    """AGENTS.md the app edits: the user's own (every conversation, ~/.gemini) or a working folder's."""
    if scope == 'global':
        return os.path.join(GEMINI_DIR, 'AGENTS.md')
    if scope == 'folder' and cwd:
        return os.path.join(os.path.expanduser(cwd), 'AGENTS.md')
    raise ValueError('unknown scope')


def agy_path():
    return shutil.which('agy') or '/usr/local/bin/agy'


def agy_env():
    """agy's environment: the bridge's, the phone's time zone in TZ (the userland has no
    /etc/localtime; none when the app has told no zone the userland has) and a Gemini API key given
    in the app."""
    env = dict(os.environ)
    try:
        with open(TZ_FILE, encoding='utf-8') as f:
            tz = f.read().strip()
        if re.fullmatch(r'[\w+-]+(/[\w+-]+)*', tz, re.ASCII) and os.path.isfile('/usr/share/zoneinfo/' + tz):
            env['TZ'] = tz
    except OSError:
        pass
    key = gemini_key()
    if key:
        env['GEMINI_API_KEY'] = key
    return env


def gemini_key():
    try:
        with open(KEY_FILE, encoding='utf-8') as f:
            return f.read().strip()
    except OSError:
        return ''


def agy_settings():
    return read_json_cached(AGY_SETTINGS, {})


# ---- agy's models: "gemini-4-argon-high" is Gemini 4 Argon thinking high ----

def split_model(model_id, name=''):
    """A model id of agy's list as (base, effort): ('gemini-4-argon', 'high') for gemini-4-argon-high
    named "Gemini 4 Argon (High)"; (id, None) for one with no thinking level of its own."""
    m = re.match(r'^(.*)-(low|medium|high|xhigh|max)$', model_id or '')
    if m and (not name or re.search(r'\((low|medium|high|xhigh|max)\)\s*$', name, re.I)):
        return m.group(1), m.group(2)
    return model_id, None


def provider_of(base):
    b = base.lower()
    if b.startswith('claude'):
        return 'Anthropic'
    if b.startswith('gpt'):
        return 'OpenAI'
    return 'Google'


def group_models(listed):
    """agy's list [(id, name)] as the app's: one entry per model (value: its base id), with its levels."""
    out, by_base = [], {}
    for mid, name in listed:
        base, effort = split_model(mid, name)
        if base not in by_base:
            shown = re.sub(r'\s*\((low|medium|high|xhigh|max)\)\s*$', '', name or base, flags=re.I) if effort else (name or base)
            by_base[base] = {'value': base, 'displayName': shown, 'description': '%s · %s' % (shown, provider_of(base)),
                             'provider': provider_of(base), 'efforts': [], 'ids': {}}
            out.append(by_base[base])
        entry = by_base[base]
        if effort:
            if effort not in entry['efforts']:
                entry['efforts'].append(effort)
            entry['ids'][effort] = mid
        else:
            entry['ids'][''] = mid
    for entry in out:
        entry['efforts'].sort(key=EFFORTS.index)
    return out


def model_id_for(base, effort):
    """The id agy knows for this model and thinking level: the level asked for when the model has
    it, else the nearest it has (medium first); the base itself when it is not listed."""
    if not base:
        return None
    entry = next((m for m in read_json_cached(MODELS_FILE, {}).get('models', []) if m.get('value') == base), None)
    if entry is None:
        return base if not effort or split_model(base)[1] else '%s-%s' % (base, effort)
    ids = entry.get('ids') or {}
    if effort in ids:
        return ids[effort]
    if '' in ids:
        return ids['']
    for e in ['medium', 'high', 'low', 'xhigh', 'max']:
        if e in ids:
            return ids[e]
    return base


def models_stamp():
    """What agy's models depend on: agy itself and how it is signed in."""
    return file_stamp(os.path.realpath(agy_path()), TOKEN_FILE, ACCOUNT_FILE, AGY_SETTINGS, KEY_FILE)


def keep_models(models_block):
    """Keeps agy's model list from a session's answer (session/new, session/load)."""
    if not isinstance(models_block, dict):
        return
    listed = [(m.get('modelId'), m.get('name') or m.get('modelId')) for m in models_block.get('availableModels') or []
              if isinstance(m, dict) and m.get('modelId')]
    if not listed:
        return
    current = models_block.get('currentModelId') or ''
    models = group_models(listed)
    changed = False
    with state_lock:
        saved = read_json(MODELS_FILE, {})
        data = {'models': models, 'current': current or saved.get('current', ''), 'stamp': models_stamp()}
        if data != saved:
            try:
                write_json(MODELS_FILE, data)
            except OSError:
                pass
            changed = [m['value'] for m in models] != [m.get('value') for m in saved.get('models', [])]
    if changed:
        hub.emit('', 'config')


models_lock = threading.Lock()
models_tried = 0.0  # when agy's models were last asked for and not given


def reset_models_tried():
    global models_tried
    models_tried = 0.0


def find_models():
    """agy's models, asked again when agy or its sign-in changes (kept in MODELS_FILE). agy gives
    them only with a session (`agy models` wants the terminal's own sign-in, not the app's): one is
    made for it, read, and deleted at once."""
    global models_tried
    if not models_lock.acquire(blocking=False):
        return
    sid = None
    try:
        saved = read_json(MODELS_FILE, {})
        if (saved.get('models') and saved.get('stamp') == models_stamp()) or not os.path.exists(agy_path()) \
                or not signed_in() or (agy.alive() and agy.restart_reason) or time.time() - models_tried < 60:
            return
        agy.ensure()
        os.makedirs(DEFAULT_CWD, exist_ok=True)
        result = agy.request('session/new', {'cwd': DEFAULT_CWD, 'mcpServers': []}, timeout=90) or {}
        sid = result.get('sessionId')
        keep_models(result.get('models'))
        if sid:
            # Close first as a round-trip barrier so any available_commands_update emitted right
            # after session/new has arrived in agy.early before we pop it.
            try:
                agy.request('session/close', {'sessionId': sid}, timeout=10)
            except (AgyError, RpcError):
                pass
            with agy.lock:
                early = agy.early.pop(sid, [])
            for u in early:
                if u.get('sessionUpdate') == 'available_commands_update':
                    remember_commands([c.get('name') for c in u.get('availableCommands') or [] if isinstance(c, dict) and c.get('name')])
            remove_session_files(sid, remember=False)
            sid = None
        models = read_json(MODELS_FILE, {}).get('models', [])
        log('models: %d' % len(models))
        if not models:
            models_tried = time.time()
        hub.emit('', 'config')
    except (AgyError, RpcError, OSError) as e:
        models_tried = time.time()
        log('models: not read: %s' % e)
    finally:
        if sid:
            try:
                agy.request('session/close', {'sessionId': sid}, timeout=10)
            except (AgyError, RpcError):
                pass
            remove_session_files(sid, remember=False)
        models_lock.release()


# ---- The agy process ----

class RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class AgyError(Exception):
    """agy could not be started or did not answer: in words for the user."""


def friendly(message):
    """An error of agy's, with what to do about it."""
    text = (message or '').strip()
    if re.search(r'authentication required|not authenticated|unauthenticated|login required', text, re.I):
        return '还没有登录：到「设置 → 账号」用 Google 登录，或填 Gemini API Key'
    if 'location is not supported' in text:
        return text + '\n\n这个模型不对你的账号或网络所在地区开放（Google 的地区限制）。可以换一个模型（比如 Claude），或者换一个网络出口。'
    if re.search(r'RESOURCE_EXHAUSTED|quota', text, re.I):
        return text + '\n\n额度用完了：稍后再试，或换一个模型。'
    return text


class Agy:
    """The one `agy acp` process every conversation is a session of: started when needed, started
    again when it ends (a session is then loaded again in the new one), JSON-RPC both ways."""

    def __init__(self):
        self.lock = threading.Lock()
        self.init_lock = threading.Lock()  # one start and initialize at a time
        self.write_lock = threading.Lock()
        self.proc = None
        self.started_at = 0.0
        self.used = time.time()  # when a conversation last needed agy: it is stopped after a long rest
        self.gen = 0  # counts processes started: a session is loaded in one of them
        self.ready = False
        self.pending = {}  # request id -> slot {'event', 'result', 'error'}
        self.next = 0
        self.info = {}
        self.stderr = []
        self.failures = 0
        self.failed_gen = -1
        self.retry_at = 0.0
        self.restart_reason = None  # started again once no reply runs
        self.early = {}  # session id -> updates that came before a conversation took the session (session/new's)
        self.args_used = None
        self.last_error = ''
        self.primed_gen = -1  # the process (gen) that has made or loaded a session: its first one is the slow one

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def ensure(self, timeout=90):
        """agy, started and initialized; AgyError when it cannot be."""
        self.used = time.time()
        with self.init_lock:
            with self.lock:
                if self.alive() and self.ready:
                    return
                if not os.path.exists(agy_path()):
                    raise AgyError('agy 还没装好：到「设置 → 关于」查看，或在终端里运行 agy-update')
                if not self.alive():
                    if time.time() < self.retry_at:
                        raise AgyError('agy 刚才没能启动，稍后再试' + ('：' + self.last_error if self.last_error else ''))
                    self.start()
                proc, gen = self.proc, self.gen
            try:
                result = self.request('initialize', {
                    'protocolVersion': 1,
                    'clientCapabilities': {'fs': {'readTextFile': False, 'writeTextFile': False}, 'terminal': False},
                    'clientInfo': {'name': 'antigravity-android', 'version': str(VERSION)}}, timeout=timeout)
            except RpcError as e:
                if self.proc is proc:
                    self.stop('initialize refused')
                raise AgyError('agy 没有接受连接：%s' % e)
            except AgyError:
                with self.lock:
                    if self.failed_gen != gen:
                        self.failed_gen = gen
                        self.failures += 1
                        self.retry_at = time.time() + min(300, 5 * 2 ** self.failures)
                if self.proc is proc:
                    self.stop('initialize failed')
                raise
            with self.lock:
                if self.proc is proc and self.gen == gen:
                    self.info = result if isinstance(result, dict) else {}
                    self.ready = True
                    self.failures = 0
                    log('agy pid %d ready (%s %s)' % (proc.pid, (self.info.get('agentInfo') or {}).get('name', 'agy'),
                                                     (self.info.get('agentInfo') or {}).get('version', '')))

    def start(self):
        """Starts a new agy (the caller holds the lock)."""
        args = [agy_path(), 'acp'] + launch.args()
        stderr_buf = []
        self.stderr = stderr_buf
        self.gen += 1
        self.ready = False
        try:
            os.makedirs(DEFAULT_CWD, exist_ok=True)
            # Binary pipes: records are split on LF only (a text reader would also split on U+2028 inside JSON).
            self.proc = subprocess.Popen(args, cwd=DEFAULT_CWD, env=agy_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, start_new_session=True)
        except OSError as e:
            self.proc = None
            self.last_error = str(e)[:200]
            self.failed_gen = self.gen
            self.failures += 1
            self.retry_at = time.time() + min(300, 5 * 2 ** self.failures)
            raise AgyError('agy 启动失败：%s' % e)
        self.args_used = (launch.gen, args)
        self.started_at = time.time()
        self.restart_reason = None
        gen, proc = self.gen, self.proc
        threading.Thread(target=self.read_out, args=(proc, gen, stderr_buf), daemon=True).start()
        threading.Thread(target=self.read_err, args=(proc, stderr_buf), daemon=True).start()
        log('agy pid %d started: %s' % (proc.pid, ' '.join(shlex.quote(a) for a in args[1:])))

    def read_err(self, proc, stderr_buf):
        for raw in proc.stderr:
            line = raw.decode('utf-8', 'replace').rstrip()
            with self.lock:
                stderr_buf.append(line)
                del stderr_buf[:-50]

    def read_out(self, proc, gen, stderr_buf):
        for raw in proc.stdout:
            line = raw.decode('utf-8', 'replace').strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            try:
                self.dispatch(msg)
            except Exception:  # one odd record must not end the reading of the rest
                import traceback
                log('agy record not understood:\n%s' % traceback.format_exc())
        code = proc.wait()
        with self.lock:
            tail = '\n  '.join(stderr_buf[-12:])
            if self.proc is proc:
                self.proc = None
                self.ready = False
                self.last_error = (stderr_buf[-1] if stderr_buf else '')[:200]
                if time.time() - self.started_at < 30 and self.failed_gen != gen:
                    self.failed_gen = gen
                    self.failures += 1
                    self.retry_at = time.time() + min(300, 5 * 2 ** self.failures)
            waiting = [self.pending.pop(k) for k in list(self.pending) if self.pending[k].get('gen') == gen]
        log('agy pid %d exited with %s%s' % (proc.pid, code, ', stderr:\n  ' + tail if tail else ''))
        for slot in waiting:
            slot['error'] = AgyError('agy 退出了（代码 %s）' % code)
            slot['event'].set()
        for chat in all_chats():
            chat.agy_gone(gen, code, tail)

    def dispatch(self, msg):
        if 'method' in msg and 'id' in msg:  # agy asks something
            self.asked(msg)
        elif 'method' in msg:
            if msg['method'] == 'session/update':
                params = msg.get('params') or {}
                sid = params.get('sessionId')
                chat = chat_of_session(sid)
                if chat is not None:
                    chat.update(params.get('update') or {})
                elif sid:  # agy tells a new session's commands as it answers: kept until it is taken
                    with self.lock:
                        self.early.setdefault(sid, []).append(params.get('update') or {})
                        for old in list(self.early)[:-8]:
                            del self.early[old]
        elif 'id' in msg:
            with self.lock:
                slot = self.pending.pop(msg['id'], None)
            if slot is None:
                return
            if 'error' in msg:
                err = msg.get('error') or {}
                slot['error'] = RpcError(err.get('code'), str(err.get('message') or err))
            else:
                slot['result'] = msg.get('result')
            slot['event'].set()
            if slot.get('then'):
                threading.Thread(target=slot['then'], args=(slot,), daemon=True).start()

    def asked(self, msg):
        method, params = msg.get('method'), msg.get('params') or {}
        if method == 'session/request_permission':
            chat = chat_of_session(params.get('sessionId'))
            if chat is not None:
                chat.permission(msg['id'], params)
                return
            self.respond(msg['id'], {'outcome': {'outcome': 'cancelled'}})
        else:
            # The bridge offers agy nothing of the client's own (files, terminals).
            self.write({'jsonrpc': '2.0', 'id': msg['id'], 'error': {'code': -32601, 'message': 'not supported: %s' % method}})

    def write(self, message, proc=None):
        proc = self.proc if proc is None else proc
        if proc is None or proc.poll() is not None:
            raise AgyError('agy 没在运行')
        data = json.dumps(message, ensure_ascii=False).encode('utf-8', 'replace') + b'\n'
        with self.write_lock:
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
            except (OSError, ValueError, RuntimeError):
                raise AgyError('agy 没在运行')

    def respond(self, rid, result):
        try:
            self.write({'jsonrpc': '2.0', 'id': rid, 'result': result})
        except AgyError:
            pass

    def notify(self, method, params):
        try:
            self.write({'jsonrpc': '2.0', 'method': method, 'params': params})
        except AgyError:
            pass

    def call(self, method, params, then=None):
        """Sends a request; its slot, whose event is set when the answer comes (then is run with it)."""
        with self.lock:
            proc = self.proc
            self.next += 1
            rid = self.next
            slot = {'event': threading.Event(), 'gen': self.gen, 'id': rid, 'then': then}
            self.pending[rid] = slot
        try:
            self.write({'jsonrpc': '2.0', 'id': rid, 'method': method, 'params': params}, proc=proc)
        except Exception as e:
            with self.lock:
                self.pending.pop(rid, None)
            slot['error'] = e if isinstance(e, AgyError) else AgyError('发送请求失败：%s' % e)
            slot['event'].set()
            if then:
                threading.Thread(target=then, args=(slot,), daemon=True).start()
        return slot

    def request(self, method, params, timeout=60):
        gen = self.gen
        slot = self.call(method, params)
        if not slot['event'].wait(timeout):
            with self.lock:
                self.pending.pop(slot['id'], None)
            raise AgyError('agy %d 秒没有回应（%s）' % (timeout, method))
        if slot.get('error') is not None:
            raise slot['error']
        if method in ('session/new', 'session/load'):
            self.primed_gen = gen
        return slot.get('result')

    def busy(self):
        with self.lock:
            return bool(self.pending) or self.init_lock.locked()

    def stop(self, reason):
        with self.lock:
            proc, self.proc = self.proc, None
            self.ready = False
        if proc is not None:
            log('agy pid %d stopped: %s' % (proc.pid, reason))
            end(proc, self.write_lock)

    def restart_when_idle(self, reason):
        """Started again (with new settings, a new sign-in) once no reply runs; at once if none does."""
        if not self.alive():
            return
        self.restart_reason = reason
        if not any(c.running for c in all_chats()) and not self.busy():
            self.stop(reason)


def end(proc, write_lock=None):
    """Ends a process: end of input (it then shuts down in order), then SIGTERM, then SIGKILL;
    returns once it is gone."""
    if proc is None:
        return
    try:
        if write_lock is not None:
            with write_lock:
                proc.stdin.close()
        else:
            proc.stdin.close()
    except (OSError, ValueError, RuntimeError):
        pass
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            proc.wait(4)
            return
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, sig)
            except OSError:
                try:
                    proc.send_signal(sig)
                except OSError:
                    pass
    proc.wait()


launch = Launch()
agy = Agy()


# ---- What agy's tool steps are, as the app shows Claude Code's ----

def first_diff(update):
    for c in update.get('content') or []:
        if isinstance(c, dict) and c.get('type') == 'diff':
            return c
    return None


def tool_view(u):
    """An ACP tool call as the app knows tools: (name, input). Commands are Bash, files read are
    Read, edits Edit (Write for a new file), searches Grep, fetches WebFetch; others keep agy's name."""
    kind, name = u.get('kind') or '', u.get('name') or ''
    args = u.get('rawInput') if isinstance(u.get('rawInput'), dict) else u.get('args') if isinstance(u.get('args'), dict) else {}
    title = u.get('title') or ''
    paths = [loc.get('path') for loc in u.get('locations') or [] if isinstance(loc, dict) and loc.get('path')]
    path = paths[0] if paths else next((args.get(k) for k in ('Path', 'AbsolutePath', 'TargetFile', 'File', 'FilePath')
                                        if isinstance(args.get(k), str)), '')
    if kind == 'execute' or name == 'run_command':
        return 'Bash', {'command': args.get('CommandLine') or args.get('Command') or title,
                        'description': args.get('toolSummary') or ''}
    if kind == 'read' and path:
        return 'Read', {'file_path': path}
    if kind == 'edit':
        diff = first_diff(u)
        if diff is not None and not diff.get('oldText'):
            return 'Write', {'file_path': diff.get('path') or path, 'content': diff.get('newText') or ''}
        if diff is not None:
            return 'Edit', {'file_path': diff.get('path') or path, 'old_string': diff.get('oldText') or '',
                            'new_string': diff.get('newText') or ''}
        if name in ('write_to_file', 'create_file'):
            return 'Write', {'file_path': path, 'content': args.get('CodeContent') or args.get('Content') or ''}
        return 'Edit', {'file_path': path, 'old_string': args.get('Target') or '', 'new_string': args.get('Replacement') or ''}
    if kind == 'search':
        query = next((args.get(k) for k in ('Query', 'query', 'Pattern', 'SearchPattern') if isinstance(args.get(k), str)), '')
        if name == 'search_web' or (not path and ('query' in args or title.endswith(' on Web'))):
            clean = query or re.sub(r'^"(.*)" on Web$', r'\1', title)
            return 'WebSearch', {'query': clean}
        return 'Grep', {'pattern': query or title, 'path': path}
    if kind == 'fetch':
        url = next((args.get(k) for k in ('Url', 'URL', 'url') if isinstance(args.get(k), str)), '')
        if url:
            return 'WebFetch', {'url': url}
        return 'WebSearch', {'query': args.get('Query') or args.get('query') or title}
    shown = dict(args)
    if title and not shown:
        shown['title'] = title
    return name or title or 'tool', shown


def tool_result(u):
    """What a finished tool call gave back: (text, images [(mime, data)])."""
    texts, images = [], []
    for c in u.get('content') or []:
        if not isinstance(c, dict):
            continue
        if c.get('type') == 'content':
            inner = c.get('content') or {}
            if inner.get('type') == 'text':
                texts.append(inner.get('text') or '')
            elif inner.get('type') == 'image' and inner.get('data'):
                images.append((inner.get('mimeType') or 'image/png', inner['data']))
        elif c.get('type') == 'terminal':
            texts.append('[终端 %s]' % c.get('terminalId'))
    text = '\n'.join(texts)
    if not text and isinstance(u.get('rawOutput'), dict):
        out = u['rawOutput'].get('output')
        text = out if isinstance(out, str) else ''
    # A command's output: without agy's lines on when it ran.
    m = re.match(r'^(?:Created At:[^\n]*\n)?(?:Completed At:[^\n]*\n)?(Command exited with code (\d+)\.\n)?\n?Command output:\n', text)
    if m:
        body = text[m.end():].replace('\r\n', '\n')
        text = body + ('\n(exit code %s)' % m.group(2) if m.group(2) and m.group(2) != '0' else '')
    return text, images


def plan_input(entries):
    status = {'pending': 'pending', 'in_progress': 'in_progress', 'completed': 'completed'}
    return {'todos': [{'content': e.get('content') or '', 'status': status.get(e.get('status'), 'pending'),
                       'activeForm': e.get('content') or ''} for e in entries or [] if isinstance(e, dict)]}


def text_of(content):
    if isinstance(content, dict):
        return content.get('text') or '' if content.get('type') == 'text' else ''
    if isinstance(content, list):
        return ''.join(text_of(c) for c in content)
    return content if isinstance(content, str) else ''


def is_error_text(text):
    return text.lstrip('\n ').startswith(ERROR_MARK)


def error_text(text):
    return text.strip().lstrip(ERROR_MARK).strip()


class Transcript:
    """A session as the app shows it (/history): users' messages and replies (text, thinking, tool
    steps with their results), from agy's replay of it (session/load) and what it says since."""

    def __init__(self, messages=None):
        self.messages = messages or []
        self.tools = {}
        for m in self.messages:
            for b in m.get('blocks') or []:
                if b.get('type') == 'tool' and b.get('id'):
                    self.tools[b['id']] = b
        self.plan_n = 0

    def reply(self):
        last = self.messages[-1] if self.messages else None
        if last is None or last.get('role') != 'assistant':
            last = {'role': 'assistant', 'blocks': [], 'uuid': None}
            self.messages.append(last)
        return last

    def user(self, text, images=None):
        self.messages.append({'role': 'user', 'text': text, 'images': images or [], 'uuid': None})

    def users(self):
        return sum(1 for m in self.messages if m.get('role') == 'user')

    def apply(self, u):
        kind = u.get('sessionUpdate')
        if kind == 'user_message_chunk':
            self.user(text_of(u.get('content')))
        elif kind in ('agent_message_chunk', 'agent_thought_chunk'):
            text = text_of(u.get('content'))
            if not text:
                return
            blocks = self.reply()['blocks']
            if kind == 'agent_message_chunk' and is_error_text(text):
                blocks.append({'type': 'text', 'text': '请求失败：' + error_text(text)})
                return
            want = 'text' if kind == 'agent_message_chunk' else 'thinking'
            if blocks and blocks[-1].get('type') == want and not blocks[-1].get('closed'):
                blocks[-1]['text'] += text
            else:
                blocks.append({'type': want, 'text': text})
        elif kind in ('tool_call', 'tool_call_update'):
            tid = u.get('toolCallId')
            tool = self.tools.get(tid)
            if tool is None:
                if kind == 'tool_call_update' and not (u.get('kind') or u.get('rawInput') or u.get('title')):
                    return
                tool = {'type': 'tool', 'id': tid, 'name': '', 'input': {}}
                self.tools[tid] = tool
                self.reply()['blocks'].append(tool)
            if u.get('kind') or u.get('rawInput') or u.get('title') or u.get('content'):
                prev_raw = tool.get('_raw') or {}
                clean = {k: v for k, v in u.items() if k != 'sessionUpdate' and v is not None}
                if 'content' in clean and first_diff(prev_raw) is not None and first_diff(clean) is None:
                    clean.pop('content', None)
                merged = dict(prev_raw, **clean)
                tool['name'], tool['input'] = tool_view(merged)
                tool['_raw'] = merged
            if u.get('status') in ('completed', 'failed'):
                tool['result'], _ = tool_result(u)
                tool['isError'] = u.get('status') == 'failed'
        elif kind == 'plan':
            reply = self.reply()
            plan = next((b for b in reply['blocks'] if b.get('type') == 'tool' and b.get('name') == 'TodoWrite'), None)
            if plan is None:
                self.plan_n += 1
                plan = {'type': 'tool', 'id': 'plan-%d' % self.plan_n, 'name': 'TodoWrite', 'result': ''}
                reply['blocks'].append(plan)
            plan['input'] = plan_input(u.get('entries'))

    def view(self, upto=None):
        out = []
        for m in self.messages[:upto]:
            m = dict(m)
            if 'blocks' in m:
                m['blocks'] = [{k: v for k, v in b.items() if not k.startswith('_') and k != 'closed'} for b in m['blocks']]
            out.append(m)
        return out


# ---- Conversations ----

class Chat:
    def __init__(self, key, session_id=None):
        self.key = key
        self.session_id = session_id
        self.cwd = DEFAULT_CWD
        self.model = None  # the app's: a base model ("claude-opus-5-5") and a level
        self.effort = None
        self.mode = None
        self.agy_model = None  # what the session has in agy
        self.agy_mode = None
        self.loaded = None  # the agy (its gen) the session is loaded in
        self.lock = threading.RLock()  # never held while agy is waited for: its reader takes it
        self.setup = threading.Lock()  # one making or loading of the session at a time
        self.running = False
        self.transcript = None  # read when the session is loaded, or from the saved copy
        self.turn_start = 0  # where the running turn's reply begins in the transcript
        self.prompts = []  # ids of the running turn's prompts (one more for each message inserted)
        self.inserted = {}  # prompt id -> [uuid, text, images] of a message inserted while a reply ran
        self.abandoned = set()  # prompts given up on (stopped and not ended 10 s later)
        self.perms = {}  # request id the app answers -> (agy's request id, options, tool id)
        self.used = time.time()
        self.last_out = time.time()
        self.quiet_logged = False
        self.turn = 0
        self.cancelled_turn = -1
        self.deleted = False
        self.commands = []
        self.usage = {}  # context window: used, size
        self.totals = {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0, 'thinking': 0}
        self.user_messages = 0
        self.tool_calls = 0
        self.timing = None  # when the running turn's message came, its session was ready, its prompt went
        # The reply being translated into the app's stream-json.
        self.msg_id = None
        self.msg_n = 0
        self.index = -1
        self.open = None  # 'text' or 'thinking': the block streaming
        self.shown_tools = {}  # tool id -> (name, input) last told
        self.last_error = ''
        self.replaying = False
        self.last_replay = 0.0
        self.replay_quiet_until = 0.0
        self.title = None
        self.autonomous = False  # True during a reactive wakeup (background task, timer, subagent) with no session/prompt
        self.auto_has_text = False
        self.active_tools = set()
        self.auto_timer = None

    # ---- what agy says ----

    def cc(self, msg):
        if self.deleted or not self.key:
            return
        msg.setdefault('session_id', self.session_id)
        hub.emit(self.key, 'cc', msg)

    def update(self, u):
        """One session/update: kept in the transcript; while a turn runs, told to the app as well."""
        with self.lock:
            self.last_out = time.time()
            if self.quiet_logged:
                log('chat %s: output again' % (self.key or '-')[:8])
                self.quiet_logged = False
            kind = u.get('sessionUpdate')
            if kind == 'usage_update':
                self.usage = {'used': u.get('used') or 0, 'size': u.get('size') or 0}
                if self.autonomous:
                    self.schedule_auto_finish()
                return
            if kind == 'available_commands_update':
                self.commands = [c.get('name') for c in u.get('availableCommands') or [] if isinstance(c, dict) and c.get('name')]
                remember_commands(self.commands)
                return
            if kind == 'current_mode_update':
                self.agy_mode = u.get('currentModeId') or self.agy_mode
                return
            if kind == 'session_info_update' and u.get('title'):
                self.title = u['title']
                if self.session_id and self.session_id not in read_json_cached(TITLES_FILE, {}):
                    hub.emit(self.key or '', 'title', {'session_id': self.session_id, 'title': u['title']})
                return
            if self.replaying:
                self.last_replay = time.time()
            if self.transcript is None:
                self.transcript = Transcript()
            prompt_live = self.running and (self.autonomous or bool(self.prompts))
            in_replay_tail = (self.running and not prompt_live) or (not self.running and time.time() < self.replay_quiet_until)
            if kind == 'agent_message_chunk' and is_error_text(text_of(u.get('content'))) and not self.replaying and not in_replay_tail:
                self.last_error = error_text(text_of(u.get('content')))
                log('chat %s: agy says: %s' % ((self.key or '-')[:8], self.last_error[:300]))
                if not self.running and not self.deleted and self.key:
                    self.start_autonomous(kind)
                if self.autonomous:
                    self.auto_has_text = True
                    self.schedule_auto_finish()
                return  # told as the turn's error when it ends
            live_update = (
                (kind in ('agent_message_chunk', 'agent_thought_chunk') and bool(text_of(u.get('content'))))
                or kind in ('tool_call', 'plan')
                or (kind == 'tool_call_update' and bool(u.get('kind') or u.get('rawInput') or u.get('title')))
            )
            if not self.replaying and not in_replay_tail and not self.running and not self.deleted and self.key and live_update:
                self.start_autonomous(kind)
                prompt_live = True
            self.first_output(kind)
            self.transcript.apply(u)
            if self.replaying or not prompt_live or not self.key:
                return
            self.translate(u)
            if self.autonomous:
                if kind == 'agent_message_chunk' and text_of(u.get('content')):
                    self.auto_has_text = True
                elif kind in ('tool_call', 'tool_call_update'):
                    tid = u.get('toolCallId')
                    if tid:
                        if u.get('status') in ('completed', 'failed'):
                            self.active_tools.discard(tid)
                        else:
                            self.active_tools.add(tid)
                            self.auto_has_text = False
                self.schedule_auto_finish()

    def start_autonomous(self, kind):
        """Starts streaming a reactive wakeup (background task, schedule timer, subagent) when no
        session/prompt is in flight (the caller holds self.lock)."""
        self.turn += 1
        self.running = True
        self.autonomous = True
        self.auto_has_text = False
        self.active_tools.clear()
        self.last_error = ''
        self.used = time.time()
        self.turn_start = len(self.transcript.messages) if self.transcript is not None else 0
        hub.emit(self.key, 'state', {'running': True})
        self.begin_turn()
        log('chat %s: reactive wakeup started (%s)' % ((self.key or '-')[:8], kind))

    def cancel_auto_timer(self):
        t, self.auto_timer = self.auto_timer, None
        if t is not None:
            t.cancel()

    def schedule_auto_finish(self):
        """Ends a reactive wakeup turn once agy goes quiet with no running tools or pending permissions."""
        self.cancel_auto_timer()
        if not self.autonomous or not self.running or self.deleted or self.active_tools or self.perms:
            return
        delay = AUTO_SETTLE_TEXT if self.auto_has_text else AUTO_SETTLE_IDLE
        turn = self.turn
        t = threading.Timer(delay, self.finish_autonomous, args=(turn,))
        t.daemon = True
        self.auto_timer = t
        t.start()

    def finish_autonomous(self, turn):
        with self.lock:
            if not self.autonomous or not self.running or self.deleted or self.turn != turn:
                return
            if self.active_tools or self.perms:
                return
            self.auto_timer = None
            if self.last_error:
                outcome = {'type': 'result', 'subtype': 'error', 'is_error': True, 'result': friendly(self.last_error)}
                if self.transcript is not None:
                    self.transcript.reply()['blocks'].append({'type': 'text', 'text': '请求失败：' + self.last_error})
            else:
                outcome = {'type': 'result', 'subtype': 'success', 'is_error': False}
        log('chat %s: reactive wakeup ended' % ((self.key or '-')[:8]))
        self.finish(outcome, turn=turn)

    def mark(self, what):
        with self.lock:
            if self.timing is not None:
                self.timing[what] = time.time()

    def first_output(self, kind):
        """Logs how long the reply took to begin, in its parts: the message to a session made or loaded
        (agy's first one is slow), to the prompt sent, and the model's first output (thinking, text, a tool)."""
        t = self.timing
        if t is None or self.replaying or not self.running or not (self.autonomous or self.prompts) \
                or kind not in ('agent_message_chunk', 'agent_thought_chunk', 'tool_call'):
            return
        self.timing = None
        now = time.time()
        session, prompted = t.get('session', t['sent']), t.get('prompted', t['sent'])
        log('chat %s: reply began after %.1fs (agy running): session ready %.1fs, prompt sent %.1fs, model answered %.1fs'
            % ((self.key or '-')[:8], now - t['sent'], session - t['sent'], prompted - t['sent'], now - prompted))

    def block(self, kind):
        """The app's stream has a block of this kind open (text or thinking), the one before closed."""
        if self.open == kind:
            return
        self.close_block()
        self.index += 1
        self.open = kind
        start = {'type': 'text', 'text': ''} if kind == 'text' else {'type': 'thinking', 'thinking': ''}
        self.cc({'type': 'stream_event', 'event': {'type': 'content_block_start', 'index': self.index, 'content_block': start}})

    def close_block(self):
        if self.open is not None:
            prev = self.open
            self.cc({'type': 'stream_event', 'event': {'type': 'content_block_stop', 'index': self.index}})
            self.open = None
            if self.transcript and self.transcript.messages:
                last = self.transcript.messages[-1]
                blocks = last.get('blocks')
                if blocks:
                    # transcript.apply() already ran for the incoming chunk, so the block being
                    # closed may now be blocks[-2] if a new block of another type was just appended.
                    if blocks[-1].get('type') == prev:
                        blocks[-1]['closed'] = True
                    elif len(blocks) >= 2 and blocks[-2].get('type') == prev:
                        blocks[-2]['closed'] = True

    def translate(self, u):
        kind = u.get('sessionUpdate')
        if kind == 'agent_message_chunk':
            text = text_of(u.get('content'))
            if text:
                self.block('text')
                self.cc({'type': 'stream_event', 'event': {'type': 'content_block_delta', 'index': self.index,
                                                           'delta': {'type': 'text_delta', 'text': text}}})
        elif kind == 'agent_thought_chunk':
            text = text_of(u.get('content'))
            if text:
                self.block('thinking')
                self.cc({'type': 'stream_event', 'event': {'type': 'content_block_delta', 'index': self.index,
                                                           'delta': {'type': 'thinking_delta', 'thinking': text}}})
        elif kind in ('tool_call', 'tool_call_update'):
            tool = (self.transcript.tools if self.transcript else {}).get(u.get('toolCallId'))
            if tool is None:
                return
            self.close_block()
            self.show_tool(tool)
            if u.get('status') in ('completed', 'failed'):
                text, images = tool_result(u)
                content = [{'type': 'text', 'text': text}] + [
                    {'type': 'image', 'source': {'type': 'base64', 'media_type': mime, 'data': data}} for mime, data in images]
                self.cc({'type': 'user', 'message': {'role': 'user', 'content': [{
                    'type': 'tool_result', 'tool_use_id': tool['id'], 'is_error': u.get('status') == 'failed', 'content': content}]}})
        elif kind == 'plan':
            reply = self.transcript.messages[-1] if self.transcript and self.transcript.messages else {}
            plan = next((b for b in reply.get('blocks') or [] if b.get('name') == 'TodoWrite'), None)
            if plan is not None:
                self.close_block()
                first = plan['id'] not in self.shown_tools
                self.show_tool(plan)
                if first:
                    self.cc({'type': 'user', 'message': {'role': 'user', 'content': [{
                        'type': 'tool_result', 'tool_use_id': plan['id'], 'is_error': False, 'content': [{'type': 'text', 'text': ''}]}]}})

    def show_tool(self, tool):
        """The app's row for a tool: told when new, and again when what it does changed."""
        seen = (tool.get('name'), json.dumps(tool.get('input'), sort_keys=True))
        if self.shown_tools.get(tool['id']) == seen:
            return
        if tool['id'] not in self.shown_tools:
            self.tool_calls += 1
        self.shown_tools[tool['id']] = seen
        self.cc({'type': 'assistant', 'message': {'id': self.msg_id, 'content': [
            {'type': 'tool_use', 'id': tool['id'], 'name': tool.get('name') or 'tool', 'input': tool.get('input') or {}}]}})

    def permission(self, rid, params):
        """agy asks whether a tool may run: the app's row for it asks the user (全部允许: yes at once)."""
        tc = params.get('toolCall') or {}
        options = [o for o in params.get('options') or [] if isinstance(o, dict)]
        with self.lock:
            if self.mode == BYPASS:
                pick = next((o for o in options if o.get('kind') == 'allow_once'), None) or \
                    next((o for o in options if str(o.get('kind', '')).startswith('allow')), None)
                if pick:
                    agy.respond(rid, {'outcome': {'outcome': 'selected', 'optionId': pick.get('optionId')}})
                    return
            if not self.running:
                agy.respond(rid, {'outcome': {'outcome': 'cancelled'}})
                return
            if self.transcript is None:
                self.transcript = Transcript()
            tid = tc.get('toolCallId') or 'tool-%s' % uuid.uuid4().hex[:8]
            if tid not in self.transcript.tools:
                self.transcript.apply(dict(tc, sessionUpdate='tool_call', toolCallId=tid))
            tool = self.transcript.tools[tid]
            self.close_block()
            self.show_tool(tool)
            request_id = 'perm-%s' % uuid.uuid4().hex[:12]
            self.perms[request_id] = (rid, options, tid)
            self.cancel_auto_timer()
            log('chat %s: agy asks to run %s' % (self.key[:8], tool.get('name')))
            self.cc({'type': 'control_request', 'request_id': request_id, 'request': {
                'subtype': 'can_use_tool', 'tool_use_id': tid, 'tool_name': tool.get('name') or 'tool', 'input': tool.get('input') or {}}})

    def answer(self, request_id, allow):
        """The user's answer to agy's question: False when it is not asked any more."""
        with self.lock:
            found = self.perms.pop(request_id, None)
            if found is not None and self.autonomous:
                self.schedule_auto_finish()
        if found is None:
            return False
        rid, options, tid = found
        kinds = ('allow_once', 'allow_always') if allow else ('reject_once', 'reject_always')
        pick = next((o for k in kinds for o in options if o.get('kind') == k), None)
        if pick is None:
            agy.respond(rid, {'outcome': {'outcome': 'cancelled'}})
        else:
            agy.respond(rid, {'outcome': {'outcome': 'selected', 'optionId': pick.get('optionId')}})
        log('chat %s: tool %s' % (self.key[:8], 'allowed' if allow else 'denied'))
        return True

    def drop_questions(self):
        """Questions agy asked that no answer will come to now (the turn ended or was stopped)."""
        with self.lock:
            left, self.perms = self.perms, {}
        for request_id, (rid, options, tid) in left.items():
            agy.respond(rid, {'outcome': {'outcome': 'cancelled'}})
            self.cc({'type': 'control_cancel_request', 'request_id': request_id})

    def agy_gone(self, gen, code, stderr):
        """agy ended: the session is loaded again in the next one; a turn running ended with it."""
        with self.lock:
            if self.loaded != gen:
                return
            self.loaded = None
            self.agy_model = self.agy_mode = None
            was_running = self.running
            self.cancel_auto_timer()
            self.running = False
            self.autonomous = False
            self.auto_has_text = False
            self.active_tools.clear()
            self.prompts = []
            self.inserted = {}
            self.abandoned.clear()
            self.perms = {}
        if was_running and not self.deleted and self.key:
            hub.emit(self.key, 'exit', {'code': code, 'running': True, 'stderr': stderr})

    # ---- what is said to agy ----

    def send(self, content, cwd=None, model=None, session_id=None, effort=None, mode=None, message_uuid=None):
        """A message: the next turn, or (a reply running) one inserted into it: agy gives it to the
        model with what it does next. The work is done in a thread of its own: starting agy and
        loading the session may take longer than the app waits for an answer."""
        with self.lock:
            finish_auto = self.autonomous and self.running
            auto_turn = self.turn
        if finish_auto:
            self.finish({'type': 'result', 'subtype': 'success', 'is_error': False}, turn=auto_turn)
        with self.lock:
            inserting = self.running and bool(self.prompts) and self.loaded == agy.gen and agy.alive()
            if self.running and not self.prompts and message_uuid is not None:
                self.cc({'type': 'command_lifecycle', 'command_uuid': message_uuid, 'state': 'failed'})
                return
            if session_id and not self.session_id:
                self.session_id = session_id
                index_chat_session(self, session_id)
            if cwd and self.loaded is None:
                self.cwd = cwd
            self.model, self.effort, self.mode = model, effort, mode
            text, images = '', []
            for b in content if isinstance(content, list) else [{'type': 'text', 'text': str(content)}]:
                if not isinstance(b, dict):
                    continue
                if b.get('type') == 'text':
                    text += ('\n\n' if text else '') + b.get('text', '')
                elif b.get('type') == 'image':
                    src = b.get('source') or {}
                    images.append((src.get('media_type') or 'image/png', src.get('data', '')))
            self.used = self.last_out = time.time()
            if not inserting:
                self.cancel_auto_timer()
                self.autonomous = False
                self.auto_has_text = False
                self.active_tools.clear()
                self.turn += 1
                self.running = True
                self.last_error = ''
                self.timing = {'sent': time.time()}
                self.turn_start = len(self.transcript.messages) if self.transcript is not None else 0
            turn = self.turn
        hub.emit(self.key, 'state', {'running': True})
        threading.Thread(target=self.deliver, args=(text, images, inserting, message_uuid, turn), daemon=True).start()

    def deliver(self, text, images, inserting, message_uuid, turn):
        try:
            if inserting:
                self.prompt(text, images, message_uuid, turn)
                return
            self.ready_session()
            self.mark('session')
            name = re.match(r'^/(?:name|rename)\s+(\S[\s\S]*)$', text.strip()) if not images else None
            if name:  # /name: the app's own name for it, not a message to the model
                cancelled = False
                with self.lock:
                    if self.turn != turn or self.deleted:
                        return
                    if self.cancelled_turn == turn:
                        cancelled = True
                    else:
                        self.begin_turn()
                if cancelled:
                    self.finish({'type': 'result', 'subtype': 'error_during_execution', 'is_error': False}, turn=turn)
                    return
                set_title(self.session_id, name.group(1))
                keep_title(self.key, self.session_id, ' '.join(name.group(1).split())[:200])
                self.finish({'type': 'result', 'subtype': 'success', 'is_error': False}, turn=turn)
                return
            with self.lock:
                if self.turn != turn or self.deleted:
                    return
                if self.cancelled_turn == turn:
                    cancelled = True
                else:
                    cancelled = False
                    self.begin_turn()
            if cancelled:
                self.finish({'type': 'result', 'subtype': 'error_during_execution', 'is_error': False}, turn=turn)
                return
            self.prompt(text, images, None, turn)
        except Exception as e:
            if not isinstance(e, (AgyError, RpcError)):
                import traceback
                log('chat %s: unexpected error in deliver:\n%s' % ((self.key or '-')[:8], traceback.format_exc()))
            else:
                log('chat %s: message not sent: %s' % ((self.key or '-')[:8], e))
            if inserting:
                self.cc({'type': 'command_lifecycle', 'command_uuid': message_uuid, 'state': 'failed'})
                return
            self.finish({'type': 'result', 'subtype': 'error', 'is_error': True, 'result': friendly(str(e))}, turn=turn)

    def begin_turn(self):
        """The turn's start as the app knows it (the caller holds the lock)."""
        self.msg_n += 1
        self.msg_id = 'agy-%s-%d' % ((self.session_id or self.key)[-6:], self.msg_n)
        self.index, self.open = -1, None
        self.shown_tools = {}
        self.cc({'type': 'system', 'subtype': 'init', 'model': self.agy_model or self.model or '', 'cwd': self.cwd,
                 'tools': [], 'slash_commands': ['name'] + (self.commands or known_commands), 'permissionMode': self.mode or 'default'})
        self.cc({'type': 'stream_event', 'event': {'type': 'message_start', 'message': {'id': self.msg_id}}})

    def ready_session(self):
        """The session, made (a new conversation) or loaded in the running agy, with the model and
        mode the app picked. The chat's lock is not held meanwhile: agy's replay of a session comes
        through it."""
        agy.ensure()
        with self.setup:
            gen = agy.gen
            if self.session_id and self.loaded == gen:
                pass
            elif self.session_id:
                self.load()
            else:
                params = {'cwd': self.cwd, 'mcpServers': []}
                prompt = launch.data.get('appendSystemPrompt', '').strip()
                if prompt:
                    params['_meta'] = {'antigravity': {'systemPrompt': {'append': prompt}}}
                os.makedirs(self.cwd, exist_ok=True)
                result = agy.request('session/new', params, timeout=90) or {}
                if not result.get('sessionId'):
                    raise AgyError('agy 没有建立对话')
                with self.lock:
                    self.session_id = result['sessionId']
                    self.loaded = gen
                    self.transcript = Transcript()
                    self.took(result)
                index_chat_session(self, self.session_id)
                with agy.lock:
                    early = agy.early.pop(self.session_id, [])
                for u in early:
                    self.update(u)
                with chats_lock:
                    loose.pop(self.session_id, None)
                log('chat %s: new session %s in %s' % (self.key[:8], self.session_id, self.cwd))
            # A level picked with agy's own model: that model at this level.
            base = self.model or (split_model(self.agy_model or '')[0] if self.effort else None)
            want = model_id_for(base, self.effort)
            if want and want != self.agy_model:
                try:
                    agy.request('session/set_model', {'sessionId': self.session_id, 'modelId': want}, timeout=30)
                    self.agy_model = want
                    log('chat %s: model %s' % (self.key[:8], want))
                except RpcError as e:
                    log('chat %s: model %s refused: %s' % (self.key[:8], want, e))
                    raise AgyError('agy 不认这个模型（%s）：%s' % (want, e))
            mode = 'default' if self.mode == BYPASS else APP_MODES.get(self.mode or '')
            if mode and mode != self.agy_mode:
                try:
                    agy.request('session/set_mode', {'sessionId': self.session_id, 'modeId': mode}, timeout=30)
                    self.agy_mode = mode
                except RpcError as e:
                    log('chat %s: mode %s refused: %s' % (self.key[:8], mode, e))

    def took(self, result):
        """What agy answered to session/new or session/load: its models and modes."""
        keep_models(result.get('models'))
        models = result.get('models') or {}
        self.agy_model = models.get('currentModelId') or self.agy_model
        modes = result.get('modes') or {}
        self.agy_mode = modes.get('currentModeId') or self.agy_mode
        if modes.get('availableModes'):
            remember_modes([m.get('id') for m in modes['availableModes'] if isinstance(m, dict) and m.get('id')])

    def load(self):
        """Loads the session into the running agy (the caller holds self.setup, not self.lock): agy
        tells it again from the start, which is kept as its transcript."""
        if self.cwd == DEFAULT_CWD:
            row_cwd = (session_row(self.session_id) or {}).get('cwd')
            if row_cwd and os.path.isdir(row_cwd):
                self.cwd = row_cwd
        cwd = self.cwd if os.path.isdir(self.cwd) else DEFAULT_CWD
        with self.lock:
            self.replaying = True
            self.last_replay = 0.0
            before, self.transcript = self.transcript, Transcript()
        try:
            result = agy.request('session/load', {'sessionId': self.session_id, 'cwd': cwd, 'mcpServers': []}, timeout=120) or {}
            # agy may still be flushing the tail of session/load replay updates asynchronously;
            # wait briefly for them to settle before turning off replaying or saving history.
            deadline = time.time() + 2.0
            while time.time() < deadline:
                with self.lock:
                    last = self.last_replay
                if last <= 0 or time.time() - last >= 0.25:
                    break
                time.sleep(0.02)
        except Exception:
            with self.lock:
                self.transcript = before
            raise
        finally:
            with self.lock:
                self.replaying = False
                self.replay_quiet_until = time.time() + 1.0
        with self.lock:
            self.loaded = agy.gen
            self.took(result)
            add_images(self.session_id, self.transcript)
            self.user_messages = self.transcript.users()
            self.tool_calls = sum(
                1 for m in self.transcript.messages if m.get('role') == 'assistant'
                for b in m.get('blocks') or [] if b.get('type') == 'tool'
            )
            deleted = self.deleted
            view = self.transcript.view()
        if not deleted:
            save_history(self.session_id, view, self.cwd)
        log('chat %s: session %s loaded (%d messages)' % ((self.key or '-')[:8], self.session_id, len(view)))

    def prompt(self, text, images, message_uuid, turn=None):
        """Sends the message to agy; the turn ends (or, inserted, the message is the model's) when
        agy answers it."""
        blocks = [{'type': 'image', 'mimeType': mime, 'data': data} for mime, data in images if data]
        if text:
            blocks.append({'type': 'text', 'text': text})
        with self.lock:
            if message_uuid is not None and not self.running:
                self.cc({'type': 'command_lifecycle', 'command_uuid': message_uuid, 'state': 'failed'})
                return
            if message_uuid is None and (self.deleted or (turn is not None and self.turn != turn)):
                return
            if self.transcript is None:
                self.transcript = Transcript()
            if message_uuid is None:
                refs = keep_images(self.session_id, self.transcript.users(), images)
                self.close_block()
                self.transcript.user(text, refs)
                self.turn_start = len(self.transcript.messages)
                self.user_messages += 1
                self.mark('prompted')
            slot = agy.call('session/prompt', {'sessionId': self.session_id, 'prompt': blocks}, then=self.answered)
            self.prompts.append(slot['id'])
            if message_uuid is not None:
                self.inserted[slot['id']] = [message_uuid, text, list(images)]
        log('chat %s: message %s (%d image)' % ((self.key or '-')[:8], 'inserted' if message_uuid else 'sent', len(images)))

    def answered(self, slot):
        """agy answered a prompt: the turn ended, or the prompt was ended by a later one (a message
        inserted while it ran: from now on that one's) and the turn goes on."""
        with self.lock:
            rid = slot['id']
            if self.deleted:
                return
            if rid in self.abandoned:
                self.abandoned.discard(rid)
                return
            if rid not in self.prompts:
                return
            idx = self.prompts.index(rid)
            later = self.prompts[idx + 1:]
            self.prompts.remove(rid)
            error, result = slot.get('error'), slot.get('result') or {}
            self.count_locked(result.get('usage'))
            if idx > 0 and rid in self.inserted:
                ins = self.inserted.pop(rid)
                self.cc({'type': 'command_lifecycle', 'command_uuid': ins[0], 'state': 'failed'})
                return
            if later:
                # The message after it is the model's now: shown as one of the conversation's.
                nxt = self.inserted.pop(later[0], None)
                if nxt:
                    message_uuid, text, imgs = nxt
                    refs = keep_images(self.session_id, self.transcript.users(), imgs)
                    self.close_block()
                    self.transcript.user(text, refs)
                    self.user_messages += 1
                    self.cc({'type': 'command_lifecycle', 'command_uuid': message_uuid, 'state': 'started'})
                return
            # An inserted prompt that failed while the parent prompt is still running must not end the turn.
            ins = self.inserted.pop(rid, None)
            if ins is not None and (self.prompts or not self.running):
                self.cc({'type': 'command_lifecycle', 'command_uuid': ins[0], 'state': 'failed'})
                return
            turn = self.turn
        if error is not None:
            outcome = {'type': 'result', 'subtype': 'error', 'is_error': True, 'result': friendly(str(error))}
        elif result.get('stopReason') == 'cancelled':
            outcome = {'type': 'result', 'subtype': 'error_during_execution', 'is_error': False}
        elif self.last_error:
            outcome = {'type': 'result', 'subtype': 'error', 'is_error': True, 'result': friendly(self.last_error)}
            with self.lock:
                if self.transcript is not None:
                    self.transcript.reply()['blocks'].append({'type': 'text', 'text': '请求失败：' + self.last_error})
        elif result.get('stopReason') == 'refusal':
            outcome = {'type': 'result', 'subtype': 'error', 'is_error': True, 'result': '模型拒绝了这个请求'}
        else:
            outcome = {'type': 'result', 'subtype': 'success', 'is_error': False}
        log('chat %s: turn ended (%s%s)' % ((self.key or '-')[:8], result.get('stopReason') or outcome['subtype'],
                                            ': %s' % error if error else ''))
        self.finish(outcome, turn=turn)

    def count(self, usage):
        with self.lock:
            self.count_locked(usage)

    def count_locked(self, usage):
        if not isinstance(usage, dict):
            return
        for key, theirs in (('input', 'inputTokens'), ('output', 'outputTokens'), ('cacheRead', 'cachedReadTokens'),
                            ('cacheWrite', 'cachedWriteTokens'), ('thinking', 'thoughtTokens')):
            self.totals[key] += usage.get(theirs) or 0

    def finish(self, result, turn=None):
        """The turn is over: the app is told, what is still asked is dropped, the transcript kept."""
        snap = None
        deleted = False
        with self.lock:
            if not self.running or (turn is not None and self.turn != turn):
                return
            left, self.perms = self.perms, {}
            self.cancel_auto_timer()
            self.close_block()
            for rid, (message_uuid, text, imgs) in list(self.inserted.items()):
                self.cc({'type': 'command_lifecycle', 'command_uuid': message_uuid, 'state': 'discarded'})
            self.inserted = {}
            self.prompts = []
            self.running = False
            self.autonomous = False
            self.auto_has_text = False
            self.active_tools.clear()
            self.used = time.time()
            deleted = self.deleted
            if not deleted and self.session_id and self.transcript is not None:
                snap = (self.session_id, self.transcript.view(), self.cwd)
        for request_id, (rid, options, tid) in left.items():
            agy.respond(rid, {'outcome': {'outcome': 'cancelled'}})
            self.cc({'type': 'control_cancel_request', 'request_id': request_id})
        if snap is not None:
            save_history(*snap)
        if not deleted:
            self.cc(result)
        if agy.restart_reason and not any(c.running for c in all_chats()) and not agy.busy():
            agy.stop(agy.restart_reason)

    def abort(self):
        """Stops and marks this conversation deleted so late callbacks never recreate its history."""
        with self.lock:
            self.cancel_auto_timer()
            self.deleted = True
            self.running = False
            self.autonomous = False
            self.auto_has_text = False
            self.active_tools.clear()
            self.prompts = []
            self.inserted = {}
            self.abandoned.clear()
            sid = self.session_id
        self.drop_questions()
        if sid:
            agy.notify('session/cancel', {'sessionId': sid})

    def cancel(self, message_uuid):
        """A message inserted while a reply ran is given to the model at once: it cannot be taken back."""
        with self.lock:
            return not any(v[0] == message_uuid for v in self.inserted.values()) and not self.running

    def stop(self):
        """Stops the turn; False when none runs. agy ends it (its prompt answers "cancelled"); one
        not ended 10 s later is ended here."""
        with self.lock:
            if not self.running:
                return False
            turn, prompts, auto = self.turn, list(self.prompts), self.autonomous
            self.cancel_auto_timer()
            self.cancelled_turn = turn
            log('chat %s: stop requested' % (self.key or '-')[:8])
        self.drop_questions()
        if self.session_id:
            agy.notify('session/cancel', {'sessionId': self.session_id})

        def force():
            time.sleep(1.0 if auto else 10)
            with self.lock:
                if not self.running or self.deleted or self.turn != turn:
                    return
                self.abandoned.update(prompts)
                self.abandoned.update(self.prompts)
                if not auto:
                    log('chat %s: not stopped 10 s after cancel: ended here' % (self.key or '-')[:8])
            self.finish({'type': 'result', 'subtype': 'error_during_execution', 'is_error': False}, turn=turn)
        threading.Thread(target=force, daemon=True).start()
        return True

    def context_usage(self):
        with self.lock:
            if not self.usage and not any(self.totals.values()):
                return None
            used, size = self.usage.get('used') or 0, self.usage.get('size') or 0
            assistant = sum(1 for m in self.transcript.messages if m.get('role') == 'assistant' and m.get('blocks')) \
                if self.transcript is not None else self.user_messages
            return {'categories': [{'name': 'Messages', 'tokens': used, 'deferred': False}],
                    'totalTokens': used, 'maxTokens': size, 'percentage': round(100.0 * used / size, 1) if size else 0,
                    'model': self.agy_model or self.model or '',
                    'stats': {'tokens': dict(self.totals), 'cost': 0, 'userMessages': self.user_messages,
                              'assistantMessages': assistant, 'toolCalls': self.tool_calls}}


chats = {}  # the app's key -> Chat
chats_by_session = {}  # session id -> Chat (index into chats for O(1) lookup)
loose = {}  # session id -> Chat of a session read (/history) that no conversation of the app has taken yet
chats_lock = threading.Lock()


def index_chat_session(chat, session_id):
    if not session_id:
        return
    with chats_lock:
        if chat.key and chats.get(chat.key) is chat:
            chats_by_session[session_id] = chat
seen_messages = []  # ids of messages already sent: the app may repeat a request whose answer it lost
known_commands = []
known_modes = []


def remember_commands(commands):
    global known_commands
    if commands and commands != known_commands:
        known_commands = list(commands)


def remember_modes(modes):
    global known_modes
    if modes and modes != known_modes:
        known_modes = list(modes)
        hub.emit('', 'config')


def all_chats():
    with chats_lock:
        return list(chats.values()) + list(loose.values())


def first_sight(mid):
    """Whether a message (its id from the app, None for one without) is new; a repeat is not sent again."""
    with chats_lock:
        if mid is None:
            return True
        if mid in seen_messages:
            return False
        seen_messages.append(mid)
        del seen_messages[:-200]
        return True


def chat_for(key, session_id=None, create=True):
    """The conversation for the app's key. A session already known under another key (the app was
    restarted meanwhile), or read for the list, is taken over, so one session is one Chat. None when
    there is none and create is False."""
    with chats_lock:
        if key in chats:
            return chats[key]
        if session_id:
            existing = chats_by_session.get(session_id)
            if existing is not None and existing.key in chats and chats[existing.key] is existing:
                del chats[existing.key]
                existing.key = key
                chats[key] = existing
                return existing
            for old_key, chat in list(chats.items()):
                if chat.session_id == session_id:
                    del chats[old_key]
                    chat.key = key
                    chats[key] = chat
                    chats_by_session[session_id] = chat
                    return chat
            if session_id in loose:
                chat = loose.pop(session_id)
                chat.key = key
                chats[key] = chat
                chats_by_session[session_id] = chat
                return chat
        if not create:
            return None
        chat = Chat(key, session_id)
        chats[key] = chat
        if session_id:
            chats_by_session[session_id] = chat
        return chat


def chat_of_session(session_id):
    if not session_id:
        return None
    with chats_lock:
        found = chats_by_session.get(session_id)
        if found is not None:
            if found.key in chats and chats[found.key] is found and found.session_id == session_id:
                return found
            chats_by_session.pop(session_id, None)
        found = next((c for c in chats.values() if c.session_id == session_id), None)
        if found is not None:
            chats_by_session[session_id] = found
            return found
        return loose.get(session_id)


def reaper():
    """Logs a running turn with no output for a minute and the bridge itself having been paused
    (frozen by the system: it did not run for a while); starts agy ahead while nothing runs, so a
    first message does not wait for it."""
    tick = time.monotonic()
    while True:
        time.sleep(5)
        now = time.monotonic()
        if now - tick > 20:
            log('bridge was paused for %ds (frozen by the system?)' % (now - tick - 5))
        tick = now
        live = all_chats()
        for chat in live:
            if chat.running and not chat.quiet_logged and time.time() - chat.last_out > 60:
                chat.quiet_logged = True
                log('chat %s: no output from agy for 60s' % chat.key[:8])
        if not any(c.running for c in live) and not agy.busy():
            if agy.restart_reason:
                agy.stop(agy.restart_reason)
            elif agy.alive() and time.time() - agy.used > IDLE_STOP:
                agy.stop('unused for %d minutes' % (IDLE_STOP // 60))
        voice_dictator.reap()
        with chats_lock:
            cutoff = time.time() - IDLE_STOP
            for sid in [sid for sid, c in loose.items() if not c.running and c.used < cutoff]:
                loose.pop(sid, None)


def warm():
    """agy started ahead (once signed in) when the app opens a conversation, unless it could not be lately,
    and its first session made ahead too (prime)."""
    agy.used = time.time()
    if not os.path.exists(agy_path()) or not signed_in() or time.time() < agy.retry_at:
        return
    try:
        agy.ensure()
    except (AgyError, RpcError) as e:
        log('agy not started ahead: %s' % e)
        return
    prime()


def prime():
    """The first session a new agy makes takes seconds (1.2 to 2.7 s measured on the phone with no network,
    3 to 5 s in the app signed in with Google, which makes its first connections then) and the next ones a
    fraction of that: one is made ahead and deleted at once, as for the models,
    so a conversation's first message does not wait for it. A no-op once this agy has made a session."""
    if agy.primed_gen == agy.gen or not models_lock.acquire(blocking=False):
        return
    sid = None
    try:
        if agy.primed_gen == agy.gen:
            return
        os.makedirs(DEFAULT_CWD, exist_ok=True)
        began = time.time()
        result = agy.request('session/new', {'cwd': DEFAULT_CWD, 'mcpServers': []}, timeout=90) or {}
        sid = result.get('sessionId')
        keep_models(result.get('models'))
        log('agy primed: its first session took %.1fs' % (time.time() - began))
    except (AgyError, RpcError, OSError) as e:
        log('agy not primed: %s' % e)
    finally:
        if sid:
            try:
                agy.request('session/close', {'sessionId': sid}, timeout=10)
            except (AgyError, RpcError):
                pass
            with agy.lock:
                early = agy.early.pop(sid, [])
            for u in early:
                if u.get('sessionUpdate') == 'available_commands_update':
                    remember_commands([c.get('name') for c in u.get('availableCommands') or [] if isinstance(c, dict) and c.get('name')])
            remove_session_files(sid, remember=False)
        models_lock.release()


# ---- Saved sessions (agy's own list, and the transcripts kept here) ----

def parse_time(text):
    """agy's "2026-11-18 06:50:02.425491448+00:00" as seconds."""
    m = re.match(r'^(\d{4}-\d\d-\d\d)[ T](\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:?\d\d)?', text or '')
    if not m:
        return 0.0
    zone = m.group(4) or '+00:00'
    zone = '+00:00' if zone == 'Z' else zone if ':' in zone else zone[:3] + ':' + zone[3:]
    try:
        t = datetime.fromisoformat('%sT%s%s' % (m.group(1), m.group(2), zone)).timestamp()
    except ValueError:
        return 0.0
    return t + float('0.' + m.group(3)) if m.group(3) else t


def uri_path(uri):
    return unquote(uri[7:]) if isinstance(uri, str) and uri.startswith('file://') else (uri or '')


def one_line(text, limit=SHORT_TITLE):
    t = ' '.join((text or '').split())
    return t if len(t) <= limit else t[:limit - 1] + '…'


def _format_session_row(row):
    sid, title, preview, steps, modified, uris, parent, depth = row
    if parent or (depth or 0) > 0 or not steps:
        return None
    try:
        folders = [uri_path(u) for u in json.loads(uris or '[]')]
    except ValueError:
        folders = []
    return {'id': sid, 'title': title or '', 'preview': preview or '', 'cwd': folders[0] if folders else '',
            'mtime': parse_time(modified), 'stamp': modified or '', 'steps': steps}


def db_sessions():
    """agy's conversations (not a sub-agent's, not one where nothing was said), newest first, from its
    own list: id, title, preview, folder, when it last changed. [] when there is none yet."""
    if not os.path.exists(SUMMARIES_DB):
        return []
    db = sqlite3.connect('file:%s?mode=ro' % SUMMARIES_DB, uri=True, timeout=3)
    try:
        rows = db.execute('SELECT conversation_id, title, preview, step_count, last_modified_time, workspace_uris, '
                          'parent_conversation_id, nesting_depth FROM conversation_summaries').fetchall()
    finally:
        db.close()
    out = []
    for row in rows:
        item = _format_session_row(row)
        if item is not None:
            out.append(item)
    out.sort(key=lambda s: (-s['mtime'], s['id']))
    return out


def session_row(session_id):
    if not session_id or not os.path.exists(SUMMARIES_DB):
        return None
    try:
        db = sqlite3.connect('file:%s?mode=ro' % SUMMARIES_DB, uri=True, timeout=3)
        try:
            row = db.execute('SELECT conversation_id, title, preview, step_count, last_modified_time, workspace_uris, '
                             'parent_conversation_id, nesting_depth FROM conversation_summaries '
                             'WHERE conversation_id = ? LIMIT 1', (session_id,)).fetchone()
        finally:
            db.close()
        return _format_session_row(row) if row is not None else None
    except sqlite3.Error:
        return None


def list_sessions():
    titles, hidden = read_json_cached(TITLES_FILE, {}), set(read_json_cached(HIDDEN_FILE, []))
    try:
        rows = db_sessions()
    except sqlite3.Error as e:
        log('sessions: %s' % e)
        rows = []
    out = []
    for s in rows:
        if s['id'] in hidden:
            continue
        chat = chat_of_session(s['id'])
        out.append({'id': s['id'], 'title': titles.get(s['id']) or s['title'] or one_line(s['preview']) or '（无标题）',
                    'cwd': s['cwd'], 'mtime': s['mtime'], 'size': s['steps'], 'running': bool(chat and chat.running)})
    return out


def history_path(session_id):
    return os.path.join(HISTORY_DIR, session_id + '.json')


history_lock = threading.Lock()


def save_history(session_id, messages, cwd):
    """The transcript as the app shows it, with when agy last changed the session (read again when it
    has changed since: continued in the terminal)."""
    row = session_row(session_id)
    try:
        with history_lock:
            if session_id in read_json_cached(HIDDEN_FILE, []):
                return
            write_json(history_path(session_id), {'stamp': row['stamp'] if row else '', 'cwd': cwd, 'messages': messages}, indent=None)
        with _search_lock:
            _search_cache.pop(session_id, None)
    except OSError as e:
        log('history %s not kept: %s' % (session_id, e))


def keep_images(session_id, n, images):
    """Pictures sent with a message (agy replays none): kept as ccimg:<session>:<message>:<k>."""
    refs = []
    if not session_id or not images:
        return refs
    folder = os.path.join(IMAGES_DIR, session_id)
    os.makedirs(folder, exist_ok=True)
    for k, (mime, data) in enumerate(images):
        ext = {'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp', 'image/gif': 'gif'}.get(mime, 'img')
        try:
            import base64
            with open(os.path.join(folder, '%d-%d.%s' % (n, k, ext)), 'wb') as f:
                f.write(base64.b64decode(data))
        except (OSError, ValueError):
            continue
        refs.append('ccimg:%s:%d:%d' % (session_id, n, k))
    return refs


def image_files(session_id):
    folder = os.path.join(IMAGES_DIR, session_id)
    try:
        return sorted(os.listdir(folder))
    except OSError:
        return []


def add_images(session_id, transcript):
    """The pictures kept here, put back with the messages they were sent with (by their place)."""
    files = image_files(session_id)
    if not files:
        return
    n = 0
    for m in transcript.messages:
        if m.get('role') != 'user':
            continue
        refs = []
        for name in files:
            parts = name.split('.')[0].split('-')
            if len(parts) == 2 and parts[0] == str(n) and parts[1].isdigit():
                refs.append((int(parts[1]), 'ccimg:%s:%s:%s' % (session_id, parts[0], parts[1])))
        if refs:
            m['images'] = [ref for _, ref in sorted(refs)]
        n += 1


def session_image(session_id, n, k):
    if not safe_id(session_id):
        return None
    for name in image_files(session_id):
        if name.split('.')[0] == '%d-%d' % (n, k):
            mime = {'jpg': 'image/jpeg', 'png': 'image/png', 'webp': 'image/webp', 'gif': 'image/gif'}.get(name.rsplit('.', 1)[-1], 'image/png')
            import base64
            try:
                with open(os.path.join(IMAGES_DIR, session_id, name), 'rb') as f:
                    return mime, base64.b64encode(f.read()).decode('ascii')
            except OSError:
                return None
    return None


refreshing_history = set()
refreshing_lock = threading.Lock()


def _refresh_history(session_id):
    try:
        data = load_history(session_id, fast=False)
        if data is not None:
            hub.emit('', 'history', {'id': session_id})
    except (AgyError, RpcError, OSError) as e:
        log('history %s background refresh failed: %s' % (session_id[:8], e))
    finally:
        with refreshing_lock:
            refreshing_history.discard(session_id)


def load_history(session_id, fast=False):
    """The conversation as the app shows it: the one running here (up to the reply under way), the
    copy kept here when agy has not changed it since, else agy's replay of it (session/load).
    When fast=True and an older saved copy exists, returns it immediately and refreshes in the background."""
    if not safe_id(session_id):
        return None
    titles = read_json_cached(TITLES_FILE, {})
    chat = chat_of_session(session_id)
    if chat is not None and chat.transcript is not None and chat.loaded == agy.gen and agy.alive():
        with chat.lock:
            upto = chat.turn_start if chat.running else None
            return {'id': session_id, 'messages': chat.transcript.view(upto), 'cwd': chat.cwd,
                    'title': titles.get(session_id), 'model': chat.agy_model, 'context': chat.usage.get('used', 0)}
    row = session_row(session_id)
    saved = read_json(history_path(session_id), {})
    if saved and (row is None or saved.get('stamp') == row['stamp']):
        return {'id': session_id, 'messages': saved.get('messages', []), 'cwd': saved.get('cwd') or (row or {}).get('cwd', ''),
                'title': titles.get(session_id), 'model': None, 'context': 0}
    if row is None:
        return None
    if fast and saved:
        with refreshing_lock:
            if session_id not in refreshing_history:
                refreshing_history.add(session_id)
                threading.Thread(target=_refresh_history, args=(session_id,), daemon=True).start()
        return {'id': session_id, 'messages': saved.get('messages', []), 'cwd': saved.get('cwd') or row['cwd'],
                'title': titles.get(session_id), 'model': None, 'context': 0, 'stale': True}
    if chat is None:
        with chats_lock:
            for c in chats.values():
                if c.session_id == session_id:
                    chat = c
                    break
            if chat is None:
                chat = loose.setdefault(session_id, Chat(None, session_id))
    try:
        agy.ensure()
        with chat.setup:
            if not chat.session_id or chat.loaded != agy.gen:
                chat.cwd = row['cwd'] or chat.cwd
                chat.load()
    except (AgyError, RpcError) as e:
        if saved:  # an older copy, rather than nothing
            return {'id': session_id, 'messages': saved.get('messages', []), 'cwd': row['cwd'],
                    'title': titles.get(session_id), 'model': None, 'context': 0}
        raise AgyError(friendly(str(e)))
    with chat.lock:
        return {'id': session_id, 'messages': chat.transcript.view(chat.turn_start if chat.running else None),
                'cwd': chat.cwd, 'title': titles.get(session_id), 'model': chat.agy_model, 'context': chat.usage.get('used', 0)}


_search_cache = {}  # session_id -> (mtime_ns, size, body_text, body_lower)
_search_lock = threading.Lock()


def _session_body_text(session_id):
    path = history_path(session_id)
    try:
        st = os.stat(path)
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        with _search_lock:
            _search_cache.pop(session_id, None)
        return '', ''
    with _search_lock:
        hit = _search_cache.get(session_id)
        if hit is not None and hit[0] == sig:
            return hit[1], hit[2]
    texts = []
    for m in read_json(path, {}).get('messages', []):
        if m.get('role') == 'user':
            texts.append(m.get('text') or '')
        for b in m.get('blocks') or []:
            if b.get('type') == 'text':
                texts.append(b.get('text') or '')
    body = '\n'.join(texts)
    body_lower = body.lower()
    with _search_lock:
        _search_cache[session_id] = (sig, body, body_lower)
    return body, body_lower


def search_sessions(query):
    """Conversations whose kept transcript (or name) has the words: those read here once."""
    q = query.strip().lower()
    if not q:
        return []
    out = []
    for s in list_sessions():
        title = s['title']
        body, body_lower = _session_body_text(s['id'])
        if body:
            text = title + '\n' + body
            lower = title.lower() + '\n' + body_lower
        else:
            text = title
            lower = title.lower()
        at = lower.find(q)
        if at >= 0:
            out.append({'id': s['id'], 'snippet': ' '.join(text[max(0, at - 30):at + len(q) + 60].split())})
    return out


def set_title(session_id, title):
    with state_lock:
        titles = read_json(TITLES_FILE, {})
        titles[session_id] = ' '.join(title.split())[:200]
        write_json(TITLES_FILE, titles)


def keep_title(key, session_id, title):
    hub.emit(key, 'title', {'session_id': session_id, 'title': title})


def delete_session(session_id):
    """Deletes a conversation: closed in agy, its files and its line in agy's list taken out (and
    remembered as deleted, should agy list it again). True when there was one."""
    if not safe_id(session_id):
        return False
    with chats_lock:
        gone = [chats.pop(k) for k in [k for k, c in chats.items() if c.session_id == session_id]]
        chats_by_session.pop(session_id, None)
        if session_id in loose:
            gone.append(loose.pop(session_id))
    for chat in gone:
        chat.abort()
        if chat.loaded == agy.gen and agy.alive():
            try:
                agy.request('session/close', {'sessionId': session_id}, timeout=10)
            except (AgyError, RpcError) as e:
                log('session %s not closed: %s' % (session_id, e))
    found = session_row(session_id) is not None
    remove_session_files(session_id)
    return found or bool(gone)


def remove_session_files(session_id, remember=True):
    """A session's line in agy's list, its files and what is kept here of it, taken out (and it
    remembered as deleted, should agy list it again)."""
    if remember:
        with state_lock:
            hidden = read_json(HIDDEN_FILE, [])
            if session_id not in hidden:
                write_json(HIDDEN_FILE, (hidden + [session_id])[-2000:])
    if os.path.exists(SUMMARIES_DB):
        try:
            db = sqlite3.connect(SUMMARIES_DB, timeout=5)
            try:
                db.execute('DELETE FROM conversation_summaries WHERE conversation_id = ?', (session_id,))
                db.commit()
            finally:
                db.close()
        except sqlite3.Error as e:
            log('session %s still in agy\'s list: %s' % (session_id, e))
    for suffix in ('.db', '.db-wal', '.db-shm', '.pb'):
        try:
            os.remove(os.path.join(AGY_DIR, 'conversations', session_id + suffix))
        except OSError:
            pass
    for folder in (os.path.join(AGY_DIR, 'brain', session_id), os.path.join(IMAGES_DIR, session_id)):
        shutil.rmtree(folder, ignore_errors=True)
    with history_lock:
        try:
            os.remove(history_path(session_id))
        except OSError:
            pass
    with _search_lock:
        _search_cache.pop(session_id, None)


def delete_all():
    """Deletes every conversation: agy is stopped meanwhile (it keeps its list open), then its
    conversations, their files and its list go, and what is kept here of them."""
    with chats_lock:
        gone = list(chats.values()) + list(loose.values())
        chats.clear()
        chats_by_session.clear()
        loose.clear()
    for chat in gone:
        chat.abort()
    agy.stop('deleting every conversation')
    for folder in ('conversations', 'brain'):
        path = os.path.join(AGY_DIR, folder)
        for name in os.listdir(path) if os.path.isdir(path) else []:
            target = os.path.join(path, name)
            if os.path.isdir(target) and not os.path.islink(target):
                shutil.rmtree(target, ignore_errors=True)
            else:
                try:
                    os.remove(target)
                except OSError:
                    pass
    for suffix in ('', '-wal', '-shm'):
        try:
            os.remove(SUMMARIES_DB + suffix)
        except OSError:
            pass
    with history_lock:
        for folder in (HISTORY_DIR, IMAGES_DIR):
            shutil.rmtree(folder, ignore_errors=True)
    with _search_lock:
        _search_cache.clear()
    for path in (TITLES_FILE, HIDDEN_FILE):
        try:
            os.remove(path)
        except OSError:
            pass
        with _json_cache_lock:
            _json_cache.pop(path, None)
    log('every conversation deleted')


# ---- agy itself: its version, its sign-in ----

agy_version = ''


def file_stamp(*paths):
    out = []
    for p in paths:
        try:
            st = os.stat(p)
            out.append('%d-%d' % (st.st_size, st.st_mtime))
        except OSError:
            out.append('-')
    return '/'.join(out)


def find_agy_info():
    """agy's version, asked once per agy installed (kept in INFO_FILE)."""
    global agy_version
    path = os.path.realpath(agy_path())
    stamp = file_stamp(path)
    saved = read_json(INFO_FILE, {})
    if saved.get('stamp') == stamp:
        agy_version = saved.get('version', '')
        return
    if not os.path.exists(path):
        return
    try:
        out = subprocess.run([agy_path(), '--version'], capture_output=True, text=True, timeout=120, env=agy_env(),
                             stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.SubprocessError):
        return
    agy_version = out.strip().split()[-1] if out.strip() else ''
    if agy_version:
        try:
            write_json(INFO_FILE, {'stamp': stamp, 'version': agy_version})
        except OSError:
            pass


def api_key_mode():
    return agy_settings().get('modelProvider') == 'gemini' and bool(gemini_key() or os.environ.get('GEMINI_API_KEY'))


def signed_in():
    return os.path.exists(TOKEN_FILE) or api_key_mode()


def write_secret_bytes(path, raw):
    """Writes sensitive bytes (OAuth token, API key) atomically with mode 0600."""
    parent = os.path.dirname(path)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    part = '%s.%d.%s.tmp' % (path, os.getpid(), uuid.uuid4().hex[:8])
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(raw)
        os.chmod(part, 0o600)
        os.replace(part, path)
    except Exception:
        try:
            os.remove(part)
        except OSError:
            pass
        raise


def token_slot(email):
    """Where a saved Google account's OAuth token is stored (hashed filename: safe for any email string)."""
    digest = hashlib.sha256(email.strip().lower().encode('utf-8')).hexdigest()[:24]
    return os.path.join(ACCOUNTS_DIR, digest + '.token')


def token_email(path=TOKEN_FILE):
    """Email embedded in agy's OAuth token file (when present, e.g. real agy including terminal sign-ins)."""
    data = read_json(path, {})
    e = data.get('id_token_email')
    return e.strip() if isinstance(e, str) else ''


def active_google_email():
    """The signed-in Google account's email when TOKEN_FILE is present."""
    if not os.path.exists(TOKEN_FILE):
        return ''
    return token_email(TOKEN_FILE) or str(read_json_cached(ACCOUNT_FILE, {}).get('email') or '').strip()


def _saved_accounts_locked():
    """Ordered list of saved Google account emails whose token files exist on disk."""
    raw = read_json_cached(ACCOUNTS_INDEX, [])
    out, seen = [], set()
    for item in raw:
        e = item.strip() if isinstance(item, str) else (str(item.get('email') or '').strip() if isinstance(item, dict) else '')
        if not e:
            continue
        low = e.lower()
        if low in seen:
            continue
        slot = token_slot(e)
        try:
            if os.path.isfile(slot) and os.path.getsize(slot) > 0:
                seen.add(low)
                out.append(e)
        except OSError:
            pass
    return out


def _write_saved_accounts_locked(emails):
    os.makedirs(ACCOUNTS_DIR, mode=0o700, exist_ok=True)
    try:
        os.chmod(ACCOUNTS_DIR, 0o700)
    except OSError:
        pass
    write_json(ACCOUNTS_INDEX, emails, mode=0o600)


def _save_current_token_locked(email_override=''):
    """Snapshots the active TOKEN_FILE into the saved-accounts vault (preserving refreshed tokens)."""
    if not os.path.exists(TOKEN_FILE):
        return
    email = (email_override or active_google_email()).strip()
    if not email:
        return
    try:
        with open(TOKEN_FILE, 'rb') as f:
            raw = f.read()
    except OSError:
        return
    if not raw:
        return
    os.makedirs(ACCOUNTS_DIR, mode=0o700, exist_ok=True)
    try:
        os.chmod(ACCOUNTS_DIR, 0o700)
    except OSError:
        pass
    write_secret_bytes(token_slot(email), raw)
    saved = _saved_accounts_locked()
    low = email.lower()
    if not any(x.lower() == low for x in saved):
        saved.append(email)
    else:
        saved = [email if x.lower() == low else x for x in saved]
    _write_saved_accounts_locked(saved)


def _remove_saved_account_locked(email):
    """Deletes a saved Google account from the vault. Returns (removed, canonical_email)."""
    low = email.strip().lower()
    saved = _saved_accounts_locked()
    kept, removed_email = [], ''
    for x in saved:
        if x.lower() == low:
            removed_email = x
        else:
            kept.append(x)
    slot = token_slot(email)
    had_file = False
    try:
        os.remove(slot)
        had_file = True
    except OSError:
        pass
    if removed_email or os.path.exists(ACCOUNTS_INDEX):
        _write_saved_accounts_locked(kept)
    return bool(removed_email or had_file), removed_email or email.strip()


def _clear_api_key_provider_locked():
    settings = agy_settings()
    if settings.get('modelProvider') == 'gemini':
        settings.pop('modelProvider', None)
        write_json(AGY_SETTINGS, settings)


def _invalidate_models_stamp_locked():
    saved = read_json(MODELS_FILE, {})
    if saved and 'stamp' in saved:
        saved.pop('stamp', None)
        try:
            write_json(MODELS_FILE, saved)
        except OSError:
            pass


def account():
    """Who agy is signed in as, plus the saved Google accounts available for one-tap switching."""
    with state_lock:
        email = active_google_email()
        if email and not os.path.exists(token_slot(email)):
            try:
                _save_current_token_locked(email)
            except OSError:
                pass
        saved = _saved_accounts_locked()
    if api_key_mode():
        return {'signedIn': True, 'method': 'apiKey', 'email': '', 'accounts': saved}
    if os.path.exists(TOKEN_FILE):
        return {'signedIn': True, 'method': 'google', 'email': email, 'accounts': saved}
    return {'signedIn': False, 'method': '', 'email': '', 'accounts': saved}


def agy_command(name):
    """A read-only slash command of agy's (/usage, /credits) as print mode answers it, with no turn, no
    quota spent and no conversation left behind: its structured data, or AgyError in words for the user."""
    try:
        done = subprocess.run([agy_path(), '-p', '/' + name, '--output-format', 'json'], capture_output=True, text=True,
                              timeout=25, env=agy_env(), cwd=DEFAULT_CWD if os.path.isdir(DEFAULT_CWD) else HOME, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise AgyError('agy 没有及时回答')
    except OSError as e:
        raise AgyError('agy 没能运行：%s' % e)
    try:
        out = json.loads(done.stdout)
    except ValueError:
        out = None
    if not isinstance(out, dict) or out.get('status') != 'SUCCESS':
        said = (out.get('response') if isinstance(out, dict) else '') or done.stderr or done.stdout
        lines = [x.strip() for x in str(said).splitlines() if x.strip()]
        raise AgyError(lines[-1][:300] if lines else 'agy 退出了（%s）' % done.returncode)
    data = (out.get('command') or {}).get('data')
    return data if isinstance(data, dict) else {}


def plan_usage():
    """The account's model quota as agy's /usage tells it: per group of models sharing it, each limit
    (the five hours, the week) with the share left and when it is full again; and the AI credits left
    (used when the quota is). {'error': 'login' / 'apiKey' / words} when there is none to tell."""
    if api_key_mode():
        return {'error': 'apiKey'}
    if not os.path.exists(TOKEN_FILE):
        return {'error': 'login'}
    credits = {}

    def read_credits():
        try:
            credits.update(agy_command('credits'))
        except AgyError:
            pass  # the quota is what matters; credits are said only when known

    other = threading.Thread(target=read_credits, daemon=True)
    other.start()
    try:
        data = agy_command('usage')
    except AgyError as e:
        return {'error': str(e)}
    groups = []
    for g in data.get('groups') or []:
        if not isinstance(g, dict):
            continue
        models = re.sub(r'^Models within this group:\s*', '', g.get('description') or '') \
            if (g.get('description') or '').startswith('Models within this group:') else ''
        limits = [{'id': b.get('id', ''), 'window': b.get('window', ''), 'name': b.get('name', ''),
                   'remaining': b.get('remaining_fraction'), 'resetsAt': b.get('reset_time') or ''}
                  for b in g.get('buckets') or [] if isinstance(b, dict)]
        groups.append({'name': g.get('name', ''), 'models': models, 'limits': limits})
    other.join(30)
    out = {'groups': groups}
    if isinstance(credits.get('remaining_credits'), (int, float)):
        out['credits'] = credits['remaining_credits']
    return out


def read_config():
    threading.Thread(target=find_models, daemon=True).start()  # a no-op unless something changed
    saved = read_json_cached(MODELS_FILE, {})
    models = [{k: v for k, v in m.items() if k != 'ids'} for m in saved.get('models', [])]
    base, effort = split_model(saved.get('current', ''))
    efforts = [e for e in EFFORTS if any(e in (m.get('efforts') or []) for m in models)]
    modes = [m for m in ('default', 'acceptEdits', 'auto', 'plan') if APP_MODES[m] in known_modes] or ['default', 'acceptEdits', 'plan']
    who = account()
    return {'models': [m['value'] for m in models], 'defaultModel': base or '', 'auth': who['signedIn'],
            'account': who, 'agyVersion': agy_version, 'claudeVersion': agy_version, 'efforts': efforts,
            'effort': effort or '', 'permissionModes': modes + [BYPASS], 'claudeModels': models,
            'commands': known_commands, 'installed': os.path.exists(agy_path())}


def set_api_key(key):
    """Signs agy in with a Gemini API key (or, with none, takes it out): kept readable by this app
    alone, given to agy as GEMINI_API_KEY, and agy's modelProvider set to gemini."""
    with state_lock:
        settings = agy_settings()
        if key:
            os.makedirs(STATE_DIR, exist_ok=True)
            part = '%s.%d.%s.tmp' % (KEY_FILE, os.getpid(), uuid.uuid4().hex[:8])
            fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                with os.fdopen(fd, 'w') as f:
                    f.write(key)
                os.replace(part, KEY_FILE)
            except Exception:
                try:
                    os.remove(part)
                except OSError:
                    pass
                raise
            settings['modelProvider'] = 'gemini'
        else:
            try:
                os.remove(KEY_FILE)
            except OSError:
                pass
            settings.pop('modelProvider', None)
        write_json(AGY_SETTINGS, settings)
    reset_models_tried()
    agy.restart_when_idle('sign-in changed')


class Login:
    """Google sign-in, as `agy acp --login` does it in a terminal: it gives a link to open, and is
    given the code the page shows after signing in. Run under a pseudo-terminal (it asks there)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.attempt = None  # dict: {'proc', 'fd', 'out', 'done'}

    def start(self):
        import pty
        with self.lock:
            self._cancel_locked()
            if not os.path.exists(agy_path()):
                raise AgyError('agy 还没装好')
            with state_lock:
                _save_current_token_locked()
            env = agy_env()
            env['TERM'] = 'dumb'
            env.pop('DISPLAY', None)
            master, slave = pty.openpty()
            try:
                proc = subprocess.Popen(
                    [agy_path(), 'acp', '--login'],
                    cwd=HOME, env=env, stdin=slave, stdout=slave, stderr=slave,
                    start_new_session=True, close_fds=True,
                )
            except OSError as e:
                os.close(master)
                os.close(slave)
                raise AgyError('没能启动登录进程：%s' % e)
            os.close(slave)
            att = {'proc': proc, 'fd': master, 'out': b'', 'done': None}
            self.attempt = att
            threading.Thread(target=self.read, args=(att,), daemon=True).start()
        deadline = time.time() + 25  # the app waits 30 s for an answer
        while time.time() < deadline:
            text = self._text(att)
            m = re.search(r'https://accounts\.google\.com/\S+', text)
            if m:
                return m.group(0)
            if att['done'] is not None:
                raise AgyError('agy 没有给出登录链接：%s' % text.strip()[-300:])
            time.sleep(0.2)
        self.cancel(att)
        raise AgyError('agy 25 秒没有给出登录链接')

    def _text(self, att):
        with self.lock:
            raw = att['out']
        return re.sub(r'\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\r', '', raw.decode('utf-8', 'replace'))

    def read(self, att):
        proc, fd = att['proc'], att['fd']
        while True:
            try:
                ready, _, _ = select.select([fd], [], [], 1)
                if ready:
                    data = os.read(fd, 65536)
                    if not data:
                        break
                    with self.lock:
                        att['out'] += data
            except OSError:
                break
            if proc.poll() is not None:
                try:
                    while select.select([fd], [], [], 0.2)[0]:
                        data = os.read(fd, 65536)
                        if not data:
                            break
                        with self.lock:
                            att['out'] += data
                except OSError:
                    pass
                break
        status = proc.wait()
        text = self._text(att)
        m = re.search(r'Logged in as (\S+?)\.?\s*$', text, re.M)
        last = [line.strip() for line in text.split('press Enter:')[-1].splitlines() if line.strip()]
        done = (bool(m), m.group(1) if m else (last[-1] if last else 'agy 退出了（%s）' % status)[-300:])
        with self.lock:
            att['done'] = done
            if att['fd'] is not None:
                try:
                    os.close(att['fd'])
                except OSError:
                    pass
                att['fd'] = None

    def code(self, code):
        """Gives agy the code (or the page's address); (ok, the account or what went wrong)."""
        with self.lock:
            att = self.attempt
            if att is None or att['fd'] is None or att['done'] is not None:
                return False, '登录已经结束了，请重新开始'
            try:
                os.write(att['fd'], code.strip().encode('utf-8') + b'\r')
            except OSError:
                return False, '登录已经结束了，请重新开始'
        deadline = time.time() + 25
        while att['done'] is None and time.time() < deadline:
            time.sleep(0.2)
        if att['done'] is None:
            self.cancel(att)
            return False, 'agy 25 秒没有回应，请重新开始'
        ok, text = att['done']
        if ok:
            with state_lock:
                write_json(ACCOUNT_FILE, {'email': text})
                _save_current_token_locked(text)
                _clear_api_key_provider_locked()
                _invalidate_models_stamp_locked()
            reset_models_tried()
            log('signed in as %s' % text)
            agy.restart_when_idle('signed in')
            voice_dictator.stop('signed in')
            threading.Thread(target=find_models, daemon=True).start()
            hub.emit('', 'config')
        else:
            log('sign-in failed: %s' % text)
        return ok, text

    def _cancel_locked(self, target=None):
        att = target if target is not None else self.attempt
        if att is not None and att['done'] is None:
            try:
                att['proc'].kill()
            except OSError:
                pass
        if target is None or self.attempt is target:
            self.attempt = None

    def cancel(self, target=None):
        with self.lock:
            self._cancel_locked(target)


login = Login()


def switch_account(email):
    """Switches agy to a previously saved Google account without re-running browser OAuth."""
    wanted = email.strip() if isinstance(email, str) else ''
    if not wanted:
        raise AgyError('缺少账号邮箱')
    already = False
    with state_lock:
        saved = _saved_accounts_locked()
        target = next((x for x in saved if x.lower() == wanted.lower()), None)
        if target is None:
            raise LookupError('找不到这个账号，请重新登录')
        cur = active_google_email()
        if not api_key_mode() and cur and cur.lower() == target.lower():
            already = True
        else:
            if cur and cur.lower() != target.lower():
                _save_current_token_locked(cur)
            try:
                with open(token_slot(target), 'rb') as f:
                    raw = f.read()
            except OSError:
                raise LookupError('找不到这个账号，请重新登录')
            if not raw:
                raise AgyError('账号登录信息已失效，请重新登录')
            write_secret_bytes(TOKEN_FILE, raw)
            write_json(ACCOUNT_FILE, {'email': target})
            _clear_api_key_provider_locked()
            _invalidate_models_stamp_locked()
    if already:
        return account()
    reset_models_tried()
    log('switched account to %s' % target)
    agy.restart_when_idle('account switched')
    voice_dictator.stop('account switched')
    threading.Thread(target=find_models, daemon=True).start()
    hub.emit('', 'config')
    return account()


def remove_account(email):
    """Removes a saved Google account from this device; if active, signs out of agy too."""
    wanted = email.strip() if isinstance(email, str) else ''
    if not wanted:
        raise AgyError('缺少账号邮箱')
    with state_lock:
        cur = active_google_email()
        is_current_token = bool(cur and cur.lower() == wanted.lower())
        is_active_google = is_current_token and not api_key_mode()
        removed, canonical = _remove_saved_account_locked(wanted)
        if not removed and not is_current_token:
            raise LookupError('找不到这个账号')
        if is_current_token and not is_active_google:
            for path in (TOKEN_FILE, ACCOUNT_FILE):
                try:
                    os.remove(path)
                except OSError:
                    pass
    if is_active_google:
        logout()
    else:
        log('removed saved account %s' % canonical)
        hub.emit('', 'config')
    return account()


def logout():
    """Signs out of the active Google account: agy forgets its token and it is removed from saved accounts."""
    cur = active_google_email()
    try:
        agy.ensure(timeout=30)
        agy.request('logout', {}, timeout=20)
    except (AgyError, RpcError) as e:
        log('logout through agy failed (%s): the token is removed here' % e)
        try:
            os.remove(TOKEN_FILE)
        except OSError:
            pass
    try:
        os.remove(ACCOUNT_FILE)
    except OSError:
        pass
    with state_lock:
        if cur:
            _remove_saved_account_locked(cur)
        _invalidate_models_stamp_locked()
    reset_models_tried()
    agy.restart_when_idle('signed out')
    voice_dictator.stop('signed out')
    hub.emit('', 'config')


# ---- Voice dictation (agy /voice dictate via ANTIGRAVITY_MIC) ----

class VtScreen:
    """Minimal VT100 screen buffer for reading agy's prompt box and status line during dictation."""

    def __init__(self, rows=40, cols=200):
        self.rows, self.cols = rows, cols
        self.grid = [[' '] * cols for _ in range(rows)]
        self.dim = [[False] * cols for _ in range(rows)]
        self.cur_dim = False
        self.r = 0
        self.c = 0
        self.buf = ''

    @staticmethod
    def _width(ch):
        return 2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1

    def _scroll_up(self):
        self.grid.pop(0)
        self.grid.append([' '] * self.cols)
        self.dim.pop(0)
        self.dim.append([False] * self.cols)

    def _scroll_down(self):
        self.grid.pop()
        self.grid.insert(0, [' '] * self.cols)
        self.dim.pop()
        self.dim.insert(0, [False] * self.cols)

    CSI_RE = re.compile(r'\x1b\[([0-9;?<>!=]*?)([A-Za-z@`~])')

    def feed(self, text):
        self.buf += text
        i, n = 0, len(self.buf)
        while i < n:
            ch = self.buf[i]
            if ch == '\x1b':
                if i + 1 >= n:
                    break
                nxt = self.buf[i + 1]
                if nxt == '[':
                    m = self.CSI_RE.match(self.buf, i)
                    if not m:
                        if n - i < 48:
                            break
                        i += 1
                        continue
                    params, cmd = m.group(1), m.group(2)
                    i = m.end()
                    if all(c in '0123456789;' for c in params):
                        ps = [int(x) if x else 0 for x in params.split(';')] if params else [0]
                        p1 = ps[0]
                        p2 = ps[1] if len(ps) > 1 else 0
                        if cmd == 'm':
                            k = 0
                            while k < len(ps):
                                v = ps[k]
                                if v in (0, 22):
                                    self.cur_dim = False
                                elif v in (2, 90):
                                    self.cur_dim = True
                                elif v in (38, 48) and k + 1 < len(ps):
                                    if ps[k + 1] == 5 and k + 2 < len(ps):
                                        if v == 38:
                                            self.cur_dim = (ps[k + 2] == 8 or 232 <= ps[k + 2] <= 246)
                                        k += 2
                                    elif ps[k + 1] == 2 and k + 4 < len(ps):
                                        if v == 38:
                                            r_, g_, b_ = ps[k + 2], ps[k + 3], ps[k + 4]
                                            self.cur_dim = (abs(r_ - g_) < 15 and abs(g_ - b_) < 15 and 60 <= r_ <= 170)
                                        k += 4
                                elif 30 <= v <= 37 or v == 39 or 91 <= v <= 97:
                                    self.cur_dim = False
                                k += 1
                        elif cmd in ('H', 'f'):
                            self.r = max(0, min(self.rows - 1, (p1 or 1) - 1))
                            self.c = max(0, min(self.cols - 1, (p2 or 1) - 1))
                        elif cmd == 'A':
                            self.r = max(0, self.r - (p1 or 1))
                        elif cmd == 'B':
                            self.r = min(self.rows - 1, self.r + (p1 or 1))
                        elif cmd == 'C':
                            self.c = min(self.cols - 1, self.c + (p1 or 1))
                        elif cmd == 'D':
                            self.c = max(0, self.c - (p1 or 1))
                        elif cmd == 'I':
                            for _ in range(p1 or 1):
                                self.c = min(self.cols - 1, (self.c + 8) & ~7)
                        elif cmd == 'Z':
                            for _ in range(p1 or 1):
                                self.c = max(0, ((self.c - 1) // 8) * 8) if self.c > 0 else 0
                        elif cmd == 'G':
                            self.c = max(0, min(self.cols - 1, (p1 or 1) - 1))
                        elif cmd == 'd':
                            self.r = max(0, min(self.rows - 1, (p1 or 1) - 1))
                        elif cmd == 'K':
                            if p1 == 0:
                                self.grid[self.r][self.c:] = [' '] * (self.cols - self.c)
                                self.dim[self.r][self.c:] = [False] * (self.cols - self.c)
                            elif p1 == 1:
                                end = min(self.c + 1, self.cols)
                                self.grid[self.r][:end] = [' '] * end
                                self.dim[self.r][:end] = [False] * end
                            elif p1 == 2:
                                self.grid[self.r] = [' '] * self.cols
                                self.dim[self.r] = [False] * self.cols
                        elif cmd == 'J':
                            if p1 == 0:
                                self.grid[self.r][self.c:] = [' '] * (self.cols - self.c)
                                self.dim[self.r][self.c:] = [False] * (self.cols - self.c)
                                for rr in range(self.r + 1, self.rows):
                                    self.grid[rr] = [' '] * self.cols
                                    self.dim[rr] = [False] * self.cols
                            elif p1 == 2:
                                self.grid = [[' '] * self.cols for _ in range(self.rows)]
                                self.dim = [[False] * self.cols for _ in range(self.rows)]
                        elif cmd == 'L':
                            for _ in range(min(p1 or 1, self.rows - self.r)):
                                self.grid.pop()
                                self.grid.insert(self.r, [' '] * self.cols)
                                self.dim.pop()
                                self.dim.insert(self.r, [False] * self.cols)
                        elif cmd == 'M':
                            for _ in range(min(p1 or 1, self.rows - self.r)):
                                self.grid.pop(self.r)
                                self.grid.append([' '] * self.cols)
                                self.dim.pop(self.r)
                                self.dim.append([False] * self.cols)
                        elif cmd == 'P':
                            cnt = min(p1 or 1, self.cols - self.c)
                            del self.grid[self.r][self.c:self.c + cnt]
                            self.grid[self.r].extend([' '] * cnt)
                            del self.dim[self.r][self.c:self.c + cnt]
                            self.dim[self.r].extend([False] * cnt)
                        elif cmd == 'X':
                            cnt = min(p1 or 1, self.cols - self.c)
                            self.grid[self.r][self.c:self.c + cnt] = [' '] * cnt
                            self.dim[self.r][self.c:self.c + cnt] = [False] * cnt
                    continue
                elif nxt == ']':
                    end = self.buf.find('\x07', i + 2)
                    st = self.buf.find('\x1b\\', i + 2)
                    if end >= 0 and (st < 0 or end < st):
                        i = end + 1
                        continue
                    if st >= 0:
                        i = st + 2
                        continue
                    if n - i < 512:
                        break
                    i += 1
                    continue
                elif nxt == 'M':
                    if self.r > 0:
                        self.r -= 1
                    else:
                        self._scroll_down()
                    i += 2
                    continue
                elif nxt in ('(', ')', '*', '+'):
                    if i + 2 >= n:
                        break
                    i += 3
                    continue
                else:
                    i += 2
                    continue
            elif ch == '\r':
                self.c = 0
                i += 1
            elif ch == '\n':
                self.r += 1
                if self.r >= self.rows:
                    self._scroll_up()
                    self.r = self.rows - 1
                i += 1
            elif ch == '\b':
                self.c = max(0, self.c - 1)
                i += 1
            elif ch == '\t':
                self.c = min(self.cols - 1, (self.c + 8) & ~7)
                i += 1
            elif ord(ch) < 32:
                i += 1
            else:
                w = self._width(ch)
                if self.c + w > self.cols:
                    self.c = 0
                    self.r += 1
                    if self.r >= self.rows:
                        self._scroll_up()
                        self.r = self.rows - 1
                self.grid[self.r][self.c] = ch
                self.dim[self.r][self.c] = self.cur_dim
                if w == 2 and self.c + 1 < self.cols:
                    self.grid[self.r][self.c + 1] = ''
                    self.dim[self.r][self.c + 1] = self.cur_dim
                self.c += w
                i += 1
        self.buf = self.buf[i:]

    def snapshot(self):
        """Returns (prompt_text_or_None, status_below, above_lines, all_text)."""
        lines = [''.join(row).rstrip() for row in self.grid]
        bars = [idx for idx, line in enumerate(lines) if len(line) >= 20 and set(line) == {'─'}]
        top = bot = None
        for idx in range(len(bars) - 1, -1, -1):
            b = bars[idx]
            if b + 1 < len(lines) and lines[b + 1].startswith('>'):
                top = b
                bot = bars[idx + 1] if idx + 1 < len(bars) else len(lines)
                break
        if top is None:
            return None, '', [], '\n'.join(lines)
        parts = []
        for idx, rr in enumerate(range(top + 1, bot)):
            row_chars = self.grid[rr]
            row_dim = self.dim[rr]
            start = 2 if (idx == 0 and ''.join(row_chars[:2]) == '> ') else (1 if (idx == 0 and row_chars[0] == '>') else 0)
            non_space = [(c, d) for c, d in zip(row_chars[start:], row_dim[start:]) if c and c != ' ']
            if non_space and all(d for _, d in non_space):
                continue
            txt = ''.join(row_chars[start:]).rstrip()
            if idx > 0 and txt.startswith('  '):
                txt = txt[2:]
            if txt:
                parts.append(txt)
        joined = ''
        for p in parts:
            if not joined:
                joined = p
            elif ord(joined[-1]) < 128 and ord(p[0]) < 128 and (joined[-1].isalnum() or joined[-1] in '.,!?;:') and p[0].isalnum():
                joined += ' ' + p
            else:
                joined += p
        status = '\n'.join(lines[bot + 1:bot + 4]).strip() if bot < len(lines) else ''
        above = [l.strip() for l in lines[max(0, top - 6):top] if l.strip()]
        return joined.strip(), status, above, '\n'.join(lines)


class VoiceDictator:
    """Drives a warm interactive `agy` PTY with ANTIGRAVITY_MIC so the app can use Antigravity's
    native subscription voice dictation (/voice dictate) with live interim transcripts."""

    def __init__(self):
        self.lock = threading.Lock()
        self.ensure_lock = threading.Lock()
        self.run_lock = threading.Lock()
        self.proc = None
        self.fd = None
        self.srv = None
        self.port = 0
        self.screen = None
        self.ready = False
        self.used = 0.0
        self.busy = False
        self.active = None  # dict for the active dictation session

    def _alive(self):
        return self.proc is not None and self.proc.poll() is None and self.fd is not None

    def _write_pty(self, data, raise_err=False):
        with self.lock:
            fd = self.fd
            if fd is None:
                if raise_err:
                    raise AgyError('语音听写进程已断开')
                return False
            try:
                os.write(fd, data)
                return True
            except OSError:
                if raise_err:
                    raise AgyError('语音听写进程已断开')
                return False

    def _mic_accept_loop(self, srv):
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                break
            threading.Thread(target=self._serve_mic, args=(conn,), daemon=True).start()

    def _serve_mic(self, conn):
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        sess = self.active
        if sess is None:
            try:
                conn.close()
            except OSError:
                pass
            return
        sess['mic_connected'].set()
        q = sess['pcm']
        try:
            while not sess['close_mic'].is_set():
                try:
                    chunk = q.get(timeout=0.1)
                except queue.Empty:
                    if sess['eof'].is_set():
                        # Keep feeding silence briefly while agy finishes after Enter
                        chunk = b'\x00' * 3200
                    else:
                        continue
                if chunk is None:
                    break
                conn.sendall(chunk)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _read_pty(self, proc, fd, screen):
        while True:
            try:
                r, _, _ = select.select([fd], [], [], 0.5)
                if r:
                    data = os.read(fd, 16384)
                    if not data:
                        break
                    text = data.decode('utf-8', 'replace')
                    with self.lock:
                        if self.screen is screen:
                            screen.feed(text)
                            self._on_screen_locked()
            except OSError:
                break
            if proc.poll() is not None:
                break
        with self.lock:
            if self.proc is proc:
                self.ready = False
                if self.fd is fd:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    self.fd = None
                self.proc = None
        try:
            proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _on_screen_locked(self):
        sess = self.active
        if sess is None or self.screen is None or not self.ready:
            return
        box, status, above, _ = self.screen.snapshot()
        sess['last_status'] = status
        sess['last_above'] = above
        if 'Recording' in status and not sess['recording']:
            sess['recording'] = True
            hub.emit('', 'voice', {'id': sess['id'], 'state': 'recording'})
        if (sess['recording'] or sess['finishing']) and box is not None and box != sess['last_text']:
            sess['last_text'] = box
            if box and not sess['cancelled'].is_set():
                hub.emit('', 'voice', {'id': sess['id'], 'state': 'partial', 'text': box})
        if sess['finishing'] and 'Recording' not in status and 'Connecting' not in status and 'Finishing' not in status:
            sess['finished'].set()

    def ensure(self, timeout=20):
        import fcntl
        import pty
        import termios
        if not os.path.exists(agy_path()):
            raise AgyError('agy 还没装好')
        if not os.path.exists(TOKEN_FILE):
            raise AgyError('需要先用 Google 账号登录才能使用 Antigravity 听写')
        with self.ensure_lock:
            with self.lock:
                self.used = time.time()
                if self._alive() and self.ready:
                    return
                if not self._alive():
                    self._stop_locked('restart')
                    os.makedirs(VOICE_DIR, exist_ok=True)
                    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    srv.bind(('127.0.0.1', 0))
                    srv.listen(4)
                    port = srv.getsockname()[1]
                    master, slave = pty.openpty()
                    try:
                        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 200, 0, 0))
                    except OSError:
                        pass
                    env = agy_env()
                    env.pop('GEMINI_API_KEY', None)
                    env['ANTIGRAVITY_MIC'] = '127.0.0.1:%d' % port
                    env['TERM'] = 'xterm-256color'
                    try:
                        proc = subprocess.Popen(
                            [agy_path()],
                            cwd=VOICE_DIR, env=env, stdin=slave, stdout=slave, stderr=slave,
                            start_new_session=True, close_fds=True,
                        )
                    except OSError as e:
                        os.close(master)
                        os.close(slave)
                        srv.close()
                        raise AgyError('没能启动语音听写进程：%s' % e)
                    os.close(slave)
                    screen = VtScreen(40, 200)
                    self.proc, self.fd, self.srv, self.port, self.screen, self.ready = proc, master, srv, port, screen, False
                    threading.Thread(target=self._mic_accept_loop, args=(srv,), daemon=True).start()
                    threading.Thread(target=self._read_pty, args=(proc, master, screen), daemon=True).start()
                    log('voice: started agy pid %d on mic port %d' % (proc.pid, port))
            deadline = time.time() + timeout
            trusted = False
            dismissed = False
            while time.time() < deadline:
                with self.lock:
                    if not self._alive():
                        raise AgyError('语音听写进程启动后退出了')
                    if self.ready:
                        return
                    box, status, _, all_txt = self.screen.snapshot()
                if 'Do you trust' in all_txt and not trusted:
                    trusted = True
                    self._write_pty(b'\r')
                elif box is not None and '? for shortcuts' in all_txt:
                    if 'esc to dismiss' in all_txt and not dismissed:
                        dismissed = True
                        self._write_pty(b'\x1b')
                        time.sleep(0.15)
                        continue
                    with self.lock:
                        self.ready = True
                    log('voice: agy dictation ready')
                    return
                time.sleep(0.1)
            self.stop('startup timeout')
            raise AgyError('Antigravity 听写启动超时，请重试')

    def warm(self):
        if not os.path.exists(agy_path()) or not os.path.exists(TOKEN_FILE):
            return
        # Let agy acp finish its cold-start prime first so both agy processes don't fight for CPU/IO.
        deadline = time.time() + 6.0
        while time.time() < deadline and agy.primed_gen != agy.gen and signed_in():
            time.sleep(0.2)
        try:
            self.ensure(timeout=20)
        except AgyError as e:
            log('voice: warm failed: %s' % e)

    def _wait_idle(self, timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                if not self._alive() or self.screen is None:
                    return False
                _, status, _, _ = self.screen.snapshot()
            if 'Recording' not in status and 'Connecting' not in status and 'Finishing' not in status:
                return True
            time.sleep(0.05)
        return False

    def _abort_active(self):
        self._write_pty(b'\x1b')
        if not self._wait_idle(3.0):
            self.stop('stuck after cancel')
            return
        self._clear_prompt()

    def _clear_prompt(self):
        with self.lock:
            screen = self.screen
            if self.fd is None or screen is None:
                return
            box, _, _, _ = screen.snapshot()
        if not box:
            return
        if not self._write_pty(b'\x1f'):  # Ctrl+_ undo dictation
            return
        time.sleep(0.15)
        with self.lock:
            screen = self.screen
            if screen is None:
                return
            box, _, _, _ = screen.snapshot()
        if box:
            self._write_pty(b'\x01\x0b\x15')  # Ctrl+A Ctrl+K Ctrl+U
            time.sleep(0.15)

    def cancel(self, vid=None):
        with self.lock:
            sess = self.active
            if sess is None or (vid and sess['id'] != vid):
                return False
            sess['cancelled'].set()
            sess['eof'].set()
            sess['close_mic'].set()
            fd = self.fd
            if fd is not None:
                try:
                    os.write(fd, b'\x1b')
                except OSError:
                    pass
        return True

    def transcribe(self, rfile, vid):
        """Streams raw 16kHz 16-bit mono PCM from rfile to agy /voice dictate until EOF, then
        returns {'ok': True, 'text': ...} or raises AgyError."""
        self.cancel()
        with self.run_lock:
            self.busy = True
            self.used = time.time()
            sess = {
                'id': vid,
                'pcm': queue.Queue(maxsize=200),
                'eof': threading.Event(),
                'close_mic': threading.Event(),
                'cancelled': threading.Event(),
                'mic_connected': threading.Event(),
                'finished': threading.Event(),
                'recording': False,
                'finishing': False,
                'last_text': '',
                'last_status': '',
                'last_above': [],
            }
            bytes_in = [0]

            def read_audio():
                try:
                    while not sess['cancelled'].is_set():
                        chunk = rfile.read1(4096) if hasattr(rfile, 'read1') else rfile.read(3200)
                        if not chunk:
                            break
                        bytes_in[0] += len(chunk)
                        try:
                            sess['pcm'].put_nowait(chunk)
                        except queue.Full:
                            try:
                                sess['pcm'].get_nowait()
                            except queue.Empty:
                                pass
                            try:
                                sess['pcm'].put_nowait(chunk)
                            except queue.Full:
                                pass
                except OSError:
                    sess['cancelled'].set()
                finally:
                    sess['eof'].set()

            reader = threading.Thread(target=read_audio, daemon=True)
            reader.start()
            with self.lock:
                self.active = sess
            try:
                hub.emit('', 'voice', {'id': vid, 'state': 'connecting'})
                self.ensure(timeout=20)
                if sess['cancelled'].is_set():
                    return {'ok': True, 'text': '', 'cancelled': True}
                if not self._wait_idle(1.5):
                    self._abort_active()
                    self.ensure(timeout=20)
                self._clear_prompt()
                if sess['cancelled'].is_set():
                    return {'ok': True, 'text': '', 'cancelled': True}
                self._write_pty(b'\x1b[15~', raise_err=True)  # F5: start dictation
                # Wait for audio stream EOF (user released button / tapped stop) or cancel
                while not sess['eof'].is_set() and not sess['cancelled'].is_set():
                    with self.lock:
                        if not self._alive():
                            raise AgyError('语音听写进程意外退出')
                    time.sleep(0.05)
                if sess['cancelled'].is_set():
                    sess['close_mic'].set()
                    self._abort_active()
                    return {'ok': True, 'text': '', 'cancelled': True}
                # Wait briefly for agy to connect & drain any buffered audio spoken while connecting
                t_drain = time.time()
                while not sess['mic_connected'].is_set() and time.time() - t_drain < 3.0 and not sess['cancelled'].is_set():
                    time.sleep(0.05)
                while not sess['pcm'].empty() and time.time() - t_drain < 4.0 and not sess['cancelled'].is_set():
                    time.sleep(0.05)
                if bytes_in[0] < 3200:
                    sess['close_mic'].set()
                    self._abort_active()
                    return {'ok': True, 'text': '', 'notice': '说话时间太短'}
                hub.emit('', 'voice', {'id': vid, 'state': 'finishing'})
                with self.lock:
                    sess['finishing'] = True
                    # Check if agy already exited recording (e.g. error/timeout)
                    st = sess['last_status']
                    if 'Recording' not in st and 'Connecting' not in st and 'Finishing' not in st and sess['recording']:
                        sess['finished'].set()
                if not sess['finished'].is_set():
                    self._write_pty(b'\r')  # Enter: finish & insert transcript
                sess['finished'].wait(timeout=10.0)
                time.sleep(0.15)
                sess['close_mic'].set()
                with self.lock:
                    box, status, above, _ = self.screen.snapshot() if self.screen else ('', '', [], '')
                final_text = (box or sess['last_text'] or '').strip()
                notice = ''
                if not final_text:
                    for line in reversed(above):
                        if '⎿' in line:
                            notice = line.split('⎿', 1)[1].strip()
                            break
                        if any(k in line.lower() for k in ('permission', 'sign in', 'logout', 'enterprise', 'business', 'error', 'failed')):
                            notice = line
                            break
                self._clear_prompt()
                if notice and not final_text:
                    if 'permission' in notice.lower() or 'logout' in notice.lower() or 'sign in' in notice.lower():
                        raise AgyError('语音听写缺少授权：请先退出 Google 账号并重新登录')
                    if 'business' in notice.lower() or 'enterprise' in notice.lower():
                        raise AgyError('企业/商业版 Google 账号暂不支持 Antigravity 听写')
                log('voice: done %s (%d chars)' % (vid, len(final_text)))
                hub.emit('', 'voice', {'id': vid, 'state': 'done', 'text': final_text})
                return {'ok': True, 'text': final_text, 'notice': notice}
            finally:
                sess['eof'].set()
                sess['close_mic'].set()
                with self.lock:
                    if self.active is sess:
                        self.active = None
                self.busy = False
                self.used = time.time()

    def reap(self):
        if self.busy:
            return
        with self.lock:
            if self._alive() and self.used and time.time() - self.used > VOICE_IDLE_STOP:
                self._stop_locked('unused for %d minutes' % (VOICE_IDLE_STOP // 60))

    def _stop_locked(self, reason=''):
        proc, fd, srv = self.proc, self.fd, self.srv
        self.proc = self.fd = self.srv = self.screen = None
        self.port = 0
        self.ready = False
        if srv is not None:
            try:
                srv.close()
            except OSError:
                pass
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if proc is not None:
            log('voice: stopping agy pid %d (%s)' % (proc.pid, reason))
            def _reap(p):
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(p.pid, sig)
                    except OSError:
                        try:
                            p.send_signal(sig)
                        except OSError:
                            pass
                    try:
                        p.wait(3)
                        return
                    except subprocess.TimeoutExpired:
                        pass
                try:
                    p.wait()
                except OSError:
                    pass
            threading.Thread(target=_reap, args=(proc,), daemon=True).start()

    def stop(self, reason=''):
        self.cancel()
        with self.lock:
            self._stop_locked(reason)


voice_dictator = VoiceDictator()


def list_dirs(path):
    path = os.path.normpath(os.path.expanduser(path or HOME))
    names = []
    try:
        for name in sorted(os.listdir(path), key=str.lower):
            if not name.startswith('.') and os.path.isdir(os.path.join(path, name)):
                names.append(name)
    except OSError as e:
        return {'path': path, 'parent': os.path.dirname(path) if path != '/' else None, 'dirs': [], 'error': str(e)}
    return {'path': path, 'parent': os.path.dirname(path) if path != '/' else None, 'dirs': names, 'home': HOME}


# ---- HTTP ----

class Handler(BaseHTTPRequestHandler):
    server_version = 'agybridge/%d' % VERSION
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def reply(self, status, data):
        self.status = status
        body = json.dumps(data, ensure_ascii=False).encode('utf-8', 'replace')  # lone surrogates
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get('Content-Length') or 0)
        if n < 0:
            raise ValueError('negative Content-Length')
        parsed = json.loads(self.rfile.read(n) or b'{}') if n else {}
        if not isinstance(parsed, dict):
            raise ValueError('expected JSON object')
        return parsed

    def logged(self, method, run):
        """Runs a request and logs it (not health checks), with its status and time."""
        path = self.path.split('?')[0]
        start = time.time()
        self.status = None
        try:
            run()
        finally:
            if path not in ('/health', '/events'):
                log('%s %s -> %s, %dms' % (method, path, self.status or 'no answer', (time.time() - start) * 1000))

    def do_GET(self):
        self.logged('GET', self.do_get)

    def do_POST(self):
        self.logged('POST', self.do_post)

    def do_get(self):
        try:
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            route = self.GET.get(url.path)
            if route is None:
                return self.reply(404, {'error': 'not found'})
            route(self, q)
        except ValueError as e:
            self.reply(400, {'error': str(e)})
        except AgyError as e:
            self.reply(503, {'error': str(e)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self.failed(e)

    def do_post(self):
        try:
            path = urlparse(self.path).path
            if path == '/upload':
                return self.upload()
            if path == '/voice/audio':
                return self.voice_audio()
            route = self.POST.get(path)
            if route is None:
                return self.reply(404, {'error': 'not found'})
            route(self, self.body())
        except (ValueError, KeyError, TypeError) as e:
            self.reply(400, {'error': 'bad request: %s' % e})
        except AgyError as e:
            self.reply(503, {'error': str(e)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self.failed(e)

    def failed(self, e):
        """An error the bridge did not expect: logged, and answered so the app can show it."""
        import traceback
        log('%s failed:\n%s' % (self.path.split('?')[0], traceback.format_exc()))
        try:
            self.reply(500, {'error': '%s: %s' % (type(e).__name__, e)})
        except OSError:
            pass

    # ---- GET ----

    def get_health(self, q):
        self.reply(200, {'ok': True, 'version': VERSION, 'started': STARTED, 'home': HOME, 'defaultCwd': DEFAULT_CWD,
                         'agy': agy_path() if os.path.exists(agy_path()) else None})

    def get_events(self, q):
        self.events(int(q.get('since') or 0), q.get('bridge'))

    def get_image(self, q):
        found = session_image(q.get('id'), int(q.get('at') or 0), int(q.get('n') or 0))
        if not found:
            return self.reply(404, {'error': 'not found'})
        self.reply(200, {'type': found[0], 'data': found[1]})

    def get_sessions(self, q):
        self.reply(200, {'sessions': list_sessions()})

    def get_search(self, q):
        self.reply(200, {'results': search_sessions(q.get('q', ''))})

    def get_history(self, q):
        data = load_history(q.get('id', ''), fast=q.get('fast') == '1')
        self.reply(200 if data else 404, data or {'error': 'not found'})

    def get_config(self, q):
        self.reply(200, read_config())

    def get_launch(self, q):
        self.reply(200, launch.view())

    def get_account(self, q):
        self.reply(200, account())

    def get_usage(self, q):
        self.reply(200, plan_usage())

    def get_memory(self, q):
        path = memory_path(q.get('scope'), q.get('cwd'))
        try:
            with open(path, encoding='utf-8') as f:
                text = f.read()
        except OSError:
            text = ''
        self.reply(200, {'path': path, 'text': text})

    def get_dirs(self, q):
        self.reply(200, list_dirs(q.get('path')))

    # ---- POST ----

    def post_send(self, data):
        mid = data.get('mid')
        if not first_sight(mid):
            return self.reply(200, {'ok': True, 'repeated': True})
        chat = chat_for(data['chat'], data.get('sessionId'))
        chat.send(data['content'], cwd=data.get('cwd'), model=data.get('model') or None,
                  session_id=data.get('sessionId'), effort=data.get('effort') or None,
                  mode=data.get('mode') or None, message_uuid=data.get('uuid') or None)
        self.reply(200, {'ok': True, 'sessionId': chat.session_id})

    def post_open(self, data):
        # A conversation was opened: agy is made ready for it (started, if it is not).
        threading.Thread(target=warm, daemon=True).start()
        self.reply(200, {'ok': True})

    def post_launch(self, data):
        launch.set(data)
        self.reply(200, launch.view())

    def post_memory(self, data):
        target = memory_path(data.get('scope'), data.get('cwd'))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, 'w', encoding='utf-8') as f:
            f.write(data.get('text') or '')
        self.reply(200, {'ok': True, 'path': target})

    def post_auth(self, data):
        provider, key = str(data.get('provider') or 'gemini').strip(), str(data.get('key') or '').strip()
        if provider != 'gemini':
            return self.reply(400, {'error': 'only a Gemini API key is kept here'})
        set_api_key(key)
        threading.Thread(target=find_models, daemon=True).start()
        hub.emit('', 'config')
        self.reply(200, {'ok': True, 'account': account()})

    def post_login(self, data):
        self.reply(200, {'url': login.start()})

    def post_login_code(self, data):
        ok, text = login.code(str(data.get('code') or ''))
        self.reply(200 if ok else 400, {'ok': ok, 'email': text} if ok else {'error': text})

    def post_logout(self, data):
        logout()
        self.reply(200, {'ok': True, 'account': account()})

    def post_account_switch(self, data):
        try:
            self.reply(200, {'ok': True, 'account': switch_account(data.get('email'))})
        except LookupError as e:
            self.reply(404, {'error': str(e)})
        except AgyError as e:
            self.reply(400, {'error': str(e)})

    def post_account_remove(self, data):
        try:
            self.reply(200, {'ok': True, 'account': remove_account(data.get('email'))})
        except LookupError as e:
            self.reply(404, {'error': str(e)})
        except AgyError as e:
            self.reply(400, {'error': str(e)})

    def post_cancel(self, data):
        chat = chat_for(data['chat'], data.get('sessionId'), create=False)
        self.reply(200, {'cancelled': True if chat is None else chat.cancel(str(data.get('uuid') or ''))})

    def post_context(self, data):
        chat = chat_for(data['chat'], data.get('sessionId'), create=False)
        self.reply(200, {'context': chat.context_usage() if chat is not None else None})

    def post_stop(self, data):
        chat = chat_for(data['chat'], data.get('sessionId'), create=False)
        self.reply(200, {'ok': True, 'stopped': bool(chat and chat.stop())})

    def post_delete(self, data):
        self.reply(200, {'ok': delete_session(data.get('id', ''))})

    def post_delete_all(self, data):
        delete_all()
        self.reply(200, {'ok': True})

    def post_rewind(self, data):
        self.reply(409, {'error': 'Antigravity 不能回到之前的消息'})

    def post_delete_message(self, data):
        self.reply(409, {'error': 'Antigravity 不能删掉对话里的单条消息'})

    def post_title(self, data):
        session_id = str(data.get('id') or '')
        title = ' '.join(str(data.get('title') or '').split())[:200]
        if not title or not safe_id(session_id):
            return self.reply(400, {'error': 'bad request'})
        set_title(session_id, title)
        self.reply(200, {'ok': True})

    def post_permission(self, data):
        chat = chat_for(data['chat'], data.get('sessionId'), create=False)
        if chat is None or not chat.answer(str(data.get('requestId') or ''), bool(data.get('allow'))):
            return self.reply(409, {'error': 'not asked any more'})
        self.reply(200, {'ok': True})

    def post_mkdir(self, data):
        try:
            path = os.path.expanduser(str(data['path']))
            os.makedirs(path, exist_ok=True)
        except OSError as e:
            return self.reply(400, {'error': str(e)})
        self.reply(200, {'ok': True, 'path': path})

    def post_voice_warm(self, data):
        threading.Thread(target=voice_dictator.warm, daemon=True).start()
        self.reply(200, {'ok': True, 'ready': voice_dictator.ready})

    def post_voice_cancel(self, data):
        vid = str(data.get('id') or '') or None
        self.reply(200, {'ok': True, 'cancelled': voice_dictator.cancel(vid)})

    def voice_audio(self):
        self.close_connection = True
        q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        vid = q.get('id') or uuid.uuid4().hex[:8]
        result = voice_dictator.transcribe(self.rfile, vid)
        self.reply(200, result)

    def upload(self):
        """A file the app attached, its bytes as the body (streamed to disk, never held whole), into
        the folder given; never over a file already there: name (1).ext, name (2).ext …"""
        q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        folder = os.path.expanduser(q.get('cwd') or DEFAULT_CWD)
        name = os.path.basename(str(q.get('name') or 'file')).strip() or 'file'
        try:
            left = int(self.headers.get('Content-Length') or 0)
            if left < 0:
                raise ValueError('negative Content-Length')
        except ValueError as e:
            return self.reply(400, {'error': str(e)})
        created = None
        try:
            os.makedirs(folder, exist_ok=True)
            stem, ext = os.path.splitext(name)
            target, n = os.path.join(folder, name), 1
            while True:
                try:
                    f = open(target, 'xb')
                    created = target
                    break
                except FileExistsError:
                    target, n = os.path.join(folder, '%s (%d)%s' % (stem, n, ext)), n + 1
            with f:
                while left > 0:
                    chunk = self.rfile.read(min(left, 1 << 16))
                    if not chunk:
                        raise OSError('the upload was cut short')
                    f.write(chunk)
                    left -= len(chunk)
        except OSError as e:
            if created is not None:
                try:
                    os.remove(created)
                except OSError:
                    pass
            return self.reply(400, {'error': str(e)})
        self.reply(200, {'ok': True, 'path': target})

    def events(self, since, bridge=None):
        """The events after `since`, then each as it happens. `bridge` is the start the app's count
        is from: another bridge's count starts over here. Events dropped meanwhile (only the last
        ones are kept) are told as one 'gap'."""
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True
        try:
            if since < 0:  # only what happens from now on
                since = hub.seq
            elif since > hub.seq or (bridge and bridge != str(STARTED)):  # another bridge's count: start over
                since = 0
            self.wfile.write((': hello %s\n\n' % STARTED).encode())

            def send(e):
                self.wfile.write(b'data: ' + json.dumps(e, ensure_ascii=False).encode('utf-8', 'replace') + b'\n\n')
            oldest = hub.oldest()
            if since < oldest - 1:
                since = oldest - 1
                send({'seq': since, 'chat': '', 'kind': 'gap', 'data': None})
            self.wfile.flush()
            log('events: app connected')
            while True:
                batch = hub.after(since, 5)  # a ping at least every 5 s: the app can tell a live bridge
                if not batch:
                    self.wfile.write(b': ping\n\n')
                elif batch[0]['seq'] > since + 1:
                    send({'seq': batch[0]['seq'] - 1, 'chat': '', 'kind': 'gap', 'data': None})
                for e in batch:
                    since = e['seq']
                    send(e)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            log('events: app gone')

    # Where each request goes: the path to the method answering it, given the query (GET) or the
    # body (POST). /upload is not here: its body is the file, read as it is streamed to disk.
    GET = {'/health': get_health, '/events': get_events, '/image': get_image, '/sessions': get_sessions,
           '/search': get_search, '/history': get_history, '/config': get_config, '/launch': get_launch,
           '/memory': get_memory, '/dirs': get_dirs, '/account': get_account, '/usage': get_usage}
    POST = {'/context': post_context, '/send': post_send, '/open': post_open, '/launch': post_launch,
            '/memory': post_memory, '/auth': post_auth, '/login': post_login, '/login-code': post_login_code,
            '/logout': post_logout, '/account/switch': post_account_switch, '/account/remove': post_account_remove,
            '/cancel': post_cancel, '/stop': post_stop, '/delete': post_delete, '/delete-all': post_delete_all,
            '/rewind': post_rewind, '/delete-message': post_delete_message, '/title': post_title,
            '/permission': post_permission, '/mkdir': post_mkdir,
            '/voice/warm': post_voice_warm, '/voice/cancel': post_voice_cancel}


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    request_queue_size = 64  # the app opens many connections at once when it starts

    def server_bind(self):
        """Over a socket file left by a bridge before (only one bridge runs: replace_running_bridge),
        readable by this app alone."""
        try:
            os.remove(self.server_address)
        except OSError:
            pass
        old = os.umask(0o177)
        try:
            super().server_bind()
        finally:
            os.umask(old)


def running_bridge():
    """The pid of the bridge running before this one, if it still runs (not a process that got its pid since)."""
    try:
        with open(PID_FILE) as f:
            pid = int(f.read().strip())
        with open('/proc/%d/cmdline' % pid, 'rb') as f:
            if b'bridge.py' not in f.read():
                return None
        return pid if pid != os.getpid() else None
    except (OSError, ValueError):
        return None


_lock = None


def replace_running_bridge():
    """Leaves exactly one bridge, the newest: it stops the one running and takes the lock that
    bridge held for its life, so two started at once end with one."""
    import fcntl
    global _lock
    os.makedirs(STATE_DIR, exist_ok=True)
    _lock = open(LOCK_FILE, 'a')
    deadline = time.time() + 12
    while True:
        old = running_bridge()
        if old:
            try:
                os.kill(old, signal.SIGTERM)
            except OSError:
                pass
        try:
            fcntl.flock(_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.time() > deadline:
                log('exit pid %d: another bridge keeps the lock' % os.getpid())
                sys.exit(1)
            time.sleep(0.2)
    old = running_bridge()  # a bridge from before the lock
    if old:
        try:
            os.kill(old, signal.SIGTERM)
            for _ in range(30):
                time.sleep(0.1)
                os.kill(old, 0)
        except OSError:
            pass
    part = '%s.%d.tmp' % (PID_FILE, os.getpid())
    with open(part, 'w') as f:
        f.write(str(os.getpid()))
    os.replace(part, PID_FILE)


_log_fp = None
_log_bytes = 0
_log_path = None


def log(text):
    global _log_fp, _log_bytes, _log_path
    with log_lock:
        try:
            if _log_fp is None or _log_path != LOG_FILE:
                if _log_fp is not None:
                    try:
                        _log_fp.close()
                    except OSError:
                        pass
                    _log_fp = None
                os.makedirs(os.path.dirname(LOG_FILE) or STATE_DIR, exist_ok=True)
                try:
                    _log_bytes = os.path.getsize(LOG_FILE)
                except OSError:
                    _log_bytes = 0
                _log_fp = open(LOG_FILE, 'a', encoding='utf-8', errors='replace')
                _log_path = LOG_FILE
            if _log_bytes > 200_000:
                _log_fp.close()
                _log_fp = None
                try:
                    os.replace(LOG_FILE, LOG_FILE + '.1')
                except OSError:
                    pass
                _log_fp = open(LOG_FILE, 'a', encoding='utf-8', errors='replace')
                _log_bytes = 0
            line = time.strftime('%Y-%m-%d %H:%M:%S ') + text + '\n'
            _log_fp.write(line)
            _log_fp.flush()
            _log_bytes += len(line.encode('utf-8', 'replace'))
        except OSError:
            if _log_fp is not None:
                try:
                    _log_fp.close()
                except OSError:
                    pass
                _log_fp = None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--socket', default=SOCKET_FILE)
    parser.add_argument('--reason', default='')
    args = parser.parse_args()
    replace_running_bridge()
    log('start pid %d version %d (%s)' % (os.getpid(), VERSION, args.reason or 'started by hand'))
    os.makedirs(DEFAULT_CWD, exist_ok=True)

    def shutdown(signum, _frame):
        try:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            log('stop pid %d: signal %d' % (os.getpid(), signum))
            login.cancel()
            voice_dictator.stop('bridge shutdown')
            proc = agy.proc
            if proc is not None:
                try:
                    proc.stdin.close()
                    proc.wait(3)
                except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except OSError:
                        pass
        finally:
            os._exit(0)
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    def crashed(kind, value, tb):
        import traceback
        log('crash pid %d:\n%s' % (os.getpid(), ''.join(traceback.format_exception(kind, value, tb))))
    sys.excepthook = crashed
    threading.excepthook = lambda a: crashed(a.exc_type, a.exc_value, a.exc_traceback)

    try:
        server = Server(args.socket, Handler)
    except OSError as e:
        log('exit pid %d: cannot listen on %s: %s' % (os.getpid(), args.socket, e))
        sys.exit('cannot listen on %s: %s' % (args.socket, e))
    def startup_warm():
        warm()
        find_models()
    threading.Thread(target=reaper, daemon=True).start()
    threading.Thread(target=startup_warm, daemon=True).start()
    threading.Thread(target=find_agy_info, daemon=True).start()
    server.serve_forever()


if __name__ == '__main__':
    main()
