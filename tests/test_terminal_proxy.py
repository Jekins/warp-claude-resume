"""Run the real relay in a disposable terminal with a fake Claude process."""
import fcntl
import json
import os
import pty
import select
import signal
import struct
import sys
import termios
import time
import unittest
from pathlib import Path

from test_persistence import PersistenceFixture, ROOT, PANE1, SID1


class TerminalProxyTests(PersistenceFixture, unittest.TestCase):
    def test_real_pty_preserves_input_output_size_and_exact_binding(self):
        self.transcript()
        script = self.root / "fake-claude.py"
        frame = {"agent": "claude", "session_id": SID1, "cwd": str(self.cwd)}
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import os,sys,time\n"
            f"frame = b'\\x1b]777;notify;warp://cli-agent;' + {json.dumps(frame)!r}.encode() + b'\\x07'\n"
            "for part in [frame[:12], frame[12:]]:\n"
            " os.write(1,part);time.sleep(.03)\n"
            "print('READY:'+str(os.isatty(0))+':'+str(os.isatty(1)),flush=True)\n"
            "line=sys.stdin.readline().strip()\n"
            "print('INPUT:'+line,flush=True)\n"
            "print('COLUMNS:'+str(os.get_terminal_size().columns),flush=True)\n"
        )
        script.chmod(0o700)
        pid, master = pty.fork()
        if pid == 0:
            os.environ["WARP_CLAUDE_STATE_DIR"] = str(self.store.state)
            os.environ["CLAUDE_CONFIG_DIR"] = str(self.config)
            os.execv(sys.executable, [sys.executable, str(ROOT / "terminal_proxy.py"),
                                     "--pane", PANE1, "--", str(script)])
        output = b""
        sent = False
        try:
            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                readable, _, _ = select.select([master], [], [], .1)
                if not readable:
                    continue
                try:
                    block = os.read(master, 65536)
                except OSError:
                    break
                if not block:
                    break
                output += block
                if b"READY:True:True" in output and not sent:
                    os.write(master, b"test-input\n")
                    sent = True
            self.assertIn(b"INPUT:test-input", output)
            self.assertIn(b"COLUMNS:120", output)
            binding = json.loads(self.store.metadata_path(PANE1).read_text())
            self.assertEqual(binding["session_id"], SID1)
            self.assertEqual(binding["origin"], "terminal-stream")
        finally:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)
            os.close(master)


if __name__ == "__main__":
    unittest.main()
