#!/usr/bin/env python3
"""Transparent PTY relay that observes Warp's Claude notification identifiers."""
import argparse
import errno
import fcntl
import json
import os
import selectors
import signal
import struct
import subprocess
import sys
import termios
import time
import tty
import atexit

from session_store import PANE, Store


class Notifications:
    prefix = b"\x1b]777;notify;warp://cli-agent;"

    def __init__(self, callback):
        self.pending = b""
        self.callback = callback

    def feed(self, data):
        self.pending += data
        while True:
            start = self.pending.find(self.prefix)
            if start < 0:
                self.pending = self.pending[-len(self.prefix):]
                return
            if start:
                self.pending = self.pending[start:]
            offset = len(self.prefix)
            ends = [(position, length) for position, length in
                    [(self.pending.find(b"\x07", offset), 1),
                     (self.pending.find(b"\x1b\\", offset), 2)] if position >= 0]
            if not ends:
                if len(self.pending) > 1024 * 1024:
                    self.pending = b""
                return
            end, length = min(ends)
            raw = self.pending[offset:end]
            self.pending = self.pending[end + length:]
            try:
                value = json.loads(raw)
                if isinstance(value, dict) and value.get("agent") == "claude":
                    self.callback(value)
            except (ValueError, OSError):
                continue


def write_all(fd, data):
    view = memoryview(data)
    while view:
        try:
            view = view[os.write(fd, view):]
        except InterruptedError:
            continue


def run(command, store, pane, cwd=None):
    if not command or not os.path.isabs(command[0]) or not os.access(command[0], os.X_OK):
        raise ValueError("Expected an absolute executable path")
    if not PANE.fullmatch(pane):
        raise ValueError("Invalid Warp pane identifier")
    if not (os.isatty(0) and os.isatty(1)):
        return subprocess.call(command, cwd=cwd)

    # Record explicit attach/resume commands even when an idle background
    # session does not emit a new notification on attachment.
    if len(command) == 3 and command[1] == "attach":
        job = store.config / "jobs" / command[2] / "state.json"
        from session_store import JOB, read_json
        if JOB.fullmatch(command[2]):
            data = read_json(job)
            if data:
                sid = data.get("resumeSessionId") or data.get("sessionId")
                try:
                    store.bind(pane, sid, data.get("cwd"), "attach", command[2], "explicit-attach")
                except (ValueError, OSError):
                    pass

    pid, master = os.forkpty()
    if pid == 0:
        try:
            if cwd:
                os.chdir(cwd)
            os.environ["WARP_CLAUDE_PROXY_ACTIVE"] = "1"
            os.execv(command[0], command)
        finally:
            os._exit(127)

    previous = termios.tcgetattr(0)
    atexit.register(termios.tcsetattr, 0, termios.TCSADRAIN, previous)
    handlers = {}
    current = [None]

    def observe(payload):
        identity = (payload.get("session_id"), payload.get("cwd"))
        if identity != current[0]:
            # This stream belongs to the actual display pane, unlike a
            # background worker's inherited terminal environment.
            binding = store.record_event(pane, payload, "terminal-stream")
            if binding:
                current[0] = identity

    def resize(*_):
        try:
            size = os.get_terminal_size(1)
            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", size.lines, size.columns, 0, 0))
        except OSError:
            pass

    def forward(number, _frame):
        try:
            os.killpg(pid, number)
        except ProcessLookupError:
            pass

    parser = Notifications(observe)
    last_archive = time.monotonic()
    selector = selectors.DefaultSelector()
    try:
        for number, handler in [(signal.SIGWINCH, resize), (signal.SIGTERM, forward), (signal.SIGHUP, forward)]:
            handlers[number] = signal.signal(number, handler)
        resize()
        tty.setraw(0)
        selector.register(master, selectors.EVENT_READ)
        selector.register(0, selectors.EVENT_READ)
        alive = True
        while alive:
            for key, _ in selector.select(timeout=1):
                try:
                    data = os.read(key.fd, 65536)
                except OSError as error:
                    if error.errno == errno.EIO and key.fd == master:
                        alive = False
                        break
                    raise
                if not data:
                    if key.fd == master:
                        alive = False
                    else:
                        selector.unregister(0)
                    continue
                if key.fd == master:
                    write_all(1, data)
                    parser.feed(data)
                else:
                    write_all(master, data)
            if time.monotonic() - last_archive > 30:
                store.maintain()
                last_archive = time.monotonic()
    finally:
        termios.tcsetattr(0, termios.TCSADRAIN, previous)
        atexit.unregister(termios.tcsetattr)
        for number, handler in handlers.items():
            signal.signal(number, handler)
        selector.close()
        os.close(master)
    _, status = os.waitpid(pid, 0)
    store.maintain()
    code = os.waitstatus_to_exitcode(status)
    return 128 - code if code < 0 else code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pane", required=True)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
    return run(command, Store(), args.pane)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError) as error:
        print("warp-claude: " + str(error), file=sys.stderr)
        sys.exit(1)
