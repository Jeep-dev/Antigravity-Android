"""Tests for the loopback HTTP/CONNECT observability proxy in bridge.py."""
import importlib.util
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.request

BRIDGE_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../main/assets/bridge.py'))


def load_bridge(home):
    env_old = {k: os.environ.get(k) for k in ('HOME', 'AGY_DATA_DIR', 'HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'NO_PROXY', 'no_proxy')}
    os.environ['HOME'] = home
    for k in ('AGY_DATA_DIR', 'HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'NO_PROXY', 'no_proxy'):
        os.environ.pop(k, None)
    try:
        spec = importlib.util.spec_from_file_location('bridge_proxy_test', BRIDGE_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in env_old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class NetProxyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, 'home')
        os.makedirs(self.home, exist_ok=True)
        self.b = load_bridge(self.home)
        os.makedirs(self.b.STATE_DIR, exist_ok=True)
        self.logs = []
        self.b.log = lambda msg: self.logs.append(msg)

    def tearDown(self):
        self.b.net_proxy.stop()
        self.tmp.cleanup()

    def _start_echo_server(self, delay=0.0):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(('127.0.0.1', 0))
        srv.listen(8)

        def loop():
            while True:
                try:
                    c, _ = srv.accept()
                except OSError:
                    break

                def handle(conn):
                    try:
                        while True:
                            data = conn.recv(65536)
                            if not data:
                                break
                            if delay:
                                time.sleep(delay)
                            conn.sendall(b'ECHO:' + data)
                    except OSError:
                        pass
                    finally:
                        conn.close()

                threading.Thread(target=handle, args=(c,), daemon=True).start()

        threading.Thread(target=loop, daemon=True).start()
        return srv, srv.getsockname()[1]

    def test_agy_env_injects_proxy_only_when_running(self):
        env_off = self.b.agy_env()
        self.assertNotIn('HTTPS_PROXY', env_off)
        url = self.b.net_proxy.start()
        self.assertTrue(url.startswith('http://127.0.0.1:'))
        env_on = self.b.agy_env()
        for k in ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy'):
            self.assertEqual(env_on.get(k), url)
        self.assertIn('127.0.0.1', env_on['NO_PROXY'])
        self.assertIn('localhost', env_on['NO_PROXY'])
        self.assertIn('::1', env_on['NO_PROXY'])
        self.b.net_proxy.stop()
        self.assertEqual(self.b.net_proxy.url(), '')

    def test_connect_tunnel_pipelined_and_wait_stall_logging(self):
        # Use short stall thresholds so the test runs quickly.
        proxy = self.b.NetProxy(dial_timeout=5.0, idle_close=10.0, exchange_quiet=0.2, wait_first=0.25, wait_step=0.6)
        self.addCleanup(proxy.stop)
        proxy.start()

        srv, echo_port = self._start_echo_server(delay=0.45)
        self.addCleanup(srv.close)

        c = socket.create_connection(('127.0.0.1', proxy.port), timeout=5)
        self.addCleanup(c.close)
        # Send CONNECT plus pipelined payload bytes in one write.
        payload = b'X' * 1200
        c.sendall(('CONNECT 127.0.0.1:%d HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n' % (echo_port, echo_port)).encode('ascii') + payload)

        resp = b''
        while b'\r\n\r\n' not in resp:
            resp += c.recv(4096)
        head, rest = resp.split(b'\r\n\r\n', 1)
        self.assertIn(b'200 Connection Established', head)

        got = rest
        expected = b'ECHO:' + payload
        while len(got) < len(expected):
            got += c.recv(65536)
        self.assertEqual(got, expected)

        # Wait for exchange quiet flush, then close cleanly.
        time.sleep(0.35)
        c.close()
        time.sleep(0.15)

        joined = '\n'.join(self.logs)
        self.assertIn('waiting ', joined)
        self.assertIn('no reply yet', joined)
        self.assertIn('first reply after ', joined)
        self.assertIn('tx=1.2KB(send ', joined)
        self.assertIn('ttfb=', joined)

    def test_plain_http_forwarding(self):
        proxy = self.b.NetProxy(dial_timeout=5.0, idle_close=5.0, exchange_quiet=0.2)
        self.addCleanup(proxy.stop)
        proxy_url = proxy.start()

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(('127.0.0.1', 0))
        srv.listen(4)
        self.addCleanup(srv.close)
        http_port = srv.getsockname()[1]
        seen = []

        def http_once():
            conn, _ = srv.accept()
            try:
                req = b''
                while b'\r\n\r\n' not in req:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    req += chunk
                seen.append(req.decode('latin1', 'replace'))
                body = b'hello-from-upstream'
                conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s' % (len(body), body))
            finally:
                conn.close()

        t = threading.Thread(target=http_once, daemon=True)
        t.start()

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({'http': proxy_url}))
        with opener.open('http://127.0.0.1:%d/ping?x=1' % http_port, timeout=5) as r:
            self.assertEqual(r.status, 200)
            self.assertEqual(r.read(), b'hello-from-upstream')
        t.join(2)
        self.assertTrue(seen and seen[0].startswith('GET /ping?x=1 HTTP/1.1'))

    def test_dial_failure_returns_502_and_logs(self):
        proxy = self.b.NetProxy(dial_timeout=2.0)
        self.addCleanup(proxy.stop)
        proxy.start()

        # Bind then close a port to get a reliably refused loopback port.
        tmp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tmp_sock.bind(('127.0.0.1', 0))
        dead_port = tmp_sock.getsockname()[1]
        tmp_sock.close()

        c = socket.create_connection(('127.0.0.1', proxy.port), timeout=5)
        self.addCleanup(c.close)
        c.sendall(('CONNECT 127.0.0.1:%d HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n' % (dead_port, dead_port)).encode('ascii'))
        resp = c.recv(4096)
        self.assertIn(b'502 Bad Gateway', resp)
        time.sleep(0.1)
        self.assertTrue(any('dial failed' in line for line in self.logs), self.logs)

    def test_upstream_proxy_chaining_and_watched_hosts(self):
        self.assertTrue(self.b._is_watched_host('daily-cloudcode-pa.googleapis.com'))
        self.assertTrue(self.b._is_watched_host('aicode.googleapis.com'))
        self.assertFalse(self.b._is_watched_host('example.com'))
        self.assertTrue(self.b._no_proxy_match('foo.local', '.local,127.0.0.1'))
        self.assertFalse(self.b._no_proxy_match('googleapis.com', '127.0.0.1,localhost'))

        # Start an upstream proxy, then chain a second proxy to it via HTTPS_PROXY.
        up = self.b.NetProxy(dial_timeout=5.0, idle_close=5.0)
        self.addCleanup(up.stop)
        up_url = up.start()

        srv, echo_port = self._start_echo_server(delay=0.0)
        self.addCleanup(srv.close)

        old = os.environ.get('HTTPS_PROXY')
        os.environ['HTTPS_PROXY'] = up_url
        try:
            down = self.b.NetProxy(dial_timeout=5.0, idle_close=5.0)
            self.addCleanup(down.stop)
            down.start()
        finally:
            if old is None:
                os.environ.pop('HTTPS_PROXY', None)
            else:
                os.environ['HTTPS_PROXY'] = old

        c = socket.create_connection(('127.0.0.1', down.port), timeout=5)
        self.addCleanup(c.close)
        c.sendall(('CONNECT 127.0.0.1:%d HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n' % (echo_port, echo_port)).encode('ascii'))
        resp = b''
        while b'\r\n\r\n' not in resp:
            resp += c.recv(4096)
        self.assertIn(b'200 Connection Established', resp)
        c.sendall(b'chained-ok')
        self.assertEqual(c.recv(1024), b'ECHO:chained-ok')


if __name__ == '__main__':
    unittest.main()
