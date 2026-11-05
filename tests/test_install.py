"""install.sh in a throwaway HOME, with launchctl and uname stubbed on PATH."""
import json
import os
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install.sh"
FILES = ("record-session.sh", "warp-claude.zsh", "session_store.py", "terminal_proxy.py")
LABEL = "dev.warp-claude.resume-maintenance"
HOOK = '"$HOME/.warp-claude/record-session.sh"'


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.tmp = Path(self.directory.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.log = self.tmp / "launchctl.log"
        self.uname = "Darwin"
        stub = self.bin / "launchctl"
        stub.write_text('#!/bin/sh\necho "$*" >> "$FAKE_LOG"\ncase "$1" in\n'
                        '  print) [ -f "$FAKE_LOADED" ] ;;\n'
                        '  bootstrap) : > "$FAKE_LOADED" ;;\n'
                        '  bootout) rm -f "$FAKE_LOADED" ;;\nesac\n')
        stub.chmod(0o755)
        uname = self.bin / "uname"
        uname.write_text('#!/bin/sh\necho "$FAKE_UNAME"\n')
        uname.chmod(0o755)

    def tearDown(self):
        self.directory.cleanup()

    @property
    def dest(self):
        return self.home / ".warp-claude"

    @property
    def settings(self):
        return self.home / ".claude" / "settings.json"

    @property
    def zshrc(self):
        return self.home / ".zshrc"

    @property
    def plist(self):
        return self.home / "Library" / "LaunchAgents" / (LABEL + ".plist")

    def run_install(self, *args, stdin=None, cwd=None, extra=None):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("WARP_CLAUDE_") and k not in ("CLAUDE_CONFIG_DIR", "ZDOTDIR")}
        env.update(HOME=str(self.home), PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                   FAKE_LOG=str(self.log), FAKE_LOADED=str(self.tmp / "loaded"),
                   FAKE_UNAME=self.uname, **(extra or {}))
        command = ["bash", "-s", "--", *args] if stdin is not None else ["bash", str(INSTALL), *args]
        return subprocess.run(command, input=stdin, cwd=cwd or self.tmp, env=env,
                              capture_output=True, text=True, timeout=120)

    def installed(self, *args, **kwargs):
        result = self.run_install(*args, **kwargs)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def snapshot(self):
        return {str(p.relative_to(self.home)): p.read_bytes()
                for p in sorted(self.home.rglob("*")) if p.is_file()}

    def hook_commands(self):
        data = json.loads(self.settings.read_text())
        return [h["command"] for g in data["hooks"]["SessionStart"] for h in g["hooks"]]

    def launchctl_calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_fresh_install_wires_every_part(self):
        self.installed()
        self.assertEqual(self.dest.stat().st_mode & 0o777, 0o700)
        for name in FILES:
            self.assertEqual((self.dest / name).read_bytes(), (ROOT / name).read_bytes())
            mode = 0o700 if name == "record-session.sh" else 0o600
            self.assertEqual((self.dest / name).stat().st_mode & 0o777, mode, name)
        self.assertEqual(self.hook_commands(), [HOOK])
        self.assertEqual(self.settings.stat().st_mode & 0o777, 0o600)
        rc = self.zshrc.read_text()
        self.assertIn('source "$HOME/.warp-claude/warp-claude.zsh"', rc)
        self.assertNotIn("WARP_CLAUDE_AUTO", rc)
        agent = plistlib.loads(self.plist.read_bytes())
        self.assertEqual(agent["ProgramArguments"][1:], [str(self.dest / "session_store.py"), "maintain"])
        self.assertEqual(agent["StartInterval"], 60)
        self.assertEqual(agent["EnvironmentVariables"]["CLAUDE_CONFIG_DIR"], str(self.home / ".claude"))
        self.assertTrue(any(c.startswith("bootstrap ") for c in self.launchctl_calls()))
        self.assertEqual(json.loads((self.dest / "installation.json").read_text())["ref"],
                         "persistent-open-sessions")

    def test_rerun_changes_nothing(self):
        self.installed()
        before = self.snapshot()
        result = self.installed()
        self.assertIn("already up to date", result.stdout)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.dest / "backups").exists())

    def test_rerun_reloads_an_unloaded_agent(self):
        self.installed()
        (self.tmp / "loaded").unlink()
        self.installed()
        self.assertEqual(sum(c.startswith("bootstrap ") for c in self.launchctl_calls()), 2)

    def test_other_settings_and_hooks_are_kept(self):
        original = {"model": "opus", "hooks": {
            "SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": "echo hi"}]}],
            "Stop": [{"hooks": [{"type": "command", "command": "echo bye"}]}]}}
        self.settings.parent.mkdir()
        self.settings.write_text(json.dumps(original))
        self.settings.chmod(0o644)
        self.installed()
        data = json.loads(self.settings.read_text())
        self.assertEqual(data["model"], "opus")
        self.assertEqual(data["hooks"]["Stop"], original["hooks"]["Stop"])
        self.assertEqual(data["hooks"]["SessionStart"][0], original["hooks"]["SessionStart"][0])
        self.assertEqual(self.hook_commands(), ["echo hi", HOOK])
        self.assertEqual(self.settings.stat().st_mode & 0o777, 0o644)
        backups = list((self.dest / "backups").glob("*/settings.json"))
        self.assertEqual([json.loads(b.read_text()) for b in backups], [original])

    def test_an_existing_upstream_hook_is_not_duplicated(self):
        self.settings.parent.mkdir()
        self.settings.write_text(json.dumps({"hooks": {"SessionStart": [
            {"hooks": [{"type": "command", "command": "$HOME/.warp-claude/record-session.sh"}]}]}}))
        before = self.settings.read_bytes()
        self.installed()
        self.assertEqual(self.settings.read_bytes(), before)

    def test_invalid_settings_abort_before_any_change(self):
        for broken in ("{not json", '{"hooks": []}', '{"hooks": {"SessionStart": {}}}', "[]"):
            with self.subTest(broken=broken):
                self.settings.parent.mkdir(exist_ok=True)
                self.settings.write_text(broken)
                result = self.run_install()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("settings.json", result.stderr)
                self.assertEqual(self.settings.read_text(), broken)
                self.assertFalse(self.dest.exists())
                self.assertFalse(self.zshrc.exists())
                self.assertFalse(self.plist.exists())

    def test_an_existing_source_line_is_not_duplicated(self):
        self.zshrc.write_text("export A=1\nsource ~/.warp-claude/warp-claude.zsh\n")
        self.installed()
        self.assertEqual(self.zshrc.read_text(), "export A=1\nsource ~/.warp-claude/warp-claude.zsh\n")

    def test_zshrc_without_final_newline_keeps_its_last_line(self):
        self.zshrc.write_text("export A=1")
        self.installed()
        self.assertTrue(self.zshrc.read_text().startswith("export A=1\n\n# BEGIN warp-claude-resume"))

    def test_dotfile_symlinks_stay_symlinks(self):
        dotfiles = self.tmp / "dotfiles"
        dotfiles.mkdir()
        (dotfiles / "zshrc").write_text("export A=1\n")
        (dotfiles / "settings.json").write_text('{"model": "opus"}\n')
        self.zshrc.symlink_to(dotfiles / "zshrc")
        self.settings.parent.mkdir()
        self.settings.symlink_to(dotfiles / "settings.json")
        self.installed()
        self.assertTrue(self.zshrc.is_symlink())
        self.assertTrue(self.settings.is_symlink())
        self.assertIn("warp-claude.zsh", (dotfiles / "zshrc").read_text())
        self.assertIn("record-session.sh", (dotfiles / "settings.json").read_text())

    def test_auto_flag_resumes_without_asking(self):
        self.installed("--auto")
        self.assertIn("\nWARP_CLAUDE_AUTO=1\nsource ", self.zshrc.read_text())

    def test_uninstall_restores_the_original_config(self):
        self.settings.parent.mkdir()
        original = {"model": "opus", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x"}]}]}}
        self.settings.write_text(json.dumps(original))
        self.zshrc.write_text("export A=1\n")
        self.installed()
        result = self.installed("--uninstall")
        self.assertEqual(json.loads(self.settings.read_text()), original)
        self.assertEqual(self.zshrc.read_text(), "export A=1\n")
        self.assertFalse(self.plist.exists())
        self.assertTrue(any(c.startswith("bootout ") for c in self.launchctl_calls()))
        self.assertTrue((self.dest / "session_store.py").exists())
        self.assertIn("rm -rf ~/.warp-claude", result.stdout)

    def test_uninstall_drops_an_emptied_hooks_key(self):
        self.installed()
        self.installed("--uninstall")
        self.assertEqual(json.loads(self.settings.read_text()), {})

    def test_home_with_spaces(self):
        self.home = self.tmp / "my home"
        self.home.mkdir()
        self.installed()
        agent = plistlib.loads(self.plist.read_bytes())
        self.assertEqual(agent["ProgramArguments"][1], str(self.home / ".warp-claude" / "session_store.py"))
        self.assertEqual(self.hook_commands(), [HOOK])

    def test_linux_skips_launchd_and_prints_a_crontab_line(self):
        self.uname = "Linux"
        result = self.installed()
        self.assertFalse(self.plist.exists())
        self.assertEqual(self.launchctl_calls(), [])
        self.assertIn("session_store.py maintain", result.stdout)

    def test_changed_installed_file_is_replaced_and_backed_up(self):
        self.installed()
        target = self.dest / "session_store.py"
        target.write_text("old version\n")
        self.installed()
        self.assertEqual(target.read_bytes(), (ROOT / "session_store.py").read_bytes())
        self.assertEqual([p.read_text() for p in (self.dest / "backups").glob("*/session_store.py")],
                         ["old version\n"])

    def test_piped_install_downloads_instead_of_using_the_current_directory(self):
        remote = self.tmp / "remote"
        remote.mkdir()
        for name in FILES:
            shutil.copy2(ROOT / name, remote / name)
        with open(remote / "warp-claude.zsh", "a") as handle:
            handle.write("# downloaded copy\n")

        def git(*args):
            subprocess.run(["git", "-C", str(remote), "-c", "user.name=t", "-c", "user.email=t@t",
                            *args], check=True, capture_output=True)
        git("init", "-q", "-b", "persistent-open-sessions")
        git("add", ".")
        git("commit", "-q", "-m", "remote")
        head = subprocess.run(["git", "-C", str(remote), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
        self.installed(stdin=INSTALL.read_text(), cwd=ROOT,
                       extra={"WARP_CLAUDE_REPO": "file://" + str(remote)})
        self.assertTrue((self.dest / "warp-claude.zsh").read_text().endswith("# downloaded copy\n"))
        self.assertEqual(json.loads((self.dest / "installation.json").read_text())["commit"], head)


if __name__ == "__main__":
    unittest.main()
