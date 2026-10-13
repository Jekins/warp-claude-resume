#!/usr/bin/env python3
"""Local, durable pane bindings and transcript protection. No external network I/O."""
import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

SESSION = re.compile(r"[0-9A-Za-z_][0-9A-Za-z_-]{0,63}\Z")
PANE = re.compile(r"[0-9a-fA-F]{32}\Z")
JOB = re.compile(r"[0-9a-f]{8}\Z")


def private_directory(path):
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.is_symlink() or not path.is_dir() or path.stat().st_uid != os.getuid():
        raise ValueError("Unsafe state directory")
    path.chmod(0o700)
    return path


def atomic_write(path, data):
    private_directory(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path):
    try:
        if path.is_symlink() or path.stat().st_size > 1024 * 1024:
            return None
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def valid_path(value):
    return (isinstance(value, str) and value.startswith("/")
            and len(value) <= 4096 and not any(unicodedata.category(c).startswith("C") for c in value))


class Store:
    def __init__(self, state=None, config=None):
        self.state = Path(state or os.environ.get("WARP_CLAUDE_STATE_DIR", str(Path.home() / ".warp-claude/panes")))
        self.config = Path(config or os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))).resolve()
        self.metadata = self.state / ".metadata"
        self.archive = self.state / ".archive"

    def metadata_path(self, pane):
        if not isinstance(pane, str) or not PANE.fullmatch(pane):
            raise ValueError("Invalid pane identifier")
        return self.metadata / (pane + ".json")

    def bindings(self):
        if not self.metadata.is_dir():
            return []
        return [data for path in self.metadata.glob("*.json")
                if (data := read_json(path)) and self.valid_binding(data)]

    @staticmethod
    def valid_binding(data):
        return (PANE.fullmatch(str(data.get("pane", ""))) is not None
                and isinstance(data.get("session_id"), str)
                and SESSION.fullmatch(data["session_id"]) is not None
                and valid_path(data.get("cwd"))
                and data.get("mode") in ("resume", "attach")
                and (data["mode"] != "attach" or JOB.fullmatch(str(data.get("job_id", ""))) is not None))

    def find_transcript(self, session_id):
        if not SESSION.fullmatch(session_id):
            return None
        root = (self.config / "projects").resolve()
        paths = []
        for path in root.glob("*/" + session_id + ".jsonl"):
            try:
                if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root):
                    paths.append(path)
            except OSError:
                continue
        return max(paths, key=lambda p: p.stat().st_mtime_ns) if paths else None

    def protect(self, binding):
        source = self.find_transcript(binding["session_id"])
        if source is None:
            return False
        directory = private_directory(self.archive / binding["session_id"])
        info = source.stat()
        manifest_path = directory / "manifest.json"
        old = read_json(manifest_path)
        fingerprint = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]
        if old and old.get("fingerprint") == fingerprint and (directory / "transcript.jsonl").is_file():
            return True
        fd, name = tempfile.mkstemp(prefix=".copy-", dir=directory)
        try:
            with os.fdopen(fd, "wb") as output, source.open("rb") as incoming:
                os.fchmod(output.fileno(), 0o600)
                # Capture a finite prefix even while the agent appends messages.
                remaining = info.st_size
                while remaining:
                    block = incoming.read(min(1024 * 1024, remaining))
                    if not block:
                        raise OSError("Transcript changed while being archived")
                    output.write(block)
                    remaining -= len(block)
                if info.st_size and block[-1:] != b"\n":
                    raise OSError("Transcript has an incomplete final record; retry later")
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, directory / "transcript.jsonl")
            manifest = {"session_id": binding["session_id"], "source": str(source),
                        "cwd": binding.get("cwd"), "job_id": binding.get("job_id"),
                        "fingerprint": fingerprint, "archived_at": time.time()}
            atomic_write(manifest_path, (json.dumps(manifest) + "\n").encode())
        finally:
            if os.path.exists(name):
                os.unlink(name)
        return True

    def ensure_transcript(self, binding):
        source = self.find_transcript(binding["session_id"])
        if source is not None:
            # An idle transcript must survive Claude's age-based startup cleanup.
            os.utime(source, None)
            return source
        directory = self.archive / binding["session_id"]
        manifest = read_json(directory / "manifest.json")
        if not manifest or manifest.get("session_id") != binding["session_id"]:
            return None
        target = Path(manifest.get("source", ""))
        root = (self.config / "projects").resolve()
        if (not target.is_absolute() or not target.resolve().is_relative_to(root)
                or target.name != binding["session_id"] + ".jsonl"):
            raise ValueError("Invalid archived transcript destination")
        archive = directory / "transcript.jsonl"
        if archive.is_symlink() or not archive.is_file():
            return None
        private_directory(target.parent)
        # Exclusive creation avoids replacing a transcript recreated by Claude.
        fd, temporary = tempfile.mkstemp(prefix=".restore-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as output, archive.open("rb") as incoming:
                os.fchmod(output.fileno(), 0o600)
                shutil.copyfileobj(incoming, output)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                pass
        finally:
            os.unlink(temporary)
        return target

    def bind(self, pane, session_id, cwd, mode="resume", job_id=None, origin="hook"):
        binding = {"pane": pane, "session_id": session_id, "cwd": cwd,
                   "mode": mode, "job_id": job_id, "origin": origin}
        if not self.valid_binding(binding):
            raise ValueError("Invalid session binding")
        private_directory(self.state)
        lock_path = self.state / ".lock"
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(lock_fd, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            record = f"{session_id}\t{cwd}\t{int(time.time())}\t\n"
            atomic_write(self.state / pane, record.encode())
            atomic_write(self.metadata_path(pane), (json.dumps(binding) + "\n").encode())
            self.protect(binding)
        return binding

    def job_for_session(self, session_id):
        for path in (self.config / "jobs").glob("*/state.json"):
            if not JOB.fullmatch(path.parent.name):
                continue
            data = read_json(path)
            if data and session_id in (data.get("resumeSessionId"), data.get("sessionId")):
                return path.parent.name
        return None

    def record_event(self, pane, payload, origin="hook", background=False):
        sid, cwd = payload.get("session_id"), payload.get("cwd")
        if not isinstance(sid, str) or not SESSION.fullmatch(sid) or not valid_path(cwd):
            return None
        job = self.job_for_session(sid)
        if background:
            # A background worker inherits its launcher's Warp UUID. Update only
            # previously verified attachments to this job, never the inherited pane.
            job_env = Path(os.environ.get("CLAUDE_JOB_DIR", "")).name
            if JOB.fullmatch(job_env):
                job = job_env
            for binding in self.bindings():
                if job and binding.get("job_id") == job:
                    self.bind(binding["pane"], sid, cwd, "attach", job, "background-hook")
            return None
        return self.bind(pane, sid, cwd, "attach" if job else "resume", job, origin)

    def maintain(self):
        protected = set()
        live_background = {}
        for path in (self.config / "sessions").glob("*.json"):
            data = read_json(path)
            if (not data or data.get("kind") != "bg"
                    or not isinstance(data.get("pid"), int) or data["pid"] <= 0
                    or not JOB.fullmatch(str(data.get("jobId", "")))
                    or not isinstance(data.get("sessionId"), str)
                    or not SESSION.fullmatch(data["sessionId"]) or not valid_path(data.get("cwd"))):
                continue
            try:
                os.kill(data["pid"], 0)
            except OSError:
                continue
            live_background[(data["jobId"], data["sessionId"])] = data
        for binding in self.bindings():
            try:
                if binding["mode"] == "attach":
                    job = read_json(self.config / "jobs" / binding["job_id"] / "state.json")
                    if job:
                        sid = job.get("resumeSessionId") or job.get("sessionId")
                        # Job state retains the dispatch directory. The live
                        # conversation may have moved to a worktree afterward.
                        live = live_background.get((binding["job_id"], sid))
                        cwd = live["cwd"] if live else binding["cwd"]
                        if isinstance(sid, str) and SESSION.fullmatch(sid) and valid_path(cwd):
                            binding = self.bind(binding["pane"], sid, cwd, "attach", binding["job_id"], binding["origin"])
                self.protect(binding)
                protected.add(binding["session_id"])
            except (OSError, ValueError):
                continue
        # Initial snapshots may protect running conversations before their
        # display-pane mapping is imported. Never invent a pane for these jobs.
        for path in self.archive.glob("*/manifest.json"):
            data = read_json(path)
            if (not data or not isinstance(data.get("session_id"), str)
                    or not SESSION.fullmatch(data["session_id"])
                    or data["session_id"] in protected or not valid_path(data.get("cwd"))):
                continue
            try:
                self.protect(data)
            except (OSError, ValueError):
                continue

    def restore_command(self, pane, binary, flags=(), prepare=True):
        if not Path(binary).is_absolute() or not os.access(binary, os.X_OK):
            raise ValueError("Claude executable must be an executable absolute path")
        binding = read_json(self.metadata_path(pane))
        if binding and binding.get("pane") != pane:
            raise ValueError("Pane metadata belongs to a different pane")
        if not binding or not self.valid_binding(binding):
            # Preserve compatibility with upstream's four-field pane records.
            fields = (self.state / pane).read_text().rstrip("\n").split("\t")
            binding = {"pane": pane, "session_id": fields[0], "cwd": fields[1], "mode": "resume"}
            if not self.valid_binding(binding):
                raise ValueError("Invalid legacy record")
        if not Path(binding["cwd"]).is_dir():
            raise ValueError("The recorded working directory no longer exists")
        if prepare:
            transcript = self.ensure_transcript(binding)
        else:
            transcript = self.find_transcript(binding["session_id"])
            if transcript is None:
                manifest = read_json(self.archive / binding["session_id"] / "manifest.json")
                candidate = self.archive / binding["session_id"] / "transcript.jsonl"
                if (manifest and manifest.get("session_id") == binding["session_id"]
                        and candidate.is_file() and not candidate.is_symlink()):
                    transcript = candidate
        if transcript is None:
            raise ValueError("The transcript is missing and no protected copy exists")
        if (binding["mode"] == "attach"
                and read_json(self.config / "jobs" / binding["job_id"] / "state.json")):
            # The native attach command reuses a live worker and can restart an
            # exited job, preserving its existing launch configuration.
            return binding, [binary, "attach", binding["job_id"]]
        return binding, [binary, *flags, "--resume", binding["session_id"]]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("hook")
    commands.add_parser("maintain")
    plan = commands.add_parser("plan")
    plan.add_argument("--binary", required=True)
    restore = commands.add_parser("restore")
    restore.add_argument("--pane", required=True)
    restore.add_argument("--binary", required=True)
    restore.add_argument("flags", nargs=argparse.REMAINDER)
    record = commands.add_parser("bind")
    record.add_argument("--pane", required=True)
    record.add_argument("--session", required=True)
    record.add_argument("--cwd", required=True)
    record.add_argument("--job")
    args = parser.parse_args()
    store = Store()
    if args.command == "hook":
        raw = sys.stdin.buffer.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            return 0
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return 0
        pane = os.environ.get("WARP_TERMINAL_SESSION_UUID", "")
        if not PANE.fullmatch(pane):
            return 0
        background = bool(os.environ.get("CLAUDE_JOB_DIR") or os.environ.get("CLAUDE_BG_BACKEND")
                          or os.environ.get("CLAUDE_CODE_SESSION_KIND") in ("bg", "background"))
        store.record_event(pane, payload, background=background)
    elif args.command == "maintain":
        store.maintain()
    elif args.command == "plan":
        rows = []
        for candidate in store.bindings():
            binding, command = store.restore_command(candidate["pane"], args.binary, prepare=False)
            rows.append({"pane": binding["pane"], "session_id": binding["session_id"],
                         "cwd": binding["cwd"], "command": command})
        print(json.dumps(rows, indent=2))
    elif args.command == "bind":
        store.bind(args.pane, args.session, args.cwd, "attach" if args.job else "resume", args.job, "verified-import")
    elif args.command == "restore":
        flags = args.flags[1:] if args.flags[:1] == ["--"] else args.flags
        binding, command = store.restore_command(args.pane, args.binary, flags)
        from terminal_proxy import run
        return run(command, store, binding["pane"], binding["cwd"])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, IndexError) as error:
        if len(sys.argv) > 1 and sys.argv[1] == "hook":
            sys.exit(0)
        print("warp-claude: " + str(error), file=sys.stderr)
        sys.exit(1)
