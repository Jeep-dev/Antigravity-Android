#!/usr/bin/env python3
"""agy-fastboot: official Android Fastboot protocol over Android USB OTG (via agy-usb / UsbBridge).

Standard library only. Speaks Linux usbfs ioctls directly on the USB file descriptor granted by
Android's UsbManager.

Usage:
  agy-fastboot devices
  agy-fastboot [-s <device>] getvar <name|all>
  agy-fastboot [-s <device>] reboot [bootloader|recovery|fastboot]
  agy-fastboot [-s <device>] flashing <unlock|lock|get_unlock_ability|unlock_critical|lock_critical>
  agy-fastboot [-s <device>] oem <args...>
  agy-fastboot [-s <device>] set_active <a|b>
  agy-fastboot [-s <device>] erase <partition>
  agy-fastboot [-s <device>] boot <image.img>
  agy-fastboot [-s <device>] flash <partition> <image.img>
"""
import ctypes
import fcntl
import importlib.util
import os
import struct
import sys

# Linux usbdevfs ioctl numbers (works on both 64-bit aarch64/x86_64 and 32-bit).
_PTR_SIZE = struct.calcsize('P')
_BULK_FMT = 'III4xP' if _PTR_SIZE == 8 else 'IIIP'
_BULK_SIZE = struct.calcsize(_BULK_FMT)
USBDEVFS_BULK = (3 << 30) | (_BULK_SIZE << 16) | (0x55 << 8) | 2
USBDEVFS_CLAIMINTERFACE = (2 << 30) | (4 << 16) | (0x55 << 8) | 15
USBDEVFS_RELEASEINTERFACE = (2 << 30) | (4 << 16) | (0x55 << 8) | 16

MAX_BULK_CHUNK = 16384  # Universal Linux usbfs single-transfer limit.
FASTBOOT_CLASS = (0xFF, 0x42, 0x03)


def _load_agy_usb():
    for candidate in (
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'agy-usb.py'),
        '/opt/agyapp/agy-usb.py',
        '/usr/local/bin/agy-usb',
    ):
        if os.path.isfile(candidate):
            spec = importlib.util.spec_from_file_location('agy_usb', candidate)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    raise RuntimeError('agy-usb helper not found')


def find_fastboot_interface(info):
    for iface in info.get('interfaces', []):
        if (iface.get('class'), iface.get('subclass'), iface.get('protocol')) == FASTBOOT_CLASS:
            ep_in = ep_out = None
            for ep in iface.get('endpoints', []):
                if ep.get('type') != 'bulk':
                    continue
                if ep.get('direction') == 'in' and ep_in is None:
                    ep_in = ep['address']
                elif ep.get('direction') == 'out' and ep_out is None:
                    ep_out = ep['address']
            if ep_in is not None and ep_out is not None:
                return iface['id'], ep_in, ep_out
    return None


class UsbfsTransport:
    def __init__(self, fd, iface_id, ep_in, ep_out):
        self.fd = fd
        self.iface_id = iface_id
        self.ep_in = ep_in
        self.ep_out = ep_out
        self.claimed = False

    def __enter__(self):
        arg = struct.pack('I', self.iface_id)
        fcntl.ioctl(self.fd, USBDEVFS_CLAIMINTERFACE, arg)
        self.claimed = True
        return self

    def __exit__(self, *_exc):
        if self.claimed:
            try:
                fcntl.ioctl(self.fd, USBDEVFS_RELEASEINTERFACE, struct.pack('I', self.iface_id))
            except OSError:
                pass
            self.claimed = False

    def write(self, data, timeout_ms=10000):
        view = memoryview(data)
        offset = 0
        total = len(view)
        while offset < total:
            chunk = bytes(view[offset:offset + MAX_BULK_CHUNK])
            buf = ctypes.create_string_buffer(chunk, len(chunk))
            req = bytearray(struct.pack(_BULK_FMT, self.ep_out, len(chunk), timeout_ms, ctypes.addressof(buf)))
            n = fcntl.ioctl(self.fd, USBDEVFS_BULK, req)
            if n <= 0:
                raise RuntimeError('USB bulk write failed (%d)' % n)
            offset += n

    def read(self, max_len=4096, timeout_ms=30000):
        buf = ctypes.create_string_buffer(max_len)
        req = bytearray(struct.pack(_BULK_FMT, self.ep_in, max_len, timeout_ms, ctypes.addressof(buf)))
        n = fcntl.ioctl(self.fd, USBDEVFS_BULK, req)
        if n < 0:
            raise RuntimeError('USB bulk read failed (%d)' % n)
        return buf.raw[:n]


class FastbootClient:
    def __init__(self, transport):
        self.transport = transport

    def command(self, cmd, timeout_ms=30000, on_info=None):
        self.transport.write(cmd.encode('utf-8'), timeout_ms=timeout_ms)
        return self.read_response(timeout_ms=timeout_ms, on_info=on_info)

    def read_response(self, timeout_ms=30000, on_info=None):
        info_lines = []
        while True:
            raw = self.transport.read(4096, timeout_ms=timeout_ms)
            if len(raw) < 4:
                raise RuntimeError('short fastboot response: %r' % (raw,))
            status = raw[:4].decode('ascii', errors='replace')
            payload = raw[4:].decode('utf-8', errors='replace')
            if status == 'INFO':
                info_lines.append(payload)
                if on_info:
                    on_info(payload)
            elif status == 'TEXT':
                if on_info:
                    on_info(payload)
            elif status == 'OKAY':
                return status, payload, info_lines
            elif status == 'DATA':
                return status, payload, info_lines
            elif status == 'FAIL':
                raise RuntimeError('FAILED (remote: %s)' % payload)
            else:
                raise RuntimeError('unexpected fastboot status %r: %s' % (status, payload))

    def download_file(self, path, progress=True):
        size = os.path.getsize(path)
        if size == 0:
            raise RuntimeError('image file is empty: %s' % path)
        status, payload, _ = self.command('download:%08x' % size, timeout_ms=15000)
        if status != 'DATA':
            raise RuntimeError('expected DATA response, got %s %s' % (status, payload))
        sent = 0
        last_pct = -1
        with open(path, 'rb') as f:
            while True:
                chunk = f.read(MAX_BULK_CHUNK * 16)  # 256 KB read chunks, written in 16 KB usbfs bursts
                if not chunk:
                    break
                self.transport.write(chunk, timeout_ms=30000)
                sent += len(chunk)
                if progress:
                    pct = sent * 100 // size
                    if pct != last_pct and (pct % 10 == 0 or pct == 100):
                        last_pct = pct
                        print('Sending %s (%d%%)...' % (os.path.basename(path), pct), file=sys.stderr, flush=True)
        status, payload, _ = self.read_response(timeout_ms=60000)
        if status != 'OKAY':
            raise RuntimeError('download failed: %s %s' % (status, payload))
        return size


def _select_device(usb_mod, requested=None):
    resp = usb_mod.request_json({'cmd': 'list'})
    if not resp.get('ok'):
        raise RuntimeError(resp.get('error', 'failed to list USB devices'))
    candidates = []
    for d in resp.get('devices', []):
        if find_fastboot_interface(d) is not None:
            candidates.append(d)
    if requested:
        for d in candidates:
            if requested in (d.get('name'), d.get('serial')):
                return d['name']
        return requested
    if not candidates:
        raise RuntimeError('no fastboot device found over USB OTG')
    if len(candidates) > 1:
        names = ', '.join(d.get('name', '') for d in candidates)
        raise RuntimeError('multiple fastboot devices found (%s); specify -s <device>' % names)
    return candidates[0]['name']


def _open_fastboot(usb_mod, requested=None):
    dev_name = _select_device(usb_mod, requested)
    bridge_sock, fd, info = usb_mod.open_device(dev_name, request_permission=True)
    ep = find_fastboot_interface(info)
    if ep is None:
        os.close(fd)
        bridge_sock.close()
        raise RuntimeError('device %s has no fastboot interface (0xff/0x42/0x03)' % dev_name)
    iface_id, ep_in, ep_out = ep
    return bridge_sock, fd, UsbfsTransport(fd, iface_id, ep_in, ep_out)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ('-h', '--help', 'help'):
        print(__doc__.strip())
        return 0

    selected = None
    while len(argv) >= 2 and argv[0] == '-s':
        selected = argv[1]
        argv = argv[2:]
    if not argv:
        print(__doc__.strip())
        return 0

    usb_mod = _load_agy_usb()
    cmd = argv[0]
    rest = argv[1:]

    if cmd == 'devices':
        resp = usb_mod.request_json({'cmd': 'list'})
        if not resp.get('ok'):
            raise RuntimeError(resp.get('error', 'list failed'))
        for d in resp.get('devices', []):
            if find_fastboot_interface(d) is not None:
                ident = d.get('serial') or d.get('name', '')
                perm = '' if d.get('has_permission') else '\t(needs-permission)'
                print('%s\tfastboot\t%s%s' % (ident, d.get('name', ''), perm))
        return 0

    bridge_sock, fd, transport = _open_fastboot(usb_mod, selected)
    try:
        with transport:
            fb = FastbootClient(transport)
            info_printer = lambda line: print('(bootloader) %s' % line)

            if cmd == 'getvar':
                if not rest:
                    raise RuntimeError('usage: agy-fastboot getvar <name|all>')
                var = rest[0]
                _st, payload, _ = fb.command('getvar:' + var, on_info=info_printer)
                if var != 'all':
                    print('%s: %s' % (var, payload))
                else:
                    print('all: %s' % payload)
            elif cmd == 'reboot':
                target = rest[0] if rest else ''
                if not target:
                    fb.command('reboot', on_info=info_printer)
                elif target == 'bootloader':
                    fb.command('reboot-bootloader', on_info=info_printer)
                elif target in ('recovery', 'fastboot'):
                    fb.command('reboot-' + target, on_info=info_printer)
                else:
                    raise RuntimeError('unknown reboot target: %s' % target)
                print('Rebooting...')
            elif cmd == 'flashing':
                if not rest:
                    raise RuntimeError('usage: agy-fastboot flashing <unlock|lock|get_unlock_ability|...>')
                _st, payload, _ = fb.command('flashing ' + ' '.join(rest), timeout_ms=60000, on_info=info_printer)
                print('OKAY %s' % payload)
            elif cmd == 'oem':
                if not rest:
                    raise RuntimeError('usage: agy-fastboot oem <args...>')
                _st, payload, _ = fb.command('oem ' + ' '.join(rest), timeout_ms=60000, on_info=info_printer)
                print('OKAY %s' % payload)
            elif cmd == 'set_active':
                if not rest:
                    raise RuntimeError('usage: agy-fastboot set_active <a|b>')
                _st, payload, _ = fb.command('set_active:' + rest[0], on_info=info_printer)
                print('OKAY %s' % payload)
            elif cmd == 'erase':
                if not rest:
                    raise RuntimeError('usage: agy-fastboot erase <partition>')
                _st, payload, _ = fb.command('erase:' + rest[0], timeout_ms=60000, on_info=info_printer)
                print('Erasing %r OKAY %s' % (rest[0], payload))
            elif cmd == 'boot':
                if not rest or not os.path.isfile(rest[0]):
                    raise RuntimeError('usage: agy-fastboot boot <image.img>')
                fb.download_file(rest[0])
                _st, payload, _ = fb.command('boot', timeout_ms=60000, on_info=info_printer)
                print('Booting OKAY %s' % payload)
            elif cmd == 'flash':
                if len(rest) < 2 or not os.path.isfile(rest[1]):
                    raise RuntimeError('usage: agy-fastboot flash <partition> <image.img>')
                part, img = rest[0], rest[1]
                # Check max-download-size if supported.
                try:
                    _s, max_dl, _ = fb.command('getvar:max-download-size')
                    limit = int(max_dl.strip(), 0)
                    img_size = os.path.getsize(img)
                    if limit > 0 and img_size > limit:
                        raise RuntimeError(
                            'image (%d bytes) exceeds device max-download-size (%d bytes); sparse images > max-download-size are not split automatically'
                            % (img_size, limit)
                        )
                except RuntimeError as e:
                    if 'exceeds device max-download-size' in str(e):
                        raise
                except ValueError:
                    pass
                fb.download_file(img)
                _st, payload, _ = fb.command('flash:' + part, timeout_ms=120000, on_info=info_printer)
                print('Writing %r OKAY %s' % (part, payload))
            else:
                raise RuntimeError('unknown command %r (use --help)' % cmd)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        bridge_sock.close()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except FileNotFoundError:
        sys.exit('agy-fastboot: UsbBridge is not running (update/restart the Antigravity app)')
    except ConnectionRefusedError:
        sys.exit('agy-fastboot: could not connect to UsbBridge')
    except RuntimeError as e:
        sys.exit('agy-fastboot: %s' % e)
