package com.jeep.antigravity;

import android.content.Context;
import android.content.res.AssetManager;
import android.net.ConnectivityManager;
import android.net.LinkProperties;
import android.net.Network;
import android.os.Environment;
import android.system.ErrnoException;
import android.system.Os;
import android.system.OsConstants;

import org.tukaani.xz.XZInputStream;

import java.io.BufferedInputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.FilterInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
import java.util.function.Consumer;

/**
 * The Linux userland agy runs in, inside this app: Alpine (with Python, git and the tools agy's
 * commands use) run directly in the phone's Linux kernel via root chroot (no proot / ptrace);
 * agy itself is downloaded into it by agy-update. Built by code/runtime/*.sh in CI; the userland
 * (rootfs.tar.xz) is downloaded from where CI published it (assets/runtime/rootfs.url), once per
 * userland, unless the APK carries it (a build of its own).
 */
final class Userland {
    static final String HOME = "/root";
    private static final String CHROOT_EXEC = "chroot-exec.sh";
    private static final String HOST_PATH = "/system/bin:/system/xbin:/vendor/bin:/sbin:/debug_ramdisk:/data/adb/ksu/bin:/data/adb/ap/bin";

    final File base, rootfs, tmp, log, chrootExec;
    private final Context context;

    Userland(Context context) {
        this.context = context.getApplicationContext();
        base = this.context.getFilesDir();
        rootfs = new File(base, "rootfs");
        tmp = new File(base, "tmp");
        log = new File(base, "runtime.log");
        chrootExec = new File(base, CHROOT_EXEC);
    }

    /** The id of the userland in this APK; null when the APK carries none (a local build). */
    String bundled() {
        try {
            return read(context.getAssets().open("runtime/id")).trim();
        } catch (IOException e) {
            return null;
        }
    }

    private File marker() { return new File(rootfs, ".agyapp-runtime"); }

    /** The userland in this APK is unpacked, and agy is in it. */
    boolean ready() {
        return unpacked() && hasAgy();
    }

    private boolean unpacked() {
        String id = bundled();
        if (id == null) return false;
        try {
            return id.equals(read(new FileInputStream(marker())).trim());
        } catch (IOException e) {
            return false;
        }
    }

    /** /usr/local/bin/agy is there (agy updates itself in place). */
    private boolean hasAgy() {
        return new File(rootfs, "usr/local/bin/agy").isFile();
    }

    /**
     * Installs agy (Google's own build for Linux, checked against the checksum Google publishes
     * with it) with the userland's agy-update, telling how far the download is.
     */
    private void installAgy(Consumer<String> status) throws IOException {
        status.accept("下载 Antigravity CLI");
        ProcessBuilder pb = new ProcessBuilder(command("/usr/local/bin/agy-update"));
        env(pb);
        pb.directory(base);
        pb.redirectErrorStream(true);
        Process p = pb.start();
        StringBuilder line = new StringBuilder(), tail = new StringBuilder();
        try {
            try (java.io.Reader in = new java.io.InputStreamReader(p.getInputStream(), StandardCharsets.UTF_8)) {
                for (int c; (c = in.read()) >= 0; ) {
                    if (c != '\r' && c != '\n') { line.append((char) c); continue; }
                    String l = line.toString();
                    line.setLength(0);
                    if (l.isEmpty()) continue;
                    // "progress 37 86" (percent, size in MB): the download as it goes.
                    if (l.startsWith("progress ")) {
                        String[] f = l.split(" ");
                        if (f.length >= 3) status.accept("下载 Antigravity CLI " + f[1] + "%（共 " + f[2] + " MB）");
                        continue;
                    }
                    if (l.startsWith("verify")) status.accept("校验 Antigravity CLI");
                    else if (l.startsWith("unpack")) status.accept("解压 Antigravity CLI");
                    tail.append(l).append('\n');
                }
            }
            try {
                if (p.waitFor() != 0 || !hasAgy()) {
                    String t = tail.toString().trim();
                    throw new IOException("没能装上 Antigravity CLI" + (t.isEmpty() ? "" : "：" + t.substring(Math.max(0, t.length() - 300))));
                }
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new IOException("被打断了");
            }
        } finally {
            if (p.isAlive()) p.destroyForcibly();
        }
    }

    /**
     * Unpacks proot and the userland over what is there (files in /root and installed packages
     * stay), downloading the userland first when the APK does not carry it. Tells how far it is.
     */
    void install(Consumer<String> status) throws IOException {
        String id = bundled();
        if (id == null) throw new IOException("这个安装包里没有运行环境");
        if (!unpacked()) unpack(id, status);
        if (!hasAgy()) installAgy(status);
    }

    private void unpack(String id, Consumer<String> status) throws IOException {
        AssetManager assets = context.getAssets();
        boolean inApk;
        try (InputStream ignored = assets.open("runtime/rootfs.tar.xz")) {
            inApk = true;
        } catch (IOException e) {
            inApk = false;
        }
        File download = new File(base, "rootfs-" + id + ".tar.xz");
        if (!inApk) fetch(read(assets.open("runtime/rootfs.url")).trim(), read(assets.open("runtime/rootfs.sha256")).trim(), download, status);
        mkdirs(tmp);
        marker().delete();
        long total = inApk ? assetLength(assets, "runtime/rootfs.tar.xz") : download.length();
        long[] done = {0};
        int[] shown = {-1};
        try (InputStream source = inApk ? assets.open("runtime/rootfs.tar.xz", AssetManager.ACCESS_STREAMING) : new FileInputStream(download);
             InputStream raw = new FilterInputStream(source) {
                 @Override public int read(byte[] b, int off, int len) throws IOException {
                     int n = super.read(b, off, len);
                     if (n > 0) {
                         done[0] += n;
                         int p = (int) (done[0] * 100 / Math.max(1, total));
                         if (p != shown[0]) { shown[0] = p; status.accept("解压 " + p + "%"); }
                     }
                     return n;
                 }
             };
             InputStream in = new BufferedInputStream(new XZInputStream(new BufferedInputStream(raw, 1 << 16)), 1 << 16)) {
            untar(in, rootfs);
        }
        download.delete();
        mkdirs(new File(rootfs, "root/work"));
        mkdirs(new File(rootfs, "opt/agyapp"));
        try (OutputStream out = new FileOutputStream(marker())) {
            out.write(id.getBytes(StandardCharsets.UTF_8));
        }
    }

    /**
     * Downloads the userland to the file, going on from what an earlier try left there, and checks
     * it against its SHA-256; a file that does not match is deleted, to start over next time.
     */
    private void fetch(String url, String sha256, File to, Consumer<String> status) throws IOException {
        // Another userland's download, left from before, is of no use now.
        File[] old = base.listFiles((d, n) -> n.startsWith("rootfs-") && n.endsWith(".tar.xz") && !n.equals(to.getName()));
        if (old != null) for (File f : old) f.delete();
        // A connection that drops is taken up again where it stopped, a few times before giving up.
        for (int attempt = 1; ; attempt++) {
            try {
                if (fetchRest(url, to, status)) break;
                throw new IOException("下载中断");
            } catch (IOException e) {
                if (attempt >= 5) throw new IOException("下载运行环境失败：" + e.getMessage(), e);
                status.accept("网络断了，正在重连…");
                try { Thread.sleep(2000L * attempt); } catch (InterruptedException i) { throw new IOException("已取消"); }
            }
        }
        status.accept("校验…");
        if (!sha256.equalsIgnoreCase(sha256(to))) {
            to.delete();
            throw new IOException("下载的运行环境不完整，请重试");
        }
    }

    /** Downloads what the file lacks; true when it is all there. */
    private static boolean fetchRest(String url, File to, Consumer<String> status) throws IOException {
        for (int redirects = 0; ; redirects++) {
            long have = to.length();
            java.net.HttpURLConnection http = (java.net.HttpURLConnection) new java.net.URL(url).openConnection();
            http.setConnectTimeout(20_000);
            http.setReadTimeout(30_000);
            http.setInstanceFollowRedirects(false);
            if (have > 0) http.setRequestProperty("Range", "bytes=" + have + "-");
            try {
                int code = http.getResponseCode();
                if (code / 100 == 3 && redirects < 5) {
                    // GitHub sends release files from another host; followed here, keeping the range.
                    url = new java.net.URL(new java.net.URL(url), http.getHeaderField("Location")).toString();
                    continue;
                }
                if (code == 416) return true; // all of it is here already
                if (code != 200 && code != 206) throw new IOException("HTTP " + code);
                boolean resumed = code == 206;
                long length = http.getContentLengthLong();
                long total = length < 0 ? -1 : (resumed ? have : 0) + length;
                long done = resumed ? have : 0;
                int shown = -1;
                try (InputStream in = http.getInputStream(); OutputStream out = new FileOutputStream(to, resumed)) {
                    byte[] buf = new byte[1 << 16];
                    for (int n; (n = in.read(buf)) > 0; ) {
                        out.write(buf, 0, n);
                        done += n;
                        int p = total > 0 ? (int) (done * 100 / total) : 0;
                        if (p != shown) {
                            shown = p;
                            status.accept(String.format(java.util.Locale.ROOT, "下载 %d%% · %.0f / %.0f MB", p, done / 1048576.0, total / 1048576.0));
                        }
                    }
                }
                return total < 0 || done >= total;
            } finally {
                http.disconnect();
            }
        }
    }

    private static String sha256(File f) throws IOException {
        try (InputStream in = new FileInputStream(f)) {
            java.security.MessageDigest md = java.security.MessageDigest.getInstance("SHA-256");
            byte[] buf = new byte[1 << 16];
            for (int n; (n = in.read(buf)) > 0; ) md.update(buf, 0, n);
            StringBuilder hex = new StringBuilder();
            for (byte b : md.digest()) hex.append(String.format("%02x", b));
            return hex.toString();
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IOException(e);
        }
    }

    // ---- tar ----

    private static void untar(InputStream in, File into) throws IOException {
        String root = into.getCanonicalPath();
        // Folders checked to be inside (a symbolic link made later may lead out: checked again).
        Set<String> inside = new java.util.HashSet<>();
        byte[] header = new byte[512];
        String longName = null, longLink = null;
        while (true) {
            if (!readFully(in, header)) return;
            if (header[0] == 0) return; // end of archive
            String name = field(header, 0, 100);
            String prefix = field(header, 345, 155);
            if (!prefix.isEmpty()) name = prefix + "/" + name;
            int mode = (int) octal(header, 100, 8);
            long size = octal(header, 124, 12);
            char type = (char) header[156];
            String link = field(header, 157, 100);
            if (type == 'L' || type == 'K' || type == 'x') {
                byte[] data = readData(in, size);
                if (type == 'L') longName = cstring(data);
                else if (type == 'K') longLink = cstring(data);
                else {
                    String p = pax(data, "path"), l = pax(data, "linkpath");
                    if (p != null) longName = p;
                    if (l != null) longLink = l;
                }
                continue;
            }
            if (longName != null) { name = longName; longName = null; }
            if (longLink != null) { link = longLink; longLink = null; }
            while (name.startsWith("./")) name = name.substring(2);
            while (name.endsWith("/")) name = name.substring(0, name.length() - 1);
            File target = new File(into, name);
            // Only inside the userland: no "..", and no folder that a symbolic link leads elsewhere.
            if (!relative(name) || !within(target.getParentFile(), root, inside)) { skip(in, (size + 511) / 512 * 512); continue; }
            switch (type) {
                case '5':
                    if (!target.isDirectory() || java.nio.file.Files.isSymbolicLink(target.toPath())) { remove(target); mkdirs(target); }
                    chmod(target, mode & 07777);
                    break;
                case '2':
                    remove(target);
                    mkdirs(target.getParentFile());
                    try {
                        Os.symlink(link, target.getPath());
                    } catch (ErrnoException e) {
                        throw new IOException("symlink " + name + ": " + e.getMessage());
                    }
                    inside.clear();
                    break;
                case '1': {
                    // Hard links become copies (the archive is made without them anyway), of a file inside.
                    String from = link;
                    while (from.startsWith("./")) from = from.substring(2);
                    File source = new File(into, from);
                    if (!relative(from) || !within(source, root, inside)) break;
                    remove(target);
                    mkdirs(target.getParentFile());
                    try (InputStream src = new FileInputStream(source); OutputStream out = new FileOutputStream(target)) {
                        copy(src, out);
                    }
                    chmod(target, mode & 07777);
                    break;
                }
                case '0': case 0: case '7': {
                    remove(target); // never write into a running binary or through an old symlink
                    mkdirs(target.getParentFile());
                    try (OutputStream out = new FileOutputStream(target)) {
                        byte[] buf = new byte[1 << 16];
                        long left = size;
                        while (left > 0) {
                            int n = in.read(buf, 0, (int) Math.min(buf.length, left));
                            if (n < 0) throw new IOException("archive ends early");
                            out.write(buf, 0, n);
                            left -= n;
                        }
                    }
                    skip(in, (512 - size % 512) % 512);
                    chmod(target, mode & 07777);
                    continue;
                }
                default:
                    break; // devices and fifos: not needed
            }
            skip(in, (size + 511) / 512 * 512);
        }
    }

    /** A path in the archive that stays where it is put: not absolute, no ".", "..", or empty segments. */
    private static boolean relative(String name) {
        if (name.isEmpty() || name.startsWith("/")) return false;
        for (String part : name.split("/", -1)) if (part.isEmpty() || part.equals(".") || part.equals("..")) return false;
        return true;
    }

    /** f, with every link on its way followed, is in root; real folders found inside are remembered. */
    private static boolean within(File f, String root, Set<String> inside) throws IOException {
        if (inside.contains(f.getPath())) return true;
        String real = f.getCanonicalPath();
        boolean in = real.equals(root) || real.startsWith(root + "/");
        if (in && f.isDirectory() && !java.nio.file.Files.isSymbolicLink(f.toPath())) inside.add(f.getPath());
        return in;
    }

    private static boolean readFully(InputStream in, byte[] buf) throws IOException {
        int off = 0;
        while (off < buf.length) {
            int n = in.read(buf, off, buf.length - off);
            if (n < 0) { if (off == 0) return false; throw new IOException("archive ends early"); }
            off += n;
        }
        return true;
    }

    private static byte[] readData(InputStream in, long size) throws IOException {
        byte[] data = new byte[(int) size];
        if (size > 0 && !readFully(in, data)) throw new IOException("archive ends early");
        skip(in, (512 - size % 512) % 512);
        return data;
    }

    private static void skip(InputStream in, long n) throws IOException {
        byte[] buf = new byte[8192];
        while (n > 0) {
            int r = in.read(buf, 0, (int) Math.min(buf.length, n));
            if (r < 0) throw new IOException("archive ends early");
            n -= r;
        }
    }

    private static String field(byte[] h, int off, int len) {
        int end = off;
        while (end < off + len && h[end] != 0) end++;
        return new String(h, off, end - off, StandardCharsets.UTF_8);
    }

    private static String cstring(byte[] data) {
        int end = 0;
        while (end < data.length && data[end] != 0) end++;
        return new String(data, 0, end, StandardCharsets.UTF_8);
    }

    private static long octal(byte[] h, int off, int len) {
        long v = 0;
        for (int i = off; i < off + len; i++) {
            byte b = h[i];
            if (b == 0 || b == ' ') { if (v != 0) break; else continue; }
            if (b < '0' || b > '7') break;
            v = v * 8 + (b - '0');
        }
        return v;
    }

    /** A key from pax extended header records ("<len> key=value\n"). */
    private static String pax(byte[] data, String key) {
        int i = 0;
        while (i < data.length) {
            int sp = i;
            while (sp < data.length && data[sp] != ' ') sp++;
            int len;
            try {
                len = Integer.parseInt(new String(data, i, sp - i, StandardCharsets.US_ASCII));
            } catch (NumberFormatException e) {
                return null;
            }
            if (sp >= data.length || len <= sp - i + 1 || i + len > data.length) return null;
            String rec = new String(data, sp + 1, i + len - sp - 2, StandardCharsets.UTF_8);
            if (rec.startsWith(key + "=")) return rec.substring(key.length() + 1);
            i += len;
        }
        return null;
    }

    /** Removes what is there unless it is a real folder: a file, or a symbolic link (to a folder too). */
    private static void remove(File f) {
        try {
            int mode = Os.lstat(f.getPath()).st_mode;
            if (OsConstants.S_ISLNK(mode) || !OsConstants.S_ISDIR(mode)) Os.remove(f.getPath());
        } catch (ErrnoException ignored) {
            // nothing there
        }
    }

    /** Bytes under a file or folder, links not followed. */
    static long size(File f) {
        long[] total = {0};
        try {
            java.nio.file.Files.walkFileTree(f.toPath(), new java.nio.file.SimpleFileVisitor<java.nio.file.Path>() {
                @Override public java.nio.file.FileVisitResult visitFile(java.nio.file.Path p, java.nio.file.attribute.BasicFileAttributes a) {
                    total[0] += a.size();
                    return java.nio.file.FileVisitResult.CONTINUE;
                }

                @Override public java.nio.file.FileVisitResult visitFileFailed(java.nio.file.Path p, IOException e) {
                    return java.nio.file.FileVisitResult.CONTINUE;
                }
            });
        } catch (IOException ignored) {
            // what was counted
        }
        return total[0];
    }

    /** Deletes a file, or a folder and all in it; a link, not what it points to. */
    static void delete(File f) {
        try {
            if (OsConstants.S_ISDIR(Os.lstat(f.getPath()).st_mode)) {
                File[] in = f.listFiles();
                if (in != null) for (File x : in) delete(x);
                Os.remove(f.getPath());
            } else {
                Os.remove(f.getPath());
            }
        } catch (ErrnoException ignored) {
            // gone already, or not ours to delete
        }
    }

    private static void mkdirs(File dir) throws IOException {
        if (dir != null && !dir.isDirectory() && !dir.mkdirs() && !dir.isDirectory()) throw new IOException("mkdir " + dir);
    }

    private static void chmod(File f, int mode) {
        try {
            Os.chmod(f.getPath(), mode);
        } catch (ErrnoException ignored) {
            // keeps the default mode
        }
    }

    // ---- running ----

    /** Quotes a single argument for POSIX sh. */
    static String shellQuote(String s) {
        return "'" + s.replace("'", "'\\''") + "'";
    }

    /** Runs a short command as real root via su; returns stdout or null on error/timeout. */
    static String suCapture(String script, int timeoutSec) {
        Process p = null;
        try {
            ProcessBuilder pb = new ProcessBuilder("/system/bin/sh", "-c", "exec su -c " + shellQuote(script));
            pb.environment().put("PATH", HOST_PATH);
            pb.redirectErrorStream(true);
            p = pb.start();
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            Process proc = p;
            Thread reader = new Thread(() -> {
                try (InputStream in = proc.getInputStream()) {
                    copy(in, out);
                } catch (IOException ignored) {
                    // process ended
                }
            }, "su-capture");
            reader.setDaemon(true);
            reader.start();
            if (!p.waitFor(timeoutSec, java.util.concurrent.TimeUnit.SECONDS)) {
                p.destroyForcibly();
                return null;
            }
            reader.join(1000);
            return p.exitValue() == 0 ? out.toString("UTF-8") : null;
        } catch (IOException e) {
            return null;
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return null;
        } finally {
            if (p != null && p.isAlive()) p.destroyForcibly();
        }
    }

    /** True when su grants real uid=0 root to this app. */
    static boolean hasRoot() {
        String out = suCapture("id -u", 5);
        return out != null && "0".equals(out.trim());
    }

    /**
     * The native root chroot command running args (inside the userland, as real uid=0 in /root,
     * with no proot/ptrace); the host environment for it is in {@link #env(ProcessBuilder)}.
     */
    List<String> command(String... args) throws IOException {
        prepare();
        if (!hasRoot()) {
            throw new IOException("未获得 Root 权限：请在 KernelSU / SukiSU / Magisk 中授予本应用 Root 权限");
        }
        StringBuilder inner = new StringBuilder();
        inner.append("/system/bin/sh ")
                .append(shellQuote(chrootExec.getPath()))
                .append(' ')
                .append(shellQuote(rootfs.getPath()))
                .append(' ')
                .append(android.os.Process.myUid());
        for (String a : args) inner.append(' ').append(shellQuote(a));
        List<String> c = new ArrayList<>(3);
        c.add("/system/bin/sh");
        c.add("-c");
        c.add("exec su -c " + shellQuote(inner.toString()));
        return c;
    }

    void env(ProcessBuilder pb) {
        pb.environment().clear();
        for (String kv : env()) pb.environment().put(kv.substring(0, kv.indexOf('=')), kv.substring(kv.indexOf('=') + 1));
    }

    /** Host environment for launching su + chroot, as KEY=value. */
    String[] env() {
        return new String[]{"PATH=" + HOST_PATH, "HOME=" + base.getPath()};
    }

    /**
     * Writes the host helper script that enters an isolated mount namespace (unshare -m),
     * bind-mounts Linux virtual filesystems and Android host directories with real root
     * permissions, and execs into the Alpine rootfs via kernel chroot.
     */
    private void writeChrootExec() throws IOException {
        String script = "#!/system/bin/sh\n"
                + "set -eu\n"
                + "ROOTFS=\"$1\"\n"
                + "APP_UID=\"$2\"\n"
                + "shift 2\n"
                + "\n"
                + "if [ \"${1:-}\" != \"--in-ns\" ]; then\n"
                + "  if command -v unshare >/dev/null 2>&1 && unshare -m true 2>/dev/null; then\n"
                + "    exec unshare -m /system/bin/sh \"$0\" \"$ROOTFS\" \"$APP_UID\" --in-ns \"$@\"\n"
                + "  elif command -v toybox >/dev/null 2>&1 && toybox unshare -m true 2>/dev/null; then\n"
                + "    exec toybox unshare -m /system/bin/sh \"$0\" \"$ROOTFS\" \"$APP_UID\" --in-ns \"$@\"\n"
                + "  else\n"
                + "    echo 'chroot-exec: unshare -m not available; refusing to mount in shared host namespace' >&2\n"
                + "    exit 1\n"
                + "  fi\n"
                + "else\n"
                + "  shift\n"
                + "fi\n"
                + "\n"
                + "# Fail-closed mount namespace isolation: Android's /data is a shared mount group,\n"
                + "# and toybox's mount does NOT recursively apply rslave/rprivate. Use Alpine's musl\n"
                + "# busybox (or KSU busybox) to execute real MS_REC|MS_SLAVE + MS_REC|MS_PRIVATE\n"
                + "# before creating any bind mounts under $ROOTFS.\n"
                + "LD=\"$ROOTFS/lib/ld-musl-aarch64.so.1\"\n"
                + "[ -x \"$LD\" ] || LD=\"$ROOTFS/lib/ld-musl-x86_64.so.1\"\n"
                + "BB=\"$ROOTFS/bin/busybox\"\n"
                + "if [ -x \"$LD\" ] && [ -x \"$BB\" ]; then\n"
                + "  \"$LD\" \"$BB\" mount --make-rslave /\n"
                + "  \"$LD\" \"$BB\" mount --make-rprivate /\n"
                + "elif [ -x /data/adb/ksu/bin/busybox ]; then\n"
                + "  /data/adb/ksu/bin/busybox mount --make-rslave /\n"
                + "  /data/adb/ksu/bin/busybox mount --make-rprivate /\n"
                + "else\n"
                + "  echo 'chroot-exec: no busybox available for recursive mount isolation' >&2\n"
                + "  exit 1\n"
                + "fi\n"
                + "\n"
                + "bind_dir() {\n"
                + "  src=\"$1\"\n"
                + "  dst=\"$ROOTFS$2\"\n"
                + "  [ -d \"$src\" ] || return 0\n"
                + "  mkdir -p \"$dst\" 2>/dev/null || true\n"
                + "  mount --bind \"$src\" \"$dst\" 2>/dev/null || mount -o bind \"$src\" \"$dst\" 2>/dev/null || true\n"
                + "}\n"
                + "\n"
                + "rbind_dir() {\n"
                + "  src=\"$1\"\n"
                + "  dst=\"$ROOTFS$2\"\n"
                + "  [ -d \"$src\" ] || return 0\n"
                + "  mkdir -p \"$dst\" 2>/dev/null || true\n"
                + "  mount --rbind \"$src\" \"$dst\" 2>/dev/null || mount -o rbind \"$src\" \"$dst\" 2>/dev/null || bind_dir \"$src\" \"$2\"\n"
                + "}\n"
                + "\n"
                + "bind_dir /dev /dev\n"
                + "bind_dir /dev/pts /dev/pts\n"
                + "bind_dir /proc /proc\n"
                + "bind_dir /sys /sys\n"
                + "mkdir -p \"$ROOTFS/dev/shm\" 2>/dev/null || true\n"
                + "mount -t tmpfs tmpfs \"$ROOTFS/dev/shm\" 2>/dev/null || bind_dir \"$ROOTFS/tmp\" /dev/shm\n"
                + "\n"
                + "bind_dir /system /system\n"
                + "rbind_dir /apex /apex\n"
                + "bind_dir /linkerconfig /linkerconfig\n"
                + "bind_dir /vendor /vendor\n"
                + "bind_dir /product /product\n"
                + "bind_dir /system_ext /system_ext\n"
                + "bind_dir /odm /odm\n"
                + "bind_dir /data /data\n"
                + "rbind_dir /storage /storage\n"
                + "if [ -d /storage/emulated/0 ]; then\n"
                + "  bind_dir /storage/emulated/0 /sdcard\n"
                + "elif [ -d /sdcard ]; then\n"
                + "  bind_dir /sdcard /sdcard\n"
                + "fi\n"
                + "\n"
                + "mkdir -p \"$ROOTFS/root/.agyapp\" \"$ROOTFS/root/.gemini/antigravity-cli\" \"$ROOTFS/root/work\" \"$ROOTFS/tmp\" 2>/dev/null || true\n"
                + "chmod 1777 \"$ROOTFS/tmp\" 2>/dev/null || true\n"
                + "if [ -n \"$APP_UID\" ] && [ \"$APP_UID\" != \"0\" ]; then\n"
                + "  chown \"$APP_UID:$APP_UID\" \"$ROOTFS/root/.agyapp\" \"$ROOTFS/root/.gemini\" \"$ROOTFS/root/.gemini/antigravity-cli\" 2>/dev/null || true\n"
                + "fi\n"
                + "\n"
                + "CHROOT_BIN=\"\"\n"
                + "for cand in /system/bin/chroot /sbin/chroot /vendor/bin/chroot; do\n"
                + "  if [ -x \"$cand\" ]; then CHROOT_BIN=\"$cand\"; break; fi\n"
                + "done\n"
                + "if [ -z \"$CHROOT_BIN\" ]; then\n"
                + "  CHROOT_BIN=\"$(command -v chroot 2>/dev/null || true)\"\n"
                + "fi\n"
                + "\n"
                + "ENV_PATH=\"/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin:/system/bin:/system/xbin:/data/adb/ksu/bin:/data/adb/ap/bin\"\n"
                + "if [ -n \"$CHROOT_BIN\" ]; then\n"
                + "  exec \"$CHROOT_BIN\" \"$ROOTFS\" /usr/bin/env -i \\\n"
                + "    HOME=/root USER=root LOGNAME=root PATH=\"$ENV_PATH\" \\\n"
                + "    SHELL=/bin/bash LANG=C.UTF-8 TERM=xterm-256color TMPDIR=/tmp \\\n"
                + "    AGY_APP_UID=\"$APP_UID\" AGY_RUNTIME=chroot \\\n"
                + "    /bin/sh -c 'cd /root && exec \"$@\"' -- \"$@\"\n"
                + "else\n"
                + "  exec toybox chroot \"$ROOTFS\" /usr/bin/env -i \\\n"
                + "    HOME=/root USER=root LOGNAME=root PATH=\"$ENV_PATH\" \\\n"
                + "    SHELL=/bin/bash LANG=C.UTF-8 TERM=xterm-256color TMPDIR=/tmp \\\n"
                + "    AGY_APP_UID=\"$APP_UID\" AGY_RUNTIME=chroot \\\n"
                + "    /bin/sh -c 'cd /root && exec \"$@\"' -- \"$@\"\n"
                + "fi\n";
        replace(chrootExec, script);
        chmod(chrootExec, 0755);
    }

    /**
     * Convenience wrapper for scripts expecting `rish`: inside root chroot we already have real
     * uid=0 and direct access to /system, /apex and /linkerconfig, so run via /system/bin/sh.
     */
    private void writeRish() throws IOException {
        File dir = new File(rootfs, "opt/agyapp");
        mkdirs(dir);
        StringBuilder env = new StringBuilder();
        for (java.util.Map.Entry<String, String> e : System.getenv().entrySet()) {
            String k = e.getKey();
            if (k.startsWith("ANDROID_") || k.endsWith("CLASSPATH") || k.equals("EXTERNAL_STORAGE")) {
                env.append("export ").append(k).append("='").append(e.getValue().replace("'", "'\\''")).append("'\n");
            }
        }
        replace(new File(dir, "android.env"), env.toString());
        File rish = new File(rootfs, "usr/local/bin/rish");
        File rishPart = new File(rish.getPath() + ".part");
        write(rishPart, "#!/bin/sh\n"
                + ". /opt/agyapp/android.env\n"
                + "export PATH=\"/system/bin:/system/xbin:/vendor/bin:/data/adb/ksu/bin:/data/adb/ap/bin:$PATH\"\n"
                + "exec /system/bin/sh \"$@\"\n");
        try {
            Os.chmod(rishPart.getPath(), 0755);
        } catch (ErrnoException e) {
            rishPart.delete();
            throw new IOException(e);
        }
        if (!rishPart.renameTo(rish)) {
            rishPart.delete();
            throw new IOException("could not move " + rishPart + " in");
        }
    }

    /** One at a time: the bridge's start and the terminal's may run together. */
    private static final Object PREPARING = new Object();

    /**
     * Per-start files: chroot launcher, DNS servers of the current network, hosts, and helper tools.
     * Each is replaced whole: programs running meanwhile read them.
     */
    private void prepare() throws IOException {
        synchronized (PREPARING) {
            prepareFiles();
        }
    }

    private void prepareFiles() throws IOException {
        mkdirs(tmp);
        writeChrootExec();
        writeRish();
        writeUsbTools();
        Set<String> dns = new LinkedHashSet<>();
        try {
            ConnectivityManager cm = (ConnectivityManager) context.getSystemService(Context.CONNECTIVITY_SERVICE);
            Network n = cm.getActiveNetwork();
            LinkProperties lp = n == null ? null : cm.getLinkProperties(n);
            if (lp != null) for (InetAddress a : lp.getDnsServers()) dns.add(a.getHostAddress());
        } catch (RuntimeException ignored) {
            // fallbacks below
        }
        dns.add("8.8.8.8");
        StringBuilder resolv = new StringBuilder();
        for (String d : dns) if (!d.contains("%")) resolv.append("nameserver ").append(d).append('\n');
        replace(new File(rootfs, "etc/resolv.conf"), resolv.toString());
        replace(new File(rootfs, "etc/hosts"), "127.0.0.1 localhost\n::1 localhost ip6-localhost ip6-loopback\n");
    }

    void writeBridge() throws IOException {
        replace(new File(rootfs, "opt/agyapp/bridge.py"), read(context.getAssets().open("bridge.py")));
    }

    /**
     * Installs agy-usb and agy-fastboot in the userland, pointing them at this app's abstract
     * UsbBridge socket so release and preview builds do not collide.
     */
    private void writeUsbTools() throws IOException {
        File dir = new File(rootfs, "opt/agyapp");
        mkdirs(dir);
        String sock = UsbBridge.socketName(context);
        for (String[] tool : new String[][]{{"agy-usb.py", "agy-usb"}, {"agy-fastboot.py", "agy-fastboot"}}) {
            File script = new File(dir, tool[0]);
            replace(script, read(context.getAssets().open(tool[0])));
            chmod(script, 0755);
            File binFile = new File(rootfs, "usr/local/bin/" + tool[1]);
            File part = new File(binFile.getPath() + ".part");
            write(part, "#!/bin/sh\n"
                    + "export AGY_USB_SOCKET=" + sock + "\n"
                    + "exec python3 /opt/agyapp/" + tool[0] + " \"$@\"\n");
            try {
                Os.chmod(part.getPath(), 0755);
            } catch (ErrnoException e) {
                part.delete();
                throw new IOException(e);
            }
            if (!part.renameTo(binFile)) {
                part.delete();
                throw new IOException("could not move " + part + " in");
            }
        }
    }

    // ---- files ----

    static void write(File f, String text) throws IOException {
        mkdirs(f.getParentFile());
        try (OutputStream out = new FileOutputStream(f)) {
            out.write(text.getBytes(StandardCharsets.UTF_8));
        }
    }

    private static long assetLength(AssetManager assets, String name) throws IOException {
        try (android.content.res.AssetFileDescriptor fd = assets.openFd(name)) {
            return fd.getLength();
        }
    }

    /**
     * Writes a file others read meanwhile (agy's settings): aside, then moved in, so it is
     * never seen half written. A symbolic link is written through, as it was.
     */
    static void replace(File f, String text) throws IOException {
        if (java.nio.file.Files.isSymbolicLink(f.toPath())) { write(f, text); return; }
        File part = new File(f.getPath() + ".part");
        write(part, text);
        if (!part.renameTo(f)) {
            part.delete();
            throw new IOException("could not replace " + f);
        }
    }

    static String read(InputStream in) throws IOException {
        try (InputStream i = in) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            copy(i, out);
            return out.toString("UTF-8");
        }
    }

    static void copy(InputStream in, OutputStream out) throws IOException {
        byte[] buf = new byte[1 << 16];
        for (int n; (n = in.read(buf)) > 0; ) out.write(buf, 0, n);
    }

    /** A file inside the userland, by its path there (including bind-mounted /sdcard and /storage). */
    File file(String path) {
        if (path == null) return rootfs;
        if (path.equals("/sdcard") || path.startsWith("/sdcard/")) {
            File ext = android.os.Environment.getExternalStorageDirectory();
            if (ext != null) {
                String rel = path.length() > 8 ? path.substring(8) : "";
                return rel.isEmpty() ? ext : new File(ext, rel);
            }
        }
        if (path.equals("/storage") || path.startsWith("/storage/")) {
            return new File(path);
        }
        File f = new File(rootfs, path.startsWith("/") ? path.substring(1) : path);
        try {
            String base = rootfs.getCanonicalPath();
            String resolved = f.getCanonicalPath();
            if (!resolved.equals(base) && !resolved.startsWith(base + File.separator)) return rootfs;
        } catch (IOException ignored) {
            if (path.contains("..")) return rootfs;
        }
        return f;
    }

    /** The running bridge's pid (it writes it), or 0: none, or a process that got its pid since. */
    int bridgePid() {
        try {
            int pid = Integer.parseInt(read(new FileInputStream(file(HOME + "/.agyapp/bridge.pid"))).trim());
            if (pid <= 0) return 0;
            // Under root chroot the bridge runs as uid=0, so app uid cannot read /proc/<pid>/cmdline directly.
            String out = suCapture("grep -a -q bridge.py /proc/" + pid + "/cmdline 2>/dev/null && echo ok", 3);
            if (out != null && out.contains("ok")) return pid;
            try {
                String cmd = read(new FileInputStream("/proc/" + pid + "/cmdline"));
                return cmd.contains("bridge.py") ? pid : 0;
            } catch (IOException e) {
                return 0;
            }
        } catch (IOException | NumberFormatException e) {
            return 0;
        }
    }

    /** Sends a POSIX signal to the running root bridge process. */
    void signalBridge(int pid, int sig) {
        if (pid <= 0) return;
        suCapture("kill -" + sig + " " + pid + " 2>/dev/null || true", 3);
    }

    /** The last bytes of a file, whole lines. */
    static String tail(File f, int bytes) {
        try (java.io.RandomAccessFile r = new java.io.RandomAccessFile(f, "r")) {
            long start = Math.max(0, r.length() - bytes);
            r.seek(start);
            byte[] b = new byte[(int) (r.length() - start)];
            r.readFully(b);
            String s = new String(b, StandardCharsets.UTF_8);
            return start > 0 && s.indexOf('\n') >= 0 ? s.substring(s.indexOf('\n') + 1) : s;
        } catch (IOException e) {
            return "";
        }
    }
}
