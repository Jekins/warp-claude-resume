"""Regression tests for indefinite retention and exact background attachments."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from session_store import Store, read_json
from terminal_proxy import Notifications

ROOT = Path(__file__).resolve().parents[1]
PANE1 = "1" * 32
PANE2 = "2" * 32
SID1 = "11111111-1111-4111-8111-111111111111"
SID2 = "22222222-2222-4222-8222-222222222222"


class PersistenceFixture:
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.config = self.root / "claude"
        self.project = self.config / "projects" / "test-project"
        self.project.mkdir(parents=True)
        self.cwd = self.root / "project"
        self.cwd.mkdir()
        self.store = Store(self.root / "panes", self.config)
        self.binary = self.root / "claude-stub"
        self.binary.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
        self.binary.chmod(0o700)

    def tearDown(self):
        self.directory.cleanup()

    def transcript(self, sid=SID1, text="old conversation"):
        path = self.project / (sid + ".jsonl")
        path.write_text(json.dumps({"sessionId": sid, "message": text}) + "\n")
        return path

    def job(self, job_id="aabbccdd", sid=SID1):
        directory = self.config / "jobs" / job_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "state.json").write_text(json.dumps({"sessionId": sid, "resumeSessionId": sid, "cwd": str(self.cwd)}))

    def bind(self, pane=PANE1, sid=SID1, job=None):
        return self.store.bind(pane, sid, str(self.cwd), "attach" if job else "resume", job)


class PersistenceTests(PersistenceFixture, unittest.TestCase):
    def test_deleted_old_transcript_restores_exact_bytes(self):
        source = self.transcript()
        old = time.time() - 86400 * 400
        os.utime(source, (old, old))
        expected = source.read_bytes()
        binding = self.bind()
        source.unlink()
        restored = self.store.ensure_transcript(binding)
        self.assertEqual(restored.read_bytes(), expected)
        self.assertGreater(restored.stat().st_mtime, time.time() - 10)

    def test_live_transcript_is_not_overwritten_by_archive(self):
        source = self.transcript()
        binding = self.bind()
        source.write_text('{"newer":true}\n')
        self.store.ensure_transcript(binding)
        self.assertEqual(source.read_text(), '{"newer":true}\n')

    def test_background_restore_attaches_instead_of_resuming_duplicate(self):
        self.transcript()
        self.job()
        self.bind(job="aabbccdd")
        _, command = self.store.restore_command(PANE1, str(self.binary))
        self.assertEqual(command, [str(self.binary), "attach", "aabbccdd"])

    def test_foreground_flags_are_preserved(self):
        self.transcript()
        self.bind()
        _, command = self.store.restore_command(PANE1, str(self.binary), ["--permission-mode", "plan"])
        self.assertEqual(command, [str(self.binary), "--permission-mode", "plan", "--resume", SID1])

    def test_two_panes_in_same_directory_keep_distinct_sessions(self):
        self.transcript(SID1)
        self.transcript(SID2)
        self.bind(PANE1, SID1)
        self.bind(PANE2, SID2)
        self.assertEqual(self.store.restore_command(PANE1, str(self.binary))[1][-1], SID1)
        self.assertEqual(self.store.restore_command(PANE2, str(self.binary))[1][-1], SID2)

    def test_background_hook_does_not_overwrite_inherited_pane(self):
        self.transcript(SID1)
        self.transcript(SID2)
        self.bind(PANE1, SID1)
        self.job(sid=SID2)
        self.store.record_event(PANE1, {"session_id": SID2, "cwd": str(self.cwd)}, background=True)
        self.assertEqual(read_json(self.store.metadata_path(PANE1))["session_id"], SID1)

    def test_background_clear_updates_verified_attachment_only(self):
        self.transcript(SID1)
        self.transcript(SID2)
        self.job()
        self.bind(PANE2, SID1, "aabbccdd")
        with patch.dict(os.environ, {"CLAUDE_JOB_DIR": str(self.config / "jobs" / "aabbccdd")}):
            self.store.record_event(PANE1, {"session_id": SID2, "cwd": str(self.cwd)}, background=True)
        self.assertFalse((self.store.state / PANE1).exists())
        self.assertEqual(read_json(self.store.metadata_path(PANE2))["session_id"], SID2)

    def test_changed_background_resume_id_is_refreshed_without_interaction(self):
        self.transcript(SID1)
        self.transcript(SID2)
        self.job()
        self.bind(job="aabbccdd")
        self.job(sid=SID2)
        self.store.maintain()
        self.assertEqual(read_json(self.store.metadata_path(PANE1))["session_id"], SID2)

    def test_maintain_refreshes_archive_after_messages_are_appended(self):
        source = self.transcript()
        self.bind()
        with source.open("a") as handle:
            handle.write('{"message":"latest"}\n')
        expected = source.read_bytes()
        self.store.maintain()
        source.unlink()
        binding = read_json(self.store.metadata_path(PANE1))
        self.assertEqual(self.store.ensure_transcript(binding).read_bytes(), expected)

    def test_partial_final_record_does_not_replace_good_archive(self):
        source = self.transcript()
        binding = self.bind()
        expected = (self.store.archive / SID1 / "transcript.jsonl").read_bytes()
        with source.open("a") as handle:
            handle.write('{"partial":')
        with self.assertRaises(OSError):
            self.store.protect(binding)
        self.assertEqual((self.store.archive / SID1 / "transcript.jsonl").read_bytes(), expected)

    def test_injection_and_traversal_are_rejected(self):
        for sid in [None, "--dangerously-skip-permissions", "$(touch HACKED)", "../other"]:
            with self.subTest(sid=sid), self.assertRaises(ValueError):
                self.store.bind(PANE1, sid, str(self.cwd))
        with self.assertRaises(ValueError):
            self.store.bind("../outside", SID1, str(self.cwd))
        with self.assertRaises(ValueError):
            self.store.bind(PANE1, SID1, str(self.cwd) + "\nother")

    def test_tampered_archive_cannot_write_outside_projects(self):
        source = self.transcript()
        binding = self.bind()
        source.unlink()
        manifest_path = self.store.archive / SID1 / "manifest.json"
        data = read_json(manifest_path)
        data["source"] = str(self.root / (SID1 + ".jsonl"))
        manifest_path.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            self.store.ensure_transcript(binding)
        self.assertFalse((self.root / (SID1 + ".jsonl")).exists())

    def test_private_archive_permissions(self):
        self.transcript()
        self.bind()
        self.assertEqual((self.store.archive / SID1).stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.store.archive / SID1 / "transcript.jsonl").stat().st_mode & 0o777, 0o600)

    def test_mismatched_pane_metadata_is_rejected(self):
        self.transcript()
        self.bind()
        path = self.store.metadata_path(PANE1)
        data = read_json(path)
        data["pane"] = PANE2
        path.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            self.store.restore_command(PANE1, str(self.binary))

    def test_missing_transcript_fails_instead_of_starting_empty_session(self):
        self.bind()
        with self.assertRaises(ValueError):
            self.store.restore_command(PANE1, str(self.binary))

    def test_default_age_limit_is_disabled(self):
        self.store.state.mkdir()
        (self.store.state / PANE1).write_text(f"{SID1}\t{self.cwd}\t1\t\n")
        command = f'source "{ROOT / "warp-claude.zsh"}"; _warp_claude_reattach'
        stubdir = self.root / "bin"
        stubdir.mkdir()
        executable = stubdir / "claude"
        executable.write_text("#!/bin/sh\nprintf 'EXACT:%s:%s\\n' \"$1\" \"$2\"\n")
        executable.chmod(0o700)
        environment = {"PATH": str(stubdir) + ":/usr/bin:/bin", "HOME": str(Path.home()),
                       "WARP_CLAUDE_STATE_DIR": str(self.store.state), "WARP_CLAUDE_AUTO": "1",
                       "WARP_TERMINAL_SESSION_UUID": PANE1, "WARP_CLAUDE_HELPER": "/nonexistent",
                       "WARP_CLAUDE_PROXY_ACTIVE": "1"}
        result = subprocess.run(["/bin/zsh", "-dfi", "-c", command], env=environment,
                                capture_output=True, text=True, check=True)
        self.assertIn("EXACT:--resume:" + SID1, result.stdout)

    def test_stream_parser_handles_every_chunk_boundary(self):
        expected = {"agent": "claude", "session_id": SID1, "cwd": str(self.cwd)}
        frame = Notifications.prefix + json.dumps(expected).encode() + b"\x07"
        for offset in range(len(frame) + 1):
            seen = []
            parser = Notifications(seen.append)
            parser.feed(frame[:offset])
            parser.feed(frame[offset:])
            self.assertEqual(seen, [expected])

    def test_stream_parser_ignores_malformed_and_non_claude_frames(self):
        seen = []
        parser = Notifications(seen.append)
        parser.feed(Notifications.prefix + b"not-json\x07")
        parser.feed(Notifications.prefix + b'{"agent":"other"}\x1b\\')
        self.assertEqual(seen, [])

    def test_stream_parser_bounds_unterminated_frames(self):
        parser = Notifications(lambda _: self.fail("Unterminated frame must not execute"))
        parser.feed(Notifications.prefix + b"x" * (1024 * 1024 + 1))
        self.assertLess(len(parser.pending), 1024)


if __name__ == "__main__":
    unittest.main()
