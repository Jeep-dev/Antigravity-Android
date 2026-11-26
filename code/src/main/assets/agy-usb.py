#!/usr/bin/env python3
"""agy-usb: access Android USB OTG devices from the Antigravity userland.

Talks to UsbBridge in the Android app over an abstract Unix domain socket.

Usage:
  agy-usb -l [--json]                   List connected USB OTG devices
  agy-usb -r <device>                   Request Android USB permission for a device
  agy-usb -e <cmd> [args...] <device>   Open device and pass its FD as the last arg (like termux-usb)
  agy-usb -E <cmd> [args...] <device>   Open device and pass its FD in USB_FD env var
"""
import array
import json
import os
import socket
import subprocess
import sys

DEFAULT_SOCKET = os.environ.get('AGY_USB_SOCKET', 'com.jeep.antigravity.usb')


def connect(sock_name=None):
    name = sock_name or DEFAULT_SOCKET
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    # Leading NUL byte selects Linux abstract Unix socket namespace (or filesystem path if starts with /).
    addr = name if name.startswith('/') else '\0' + name
    s.connect(addr)
    return s


def request_json(payload, sock_name=None):
    with connect(sock_name) as s:
        s.sendall((json.dumps(payload) + '\n').encode('utf-8'))
        data = b''
        while not data.endswith(b'\n'):
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
        if not data:
            raise RuntimeError('empty response from UsbBridge')
        return json.loads(data.decode('utf-8'))


def open_device(device='', request_permission=True, sock_name=None):
    """Returns (bridge_socket, usb_fd, device_info). Caller must close both usb_fd and bridge_socket."""
    s = connect(sock_name)
    try:
        req = {'cmd': 'open', 'device': device, 'request': request_permission}
        s.sendall((json.dumps(req) + '\n').encode('utf-8'))
        fds = array.array('i')
        msg, ancdata, _flags, _addr = s.recvmsg(65536, socket.CMSG_SPACE(fds.itemsize))
        for cmsg_level, cmsg_type, cmsg_data in ancdata:
            if cmsg_level == socket.SOL_SOCKET and cmsg_type == socket.SCM_RIGHTS:
                fds.frombytes(cmsg_data[:len(cmsg_data) - (len(cmsg_data) % fds.itemsize)])
        while msg and not msg.endswith(b'\n'):
            chunk = s.recv(4096)
            if not chunk:
                break
            msg += chunk
        if not msg:
            raise RuntimeError('empty response from UsbBridge')
        info = json.loads(msg.decode('utf-8'))
        if not info.get('ok'):
            raise RuntimeError(info.get('error', 'failed to open USB device'))
        if not fds:
            raise RuntimeError('UsbBridge did not attach a USB file descriptor')
        return s, fds[0], info
    except Exception:
        s.close()
        raise


def _tags(dev):
    tags = []
    for iface in dev.get('interfaces', []):
        cls, sub, proto = iface.get('class'), iface.get('subclass'), iface.get('protocol')
        if (cls, sub, proto) == (0xFF, 0x42, 0x03):
            tags.append('fastboot')
        elif (cls, sub, proto) == (0xFF, 0x42, 0x01):
            tags.append('adb')
    return tags


def cmd_list(as_json=False):
    resp = request_json({'cmd': 'list'})
    if not resp.get('ok'):
        sys.exit('agy-usb: %s' % resp.get('error', 'list failed'))
    devices = resp.get('devices', [])
    if as_json:
        print(json.dumps(devices, indent=2, ensure_ascii=False))
        return 0
    if not devices:
        print('No USB OTG devices connected.')
        return 0
    for d in devices:
        vid_pid = '%04x:%04x' % (d.get('vid', 0), d.get('pid', 0))
        label = ' '.join(x for x in (d.get('manufacturer', ''), d.get('product', '')) if x).strip() or 'USB Device'
        perm = 'granted' if d.get('has_permission') else 'needs-permission'
        tags = _tags(d)
        extra = ' [%s]' % ','.join(tags) if tags else ''
        serial = ' serial=%s' % d['serial'] if d.get('serial') else ''
        print('%s  %s  %s (%s)%s%s' % (d.get('name', ''), vid_pid, label, perm, extra, serial))
    return 0


def cmd_request(device):
    resp = request_json({'cmd': 'request', 'device': device})
    if not resp.get('ok'):
        sys.exit('agy-usb: %s' % resp.get('error', 'request failed'))
    granted = bool(resp.get('granted'))
    print('Permission %s for %s' % ('granted' if granted else 'denied', resp.get('device', device)))
    return 0 if granted else 1


def cmd_exec(args, via_env=False):
    if len(args) < 2:
        sys.exit('usage: agy-usb %s <command> [args...] <device>' % ('-E' if via_env else '-e'))
    device = args[-1]
    cmd = list(args[:-1])
    bridge_sock, fd, _info = open_device(device, request_permission=True)
    try:
        os.set_inheritable(fd, True)
        env = os.environ.copy()
        if via_env:
            env['USB_FD'] = str(fd)
        else:
            cmd.append(str(fd))
        proc = subprocess.run(cmd, env=env, pass_fds=(fd,))
        return proc.returncode
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        bridge_sock.close()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ('-h', '--help', 'help'):
        print(__doc__.strip())
        return 0
    flag = argv[0]
    if flag in ('-l', '--list', 'list'):
        return cmd_list(as_json='--json' in argv[1:])
    if flag in ('-r', '--request', 'request'):
        if len(argv) < 2:
            sys.exit('usage: agy-usb -r <device>')
        return cmd_request(argv[1])
    if flag in ('-e', '--exec'):
        return cmd_exec(argv[1:], via_env=False)
    if flag == '-E':
        return cmd_exec(argv[1:], via_env=True)
    sys.exit('agy-usb: unknown option %s (use --help)' % flag)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except FileNotFoundError:
        sys.exit('agy-usb: UsbBridge is not running (update/restart the Antigravity app)')
    except ConnectionRefusedError:
        sys.exit('agy-usb: could not connect to UsbBridge')
    except RuntimeError as e:
        sys.exit('agy-usb: %s' % e)
