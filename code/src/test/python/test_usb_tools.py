"""Tests for agy-usb.py and agy-fastboot.py against an in-process mock UsbBridge."""
import array
import importlib.util
import json
import os
import socket
import tempfile
import threading
import unittest

ASSETS = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'main', 'assets'))


def _load(name):
    path = os.path.join(ASSETS, name)
    spec = importlib.util.spec_from_file_location(name.replace('.', '_').replace('-', '_'), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


agy_usb = _load('agy-usb.py')
agy_fastboot = _load('agy-fastboot.py')


FAKE_DEVICE = {
    'name': '/dev/bus/usb/001/002',
    'vid': 0x18D1,
    'pid': 0x4EE0,
    'manufacturer': 'Google',
    'product': 'Android Fastboot',
    'serial': 'TEST1234',
    'has_permission': True,
    'interfaces': [
        {
            'id': 0,
            'class': 0xFF,
            'subclass': 0x42,
            'protocol': 0x03,
            'endpoints': [
                {'address': 0x81, 'direction': 'in', 'type': 'bulk', 'max_packet': 512},
                {'address': 0x01, 'direction': 'out', 'type': 'bulk', 'max_packet': 512},
            ],
        }
    ],
}


class MockUsbBridge:
    def __init__(self, sock_path):
        self.sock_path = sock_path
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(sock_path)
        self.srv.listen(4)
        self.stopped = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self.stopped:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            data = b''
            while not data.endswith(b'\n'):
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
            req = json.loads(data.decode('utf-8'))
            cmd = req.get('cmd')
            if cmd == 'list':
                conn.sendall((json.dumps({'ok': True, 'devices': [FAKE_DEVICE]}) + '\n').encode('utf-8'))
            elif cmd == 'request':
                conn.sendall((json.dumps({'ok': True, 'device': req.get('device'), 'granted': True}) + '\n').encode('utf-8'))
            elif cmd == 'open':
                r_fd, w_fd = os.pipe()
                os.write(w_fd, b'usb-fd-ok')
                os.close(w_fd)
                try:
                    resp = dict(FAKE_DEVICE, ok=True)
                    conn.sendmsg(
                        [(json.dumps(resp) + '\n').encode('utf-8')],
                        [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [r_fd]))],
                    )
                finally:
                    os.close(r_fd)
                while conn.recv(1):
                    pass
        finally:
            conn.close()

    def close(self):
        self.stopped = True
        self.srv.close()


class FakeTransport:
    def __init__(self, replies):
        self.replies = list(replies)
        self.writes = []

    def write(self, data, timeout_ms=10000):
        self.writes.append(data)

    def read(self, max_len=4096, timeout_ms=30000):
        return self.replies.pop(0)


class UsbToolsTest(unittest.TestCase):
    def test_usb_bridge_list_request_and_fd_passing(self):
        with tempfile.TemporaryDirectory() as tmp:
            sock = os.path.join(tmp, 'usb.sock')
            bridge = MockUsbBridge(sock)
            try:
                resp = agy_usb.request_json({'cmd': 'list'}, sock_name=sock)
                self.assertTrue(resp['ok'])
                self.assertEqual(resp['devices'][0]['serial'], 'TEST1234')

                req = agy_usb.request_json({'cmd': 'request', 'device': '/dev/bus/usb/001/002'}, sock_name=sock)
                self.assertTrue(req['granted'])

                s, fd, info = agy_usb.open_device('/dev/bus/usb/001/002', sock_name=sock)
                try:
                    self.assertEqual(info['vid'], 0x18D1)
                    self.assertEqual(os.read(fd, 32), b'usb-fd-ok')
                finally:
                    os.close(fd)
                    s.close()
            finally:
                bridge.close()

    def test_fastboot_interface_detection(self):
        res = agy_fastboot.find_fastboot_interface(FAKE_DEVICE)
        self.assertEqual(res, (0, 0x81, 0x01))

    def test_fastboot_protocol_getvar_and_download(self):
        tr = FakeTransport([b'INFOhello', b'OKAYvermeer'])
        fb = agy_fastboot.FastbootClient(tr)
        infos = []
        status, payload, info_lines = fb.command('getvar:product', on_info=infos.append)
        self.assertEqual(status, 'OKAY')
        self.assertEqual(payload, 'vermeer')
        self.assertEqual(infos, ['hello'])
        self.assertEqual(info_lines, ['hello'])
        self.assertEqual(tr.writes, [b'getvar:product'])

        with tempfile.NamedTemporaryFile(delete=False) as img:
            img.write(b'ANDROID!bootdata')
            img_path = img.name
        try:
            tr2 = FakeTransport([b'DATA00000010', b'OKAY', b'OKAYdone'])
            fb2 = agy_fastboot.FastbootClient(tr2)
            size = fb2.download_file(img_path, progress=False)
            self.assertEqual(size, 16)
            self.assertEqual(tr2.writes[0], b'download:00000010')
            self.assertEqual(tr2.writes[1], b'ANDROID!bootdata')
            st, pl, _ = fb2.command('boot')
            self.assertEqual((st, pl), ('OKAY', 'done'))
        finally:
            os.remove(img_path)

    def test_fastboot_fail_raises(self):
        tr = FakeTransport([b'FAILnot allowed'])
        fb = agy_fastboot.FastbootClient(tr)
        with self.assertRaises(RuntimeError) as ctx:
            fb.command('flashing unlock')
        self.assertIn('not allowed', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
