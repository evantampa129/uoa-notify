#!/usr/bin/env python3
"""Persistent notification daemon: keeps every notification clickable.

Why this exists
---------------

The obvious way to make a desktop notification clickable is `notify-send
--action`, and it does not survive contact with reality. `--action` implies
`--wait`, so one process blocks per notification; GNOME Shell ignores
`--expire-time` and keeps notifications in its tray indefinitely, so that wait
never ends on its own. The result observed in practice was 97 live helper
processes, the oldest fourteen hours old.

Bounding the wait fixes the process leak and creates a worse bug. GNOME keeps
the notification in its tray after the helper has exited, so the banner is
still there, still looks clickable — and the connection that would have
received the click is gone. Clicking does nothing, silently, forever. That is
the failure this daemon removes.

`ActionInvoked` is broadcast on the session bus, and the notification's own id
is the first argument. So one long-lived process can subscribe once, keep a
map of notification id to what that notification points at, and act on every
click regardless of which process originally posted it, and regardless of how
long ago.

Protocol
--------

A line-delimited JSON request on a Unix socket at
`$XDG_RUNTIME_DIR/uoa-notify/notifyd.sock`:

    {"title": ..., "body": ..., "urgency": "low|normal|critical",
     "url": ..., "tag": ..., "source": ..., "message_id": ...}

The reply is one JSON line, `{"ok": true, "id": <notification id>}`.

Requests carrying the same tag replace the live notification for that tag
rather than stacking a second copy, which is what the per-item lock file used
to be for.

On a click the daemon opens the URL and then asks notify.py to record the
read, in that order, so the browser never waits on a network round trip.
"""

import errno
import json
import os
import socket
import subprocess
import sys
import threading
import time

# The system pygobject lives in dist-packages, which is not always on the
# path of the interpreter that ends up running this. Add it rather than fail.
for _extra in ("/usr/lib/python3/dist-packages",):
    if os.path.isdir(_extra) and _extra not in sys.path:
        sys.path.append(_extra)

try:
    import gi
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib
except (ImportError, ValueError) as exc:  # pragma: no cover - environment
    sys.stderr.write(
        f"uoa-notifyd: pygobject is required ({exc}).\n"
        "Install it with: sudo apt install python3-gi\n")
    raise SystemExit(3)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import uoa_common as U  # noqa: E402

TOOL = "notifyd"


def log(tool, level, msg):
    U.log(tool, level, msg)

BUS_NAME = "org.freedesktop.Notifications"
BUS_PATH = "/org/freedesktop/Notifications"

# Idle timeout. The daemon is started on demand by the first notification of a
# cron cycle and exits once nothing has needed it for a while, so it does not
# outlive a session or need a service file to be tidy.
IDLE_EXIT_SECONDS = 6 * 3600

# How often to pull read state back from Gmail. cron does this every fifteen
# minutes, which is as fine-grained as cron gets and still means a mail read
# on the phone can keep nagging on the laptop for a quarter of an hour. The
# daemon is already resident, so it can close that gap for free.
#
# Two minutes is a deliberate floor rather than a maximum: each pass is an
# assistant CLI round trip, so a much shorter interval would spend more time
# syncing than not, for a difference nobody perceives.
SYNC_INTERVAL_SECONDS = 120


def adopt_session_env():
    """Put the live session's variables into this process's environment.

    GIO reads XDG_DATA_DIRS from the real environment, not from an env dict
    handed to a subprocess, so passing them along at launch time is not
    enough: the daemon itself has to hold them.
    """
    for key, value in U.desktop_env().items():
        os.environ.setdefault(key, value)


def runtime_dir():
    """Resolved by uoa_common so the daemon and its clients always agree.

    cron has no XDG_RUNTIME_DIR; the desktop session does. Resolving it
    independently here is how the daemon ended up listening on one path while
    the cron jobs looked for it on another.
    """
    path = os.path.join(U.runtime_base(), "uoa-notify")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def socket_path():
    return os.path.join(runtime_dir(), "notifyd.sock")


class Daemon:
    def __init__(self):
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.loop = GLib.MainLoop()
        # notification id -> item it points at
        self.items = {}
        # tag -> notification id, so a repeat replaces rather than stacks
        self.by_tag = {}
        self.lock = threading.Lock()
        self.last_activity = time.time()
        self.sync_proc = None
        self.bus.signal_subscribe(None, BUS_NAME, None, BUS_PATH, None,
                                  Gio.DBusSignalFlags.NONE, self._on_signal)

    # ---------------------------------------------------------------- posting

    def notify(self, req):
        """Post one notification and remember what it points at."""
        tag = (req.get("tag") or "").strip()
        urgency = {"low": 0, "normal": 1, "critical": 2}.get(
            req.get("urgency", "normal"), 1)
        hints = {
            "urgency": GLib.Variant("y", urgency),
            # Associates the notification with the installed desktop entry, so
            # GNOME groups it under one application instead of showing each as
            # an unrelated stray.
            "desktop-entry": GLib.Variant("s", "uoa-notify"),
        }
        # Reusing the previous id for the same tag is the documented way to
        # replace a notification in place.
        replaces = self.by_tag.get(tag, 0) if tag else 0

        # "default" binds the click on the notification body; "open" adds the
        # visible button for the daemons that render one. Both land in the
        # same handler.
        actions = ["default", "Open", "open", "Open"]

        # expire_time 0 means "never expire on its own". That is safe here in
        # a way it was not before: nothing is blocked waiting on it.
        res = self.bus.call_sync(
            BUS_NAME, BUS_PATH, BUS_NAME, "Notify",
            GLib.Variant("(susssasa{sv}i)", (
                "UoA", replaces, "mail-unread",
                req.get("title", "UoA")[:200],
                req.get("body", "")[:400],
                actions, hints, 0)),
            None, Gio.DBusCallFlags.NONE, 10000, None)
        nid = res.unpack()[0]

        with self.lock:
            self.items[nid] = {
                "url": req.get("url", ""),
                "tag": tag,
                "source": req.get("source", ""),
                "message_id": req.get("message_id", ""),
                "title": req.get("title", ""),
            }
            if tag:
                self.by_tag[tag] = nid
            self.last_activity = time.time()
        return nid

    # --------------------------------------------------------------- clicking

    def _on_signal(self, conn, sender, path, iface, signal, params):
        args = params.unpack()
        nid = args[0]
        with self.lock:
            item = self.items.get(nid)
        if item is None:
            # A click on a notification this daemon did not post - almost
            # always one left in the tray by an older helper that has since
            # exited. Nothing can be done for it, but it is logged: silence
            # here is what made the original bug so hard to see, because a
            # click that lands nowhere looks identical to no click at all.
            if signal == "ActionInvoked":
                log(TOOL, "info",
                    f"click on unknown notification id={nid} "
                    f"action='{args[1]}' - posted before this daemon started")
            return
        if signal == "ActionInvoked":
            self._activate(nid, item, args[1])
        elif signal == "NotificationClosed":
            # Dismissed rather than opened: forget it, but leave the item
            # unread so the next cron cycle brings it back.
            with self.lock:
                self.items.pop(nid, None)
                if item["tag"] and self.by_tag.get(item["tag"]) == nid:
                    self.by_tag.pop(item["tag"], None)

    def _activate(self, nid, item, action):
        log(TOOL, "info", f"action '{action}' on {item['tag'] or nid}")
        if action == "dismiss":
            return
        self._open(item)
        self._mark_read(item)
        with self.lock:
            self.items.pop(nid, None)
            if item["tag"] and self.by_tag.get(item["tag"]) == nid:
                self.by_tag.pop(item["tag"], None)

    def _open(self, item):
        """Open the page, and report success only when it actually opened.

        The first version called xdg-open, discarded its output and logged
        success unconditionally. That produced a log line claiming the page
        had opened while nothing happened on screen: xdg-open needs
        XDG_DATA_DIRS and XDG_CURRENT_DESKTOP to work out which browser to
        use, had neither, and failed without printing anything. Two lessons
        are encoded here — try the session's own launcher first, and never
        report success without checking.
        """
        url = item.get("url")
        if not url:
            return False

        # 1. GIO's default handler: the same lookup the desktop itself does,
        # performed in process, so it depends on neither PATH nor a helper.
        try:
            if Gio.AppInfo.launch_default_for_uri(url, None):
                log(TOOL, "info", f"opened {url}")
                return True
            log(TOOL, "warn", "GIO reported no default handler")
        except GLib.Error as exc:
            log(TOOL, "warn", f"GIO could not open {url}: {exc.message}")

        # 2. External launchers, best-behaved on GNOME first.
        env = U.desktop_env()
        for cmd in (["gio", "open", url], ["xdg-open", url]):
            try:
                r = subprocess.run(cmd, env=env, timeout=20,
                                   stdin=subprocess.DEVNULL,
                                   capture_output=True, text=True)
            except (OSError, subprocess.SubprocessError) as exc:
                log(TOOL, "warn", f"{cmd[0]} failed: {exc}")
                continue
            if r.returncode == 0:
                log(TOOL, "info", f"opened {url} with {cmd[0]}")
                return True
            log(TOOL, "warn", f"{cmd[0]} exited {r.returncode}: "
                              f"{(r.stderr or '').strip()[:200]}")

        log(TOOL, "error", f"could not open {url} by any method")
        return False

    def _mark_read(self, item):
        """Record the read out of process, so the click returns immediately.

        The Gmail push inside notify.py is a network round trip; running it
        here would block the main loop and with it every other notification.
        """
        if not (item.get("tag") or item.get("message_id")):
            return
        cmd = [os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "notify.py"), "--mark-read"]
        if item.get("source"):
            cmd += ["--source", item["source"]]
        if item.get("message_id"):
            cmd += ["--message-id", item["message_id"]]
        if item.get("tag"):
            cmd += ["--tag", item["tag"]]
        try:
            subprocess.Popen(cmd, start_new_session=True,
                             stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except OSError as exc:
            U.log(TOOL, "warn", f"cannot record read: {exc}")

    # ---------------------------------------------------------------- socket

    def serve(self, server):
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                try:
                    conn.settimeout(5)
                    data = b""
                    while not data.endswith(b"\n"):
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        data += chunk
                    req = json.loads(data.decode("utf-8") or "{}")
                    if req.get("command") == "ping":
                        reply = {"ok": True, "pong": True}
                    elif req.get("command") == "quit":
                        reply = {"ok": True}
                        conn.sendall((json.dumps(reply) + "\n").encode())
                        GLib.idle_add(self.loop.quit)
                        return
                    else:
                        nid = self.notify(req)
                        reply = {"ok": True, "id": nid}
                except (ValueError, OSError, GLib.Error) as exc:
                    reply = {"ok": False, "error": str(exc)}
                    U.log(TOOL, "warn", f"request failed: {exc}")
                try:
                    conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))
                except OSError:
                    pass

    # ------------------------------------------------------------ auto sync

    def _sync_tick(self):
        """Pull Gmail's read state onto the ledger, in the background.

        Runs out of process for the same reason the read push does: it is a
        network round trip, and blocking the main loop on it would freeze
        every notification for its duration.

        Overlapping runs are skipped rather than queued. A pass that is still
        going when the next tick fires means the network is slow, and starting
        a second one would only make that worse.
        """
        if self.sync_proc is not None and self.sync_proc.poll() is None:
            return True
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "sync-read-status.py")
        if not os.access(script, os.X_OK):
            return True
        try:
            self.sync_proc = subprocess.Popen(
                [script, "--quiet"], env=U.desktop_env(),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as exc:
            log(TOOL, "warn", f"cannot start read sync: {exc}")
        return True

    def _idle_check(self):
        if time.time() - self.last_activity > IDLE_EXIT_SECONDS:
            U.log(TOOL, "info", "idle; exiting")
            self.loop.quit()
            return False
        return True

    def run(self):
        path = socket_path()
        # A socket left behind by a crash would block bind(); it is safe to
        # remove because single_instance() has already proved no live daemon
        # is answering on it.
        try:
            os.unlink(path)
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                raise
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o077)          # the socket is private to this user
        try:
            server.bind(path)
        finally:
            os.umask(old)
        server.listen(16)

        threading.Thread(target=self.serve, args=(server,), daemon=True).start()
        GLib.timeout_add_seconds(300, self._idle_check)
        if "--no-sync" not in sys.argv:
            GLib.timeout_add_seconds(SYNC_INTERVAL_SECONDS, self._sync_tick)
            U.log(TOOL, "info",
                  f"auto-sync every {SYNC_INTERVAL_SECONDS}s")
        U.log(TOOL, "info", f"listening on {path}")
        try:
            self.loop.run()
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass


def single_instance():
    """True if a daemon is already answering on the socket."""
    path = socket_path()
    if not os.path.exists(path):
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(2)
            s.connect(path)
            s.sendall(b'{"command": "ping"}\n')
            return b"pong" in s.recv(256)
    except OSError:
        return False


def self_test():
    """Post one notification and report what the click actually did.

    This exists because every part of the chain fails silently. The daemon can
    be listening on a path the caller never looks at; the notification can be
    posted without an action bound to its body; the click can be delivered to
    a connection that has already exited. None of that produces an error
    anywhere — it produces a banner that does nothing.

    So: post through the real client path, wait for the real signal, and say
    plainly which link broke.
    """
    # Progress has to appear as it happens even when this is piped to a file
    # or watched by another process, so the caller can see which step stalled.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, OSError):
        pass

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    adopt_session_env()
    url = "https://www.di.uoa.gr/announcements/3156"

    print("1. daemon reachable on the socket the client uses")
    print(f"   socket: {U.notifyd_socket()}")
    if not U.notifyd_start(TOOL):
        print("   FAILED: could not reach or start the daemon")
        print("   check: python3 -c 'import gi'  (needs python3-gi)")
        return 1
    print("   ok")

    print("2. posting a notification through notify_send, as a checker would")
    posted = U.notify_send(
        "UoA - SELF TEST", "Click this notification now.", "normal", TOOL,
        url=url, tag="SELFTEST", source="", message_id="")
    if not posted:
        print("   FAILED: notify_send could not post")
        return 1
    print("   ok - the notification is on screen and will not expire")

    print("3. waiting up to 120s for your click...")
    print("   (click the notification body, or its Open button)")
    deadline = time.time() + 120
    marker = "action '"
    log_path = os.path.expanduser("~/.local/log/notifications.log")
    start_size = os.path.getsize(log_path) if os.path.exists(log_path) else 0
    while time.time() < deadline:
        time.sleep(1)
        if not os.path.exists(log_path):
            continue
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(start_size)
            tail = fh.read()
        if marker in tail and "SELFTEST" in tail:
            print("   ok - the click was received")
            print()
            print("PASS: the notification chain works end to end.")
            print(f"      your browser should now be showing {url}")
            return 0
    print("   FAILED: no click arrived within 120s")
    print()
    print("If you did click, the click is not reaching this process. Check:")
    print("  - gnome-shell is the notification server:")
    print("    gdbus call --session --dest org.freedesktop.Notifications \\")
    print("      --object-path /org/freedesktop/Notifications \\")
    print("      --method org.freedesktop.Notifications.GetServerInformation")
    print("  - 'actions' is in its capabilities (GetCapabilities)")
    print("  - no other notification daemon is running alongside it")
    return 1


def main():
    if "--self-test" in sys.argv:
        return self_test()
    if "--status" in sys.argv:
        running = single_instance()
        print(f"running={running} socket={socket_path()}")
        return 0 if running else 1
    if "--stop" in sys.argv:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(2)
                s.connect(socket_path())
                s.sendall(b'{"command": "quit"}\n')
                s.recv(256)
            print("stopped")
            return 0
        except OSError as exc:
            print(f"not running ({exc})")
            return 1
    if single_instance():
        U.log(TOOL, "info", "already running; nothing to do")
        return 0
    adopt_session_env()
    Daemon().run()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
