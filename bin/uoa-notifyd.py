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
BUS_NAME = "org.freedesktop.Notifications"
BUS_PATH = "/org/freedesktop/Notifications"

# Idle timeout. The daemon is started on demand by the first notification of a
# cron cycle and exits once nothing has needed it for a while, so it does not
# outlive a session or need a service file to be tidy.
IDLE_EXIT_SECONDS = 6 * 3600


def runtime_dir():
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    path = os.path.join(base, "uoa-notify")
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
        U.log(TOOL, "info", f"action '{action}' on {item['tag'] or nid}")
        if action == "dismiss":
            return
        self._open(item)
        self._mark_read(item)
        with self.lock:
            self.items.pop(nid, None)
            if item["tag"] and self.by_tag.get(item["tag"]) == nid:
                self.by_tag.pop(item["tag"], None)

    def _open(self, item):
        url = item.get("url")
        if not url:
            return
        try:
            subprocess.Popen(["xdg-open", url], start_new_session=True,
                             stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            U.log(TOOL, "info", f"opened {url}")
        except OSError as exc:
            U.log(TOOL, "warn", f"cannot open {url}: {exc}")

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


def main():
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
    Daemon().run()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
