#!/bin/sh
# Builds the Linux userland the app runs agy in: Alpine with Python, git and a few packages, packed as
# one tar.xz (unpacked by the app on first launch; agy itself is downloaded then, with agy-update).
# usage: build-rootfs.sh <aarch64|x86_64> <out.tar.xz>   (as root: apk sets file owners)
set -eu
ARCH=$1
OUT=$(realpath "$2")
ALPINE=3.24
ALPINE_RELEASE=3.24.2
PACKAGES="bash python3 git ripgrep fd libgcc libstdc++ curl less ca-certificates tzdata"
MIRROR=https://dl-cdn.alpinelinux.org/alpine
# What the app's apk installs from later (fast from China, works anywhere).
APP_MIRROR=https://mirrors.aliyun.com/alpine

case $ARCH in
    aarch64|x86_64) ;;
    *) echo "unknown arch $ARCH" >&2; exit 1 ;;
esac

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
root=$work/root
mkdir -p "$root"

curl -fsSL "$MIRROR/v$ALPINE/releases/$ARCH/alpine-minirootfs-$ALPINE_RELEASE-$ARCH.tar.gz" | tar -xz -C "$root"

# apk for this machine, installing packages for $ARCH without running them.
host=$(uname -m)
curl -fsSL "$MIRROR/v$ALPINE/main/$host/APKINDEX.tar.gz" | tar -xzO APKINDEX > "$work/index"
apkver=$(awk '/^P:apk-tools-static$/ {f=1} f && /^V:/ {print substr($0, 3); exit}' "$work/index")
curl -fsSL "$MIRROR/v$ALPINE/main/$host/apk-tools-static-$apkver.apk" | tar -xz -C "$work" sbin/apk.static 2>/dev/null
"$work/sbin/apk.static" --arch "$ARCH" --root "$root" --no-scripts --no-cache \
    -X "$MIRROR/v$ALPINE/main" -X "$MIRROR/v$ALPINE/community" -U add $PACKAGES

printf '%s/v%s/main\n%s/v%s/community\n' "$APP_MIRROR" "$ALPINE" "$APP_MIRROR" "$ALPINE" > "$root/etc/apk/repositories"
echo /bin/bash >> "$root/etc/shells"
sed -i 's|^root:x:0:0:root:/root:/bin/sh$|root:x:0:0:root:/root:/bin/bash|' "$root/etc/passwd"

# agy is not in it: the app downloads Google's own build with agy-update on first launch (agy then
# updates itself, as does `agy update`).
cat > "$root/usr/local/bin/agy-update" <<'UPDATE'
#!/usr/bin/env python3
"""agy-update: installs the latest Antigravity CLI (agy) as /usr/local/bin/agy, as Google's installer
(https://antigravity.google/cli/install.sh) does: its release manifest for this machine, the build it
names, checked against the SHA-512 the manifest gives. Says how far it is ("progress <percent> <MB>")."""
import hashlib, json, os, platform, shutil, sys, tarfile, urllib.request
BASE = 'https://antigravity-cli-auto-updater-974169037036.us-central1.run.app'
TARGET = '/usr/local/bin/agy'
arch = {'aarch64': 'arm64', 'arm64': 'arm64', 'x86_64': 'amd64', 'amd64': 'amd64'}.get(platform.machine())
if not arch:
    sys.exit('agy-update: no Antigravity CLI for %s' % platform.machine())
def get(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'agy-update'}), timeout=60)
try:
    with get('%s/manifests/linux_%s_musl.json' % (BASE, arch)) as r:
        manifest = json.load(r)
    url, sha512 = manifest['url'], manifest['sha512']
except Exception as e:
    sys.exit('agy-update: the release manifest could not be read: %s' % e)
print('version %s' % manifest.get('version', '?'), flush=True)
# Streamed to disk beside the target (never held whole: the binary is over 200 MB unpacked).
download, part, digest = TARGET + '.download', TARGET + '.part', hashlib.sha512()
try:
    with get(url) as r, open(download, 'wb') as f:
        size, got, last = int(r.headers.get('Content-Length') or 0), 0, -1
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
            digest.update(chunk)
            got += len(chunk)
            if size and got * 100 // size != last:
                last = got * 100 // size
                print('progress %d %d' % (last, size >> 20), flush=True)
except Exception as e:
    os.path.exists(download) and os.remove(download)
    sys.exit('agy-update: download failed: %s' % e)
print('verify', flush=True)
if digest.hexdigest() != sha512.strip().lower():
    os.remove(download)
    sys.exit('agy-update: the download does not match its checksum; nothing was installed')
print('unpack', flush=True)
try:
    if url.split('?')[0].endswith('.tar.gz'):
        with tarfile.open(download, mode='r:gz') as tar, open(part, 'wb') as f:
            shutil.copyfileobj(tar.extractfile('antigravity'), f, 1 << 20)
        os.remove(download)
    else:
        os.replace(download, part)
    os.chmod(part, 0o755)
    os.replace(part, TARGET)
except Exception as e:
    for leftover in (download, part):
        os.path.exists(leftover) and os.remove(leftover)
    sys.exit('agy-update: could not install: %s' % e)
print('installed agy %s' % manifest.get('version', ''), flush=True)
UPDATE
chmod 755 "$root/usr/local/bin/agy-update"

rm -rf "$root/var/cache/apk/"*
tar --numeric-owner --hard-dereference -C "$root" -cf - . | xz -T0 -6 > "$OUT"
ls -l "$OUT"
