#!/usr/bin/env bash
# Install, update or remove warp-claude-resume (persistent-session fork).
#
#   curl -fsSL https://raw.githubusercontent.com/Jekins/warp-claude-resume/persistent-open-sessions/install.sh | bash
#   curl -fsSL .../install.sh | bash -s -- --auto        # resume without asking
#   bash install.sh [--auto] [--test] [--uninstall]      # from a clone
#
# Safe to re-run: an unchanged install changes nothing. Every file it replaces
# is copied to ~/.warp-claude/backups/<timestamp>/ first.
set -euo pipefail

REPO="${WARP_CLAUDE_REPO:-https://github.com/Jekins/warp-claude-resume.git}"
REF="${WARP_CLAUDE_REF:-persistent-open-sessions}"
FILES="record-session.sh warp-claude.zsh session_store.py terminal_proxy.py"
LABEL="dev.warp-claude.resume-maintenance"
# shellcheck disable=SC2016  # $HOME is expanded by the hook's shell, not here
HOOK_COMMAND='"$HOME/.warp-claude/record-session.sh"'
BEGIN_MARK="# BEGIN warp-claude-resume (persistent-session fork)"
END_MARK="# END warp-claude-resume"

say()  { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: install.sh [--auto] [--test] [--uninstall]
  --auto       resume a restored pane's conversation without asking
  --test       run the test suites before installing
  --uninstall  remove the hook, shell integration and maintenance agent
               (keeps ~/.warp-claude with the archived conversations)
EOF
  exit "${1:-0}"
}

backup() {
  [ -e "$1" ] || return 0
  mkdir -p "$BACKUP"
  cp -p "$1" "$BACKUP/$(basename "$1")"
}

# settings.json is read and written only here, by python3, so other keys and
# hooks survive. Prints "present"/"absent" for check; exits 2 on invalid JSON.
settings() {
  python3 - "$SETTINGS" "$1" "$HOOK_COMMAND" <<'PY'
import json, os, sys, tempfile
path, action, command = sys.argv[1:4]
mark = "/.warp-claude/record-session.sh"
real = os.path.realpath(path)

def fail(message):
    print("error: %s: %s" % (path, message), file=sys.stderr)
    sys.exit(2)

try:
    with open(real, encoding="utf-8") as handle:
        text = handle.read()
except FileNotFoundError:
    text = None
try:
    data = json.loads(text) if text and text.strip() else {}
except ValueError as error:
    fail("not valid JSON (%s); fix it and re-run" % error)
if not isinstance(data, dict):
    fail("top level is not a JSON object")
hooks = data.get("hooks", {})
if not isinstance(hooks, dict):
    fail('"hooks" is not an object')
groups = hooks.get("SessionStart", [])
if not isinstance(groups, list) or not all(
        isinstance(g, dict) and isinstance(g.get("hooks", []), list) for g in groups):
    fail('"hooks.SessionStart" is not a list of hook groups')

def ours(hook):
    return isinstance(hook, dict) and mark in str(hook.get("command", ""))

present = any(ours(h) for g in groups for h in g.get("hooks", []))
if action == "check":
    print("present" if present else "absent")
    sys.exit(0)
if action == "add":
    if present:
        sys.exit(0)
    groups.append({"hooks": [{"type": "command", "command": command, "timeout": 10}]})
    hooks["SessionStart"] = groups
    data["hooks"] = hooks
elif action == "remove":
    if not present:
        sys.exit(0)
    kept = []
    for group in groups:
        rest = [h for h in group.get("hooks", []) if not ours(h)]
        if rest or not any(ours(h) for h in group.get("hooks", [])):
            kept.append(dict(group, hooks=rest))
    if kept:
        hooks["SessionStart"] = kept
    else:
        hooks.pop("SessionStart", None)
    if not hooks:
        data.pop("hooks", None)

directory = os.path.dirname(real)
os.makedirs(directory, exist_ok=True)
mode = os.stat(real).st_mode & 0o777 if text is not None else 0o600
fd, temporary = tempfile.mkstemp(prefix=".settings-", dir=directory)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), mode)
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(temporary, real)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
}

write_plist() {
  python3 - "$1" "$LABEL" "$PYTHON" "$DEST" "$CONFIG" "$STATE" <<'PY'
import plistlib, sys
out, label, python, dest, config, state = sys.argv[1:7]
log = dest + "/maintenance.log"
with open(out, "wb") as handle:
    plistlib.dump({
        "Label": label,
        "ProgramArguments": [python, dest + "/session_store.py", "maintain"],
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                                 "CLAUDE_CONFIG_DIR": config,
                                 "WARP_CLAUDE_STATE_DIR": state},
        "StartInterval": 60,
        "RunAtLoad": True,
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }, handle)
PY
}

sources_integration() {
  [ -f "$ZSHRC" ] && grep -Eq '^[[:space:]]*(source|\.)[[:space:]].*warp-claude/warp-claude\.zsh' "$ZSHRC"
}

# ------------------------------------------------------------------ install
install_files() {
  mkdir -p "$DEST"
  chmod 700 "$DEST"
  local f mode
  for f in $FILES; do
    if cmp -s "$SRC/$f" "$DEST/$f"; then
      continue
    fi
    backup "$DEST/$f"
    mode=600
    [ "$f" = record-session.sh ] && mode=700
    cp "$SRC/$f" "$DEST/.$f.new"
    chmod "$mode" "$DEST/.$f.new"
    mv -f "$DEST/.$f.new" "$DEST/$f"
    CHANGED="$CHANGED
  - $DEST/$f"
  done

  local record="$DEST/installation.json" new="$DEST/.installation.json.new"
  python3 -c 'import json, sys
print(json.dumps(dict(zip(("repository", "ref", "commit"), sys.argv[1:])), indent=2))' \
    "$REPO" "$REF" "$COMMIT" > "$new"
  chmod 600 "$new"
  if cmp -s "$new" "$record"; then
    rm -f "$new"
  else
    backup "$record"
    mv -f "$new" "$record"
  fi
}

install_hook() {
  if [ "$(settings check)" = present ]; then
    return 0
  fi
  backup "$SETTINGS"
  settings add
  CHANGED="$CHANGED
  - $SETTINGS: SessionStart hook added"
}

install_zshrc() {
  if sources_integration; then
    if [ "$AUTO" = 1 ] && ! grep -Eq '^[[:space:]]*WARP_CLAUDE_AUTO=1' "$ZSHRC"; then
      warn "$ZSHRC already sources warp-claude.zsh; add WARP_CLAUDE_AUTO=1 above that line yourself"
    fi
    return 0
  fi
  backup "$ZSHRC"
  if [ -s "$ZSHRC" ] && [ -n "$(tail -c1 "$ZSHRC")" ]; then
    printf '\n' >> "$ZSHRC"
  fi
  {
    printf '\n%s\n' "$BEGIN_MARK"
    printf '%s\n' "# Reopen each restored Warp pane's own Claude Code conversation."
    [ "$AUTO" = 1 ] && printf '%s\n' "WARP_CLAUDE_AUTO=1"
    # shellcheck disable=SC2016  # expanded by zsh at startup
    printf '%s\n' 'source "$HOME/.warp-claude/warp-claude.zsh"'
    printf '%s\n' "$END_MARK"
  } >> "$ZSHRC"
  CHANGED="$CHANGED
  - $ZSHRC: shell integration added"
}

install_agent() {
  if [ "$(uname -s)" != Darwin ]; then
    NOTES="$NOTES
  - No launchd here. Add this crontab line to keep idle conversations archived:
      * * * * * $PYTHON $DEST/session_store.py maintain"
    return 0
  fi
  local domain new load=0
  domain="gui/$(id -u)"
  mkdir -p "$(dirname "$PLIST")"
  new="$(dirname "$PLIST")/.$LABEL.plist.new"
  write_plist "$new"
  if cmp -s "$new" "$PLIST"; then
    rm -f "$new"
    launchctl print "$domain/$LABEL" >/dev/null 2>&1 || load=1
  else
    backup "$PLIST"
    mv -f "$new" "$PLIST"
    launchctl bootout "$domain/$LABEL" >/dev/null 2>&1 || true
    load=1
    CHANGED="$CHANGED
  - $PLIST: maintenance agent (every 60 s)"
  fi
  if [ "$load" = 1 ]; then
    # A bootstrap right after bootout can fail while launchd finishes the unload.
    launchctl bootstrap "$domain" "$PLIST" >/dev/null 2>&1 \
      || { sleep 1; launchctl bootstrap "$domain" "$PLIST" >/dev/null 2>&1; } \
      || warn "could not load $LABEL now; it loads at next login"
  fi
}

# ---------------------------------------------------------------- uninstall
uninstall() {
  if [ -f "$SETTINGS" ] && [ "$(settings check)" = present ]; then
    backup "$SETTINGS"
    settings remove
    CHANGED="$CHANGED
  - $SETTINGS: SessionStart hook removed"
  fi
  if [ -f "$ZSHRC" ] && grep -qxF "$BEGIN_MARK" "$ZSHRC"; then
    backup "$ZSHRC"
    local kept
    kept="$(awk -v b="$BEGIN_MARK" -v e="$END_MARK" '$0==b{skip=1;next} skip&&$0==e{skip=0;next} !skip' "$ZSHRC")"
    # Rewrite in place: keeps the file's mode and a dotfiles symlink.
    printf '%s\n' "$kept" > "$ZSHRC"
    CHANGED="$CHANGED
  - $ZSHRC: shell integration removed"
  fi
  if sources_integration; then
    warn "$ZSHRC still sources warp-claude.zsh outside the installer's block; remove that line yourself"
  fi
  if [ "$(uname -s)" = Darwin ] && [ -f "$PLIST" ]; then
    launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
    backup "$PLIST"
    rm -f "$PLIST"
    CHANGED="$CHANGED
  - $PLIST: maintenance agent removed"
  fi
  if [ "$(uname -s)" != Darwin ]; then
    NOTES="$NOTES
  - If you added the session_store.py maintain line to your crontab, remove it."
  fi
  NOTES="$NOTES
  - Kept $DEST: it holds the archived conversations. Delete it with: rm -rf ~/.warp-claude"
}

# --------------------------------------------------------------------- main
main() {
  AUTO=0; TEST=0; ACTION=install
  while [ $# -gt 0 ]; do
    case "$1" in
      --auto) AUTO=1 ;;
      --test) TEST=1 ;;
      --uninstall) ACTION=uninstall ;;
      -h|--help) usage 0 ;;
      *) warn "unknown option: $1"; usage 1 ;;
    esac
    shift
  done

  [ -n "${HOME:-}" ] && [ -d "$HOME" ] || die "HOME is not set"
  case "$(uname -s)" in Darwin|Linux) ;; *) die "only macOS and Linux are supported" ;; esac
  PYTHON="$(command -v python3 || true)"
  if [ -z "$PYTHON" ] || ! "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 9))' >/dev/null 2>&1; then
    die "python3 3.9 or newer is required (macOS: xcode-select --install)"
  fi

  DEST="$HOME/.warp-claude"
  CONFIG="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
  SETTINGS="$CONFIG/settings.json"
  STATE="${WARP_CLAUDE_STATE_DIR:-$DEST/panes}"
  ZSHRC="${ZDOTDIR:-$HOME}/.zshrc"
  PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
  BACKUP="$DEST/backups/$(date -u +%Y%m%dT%H%M%SZ)"
  CHANGED=""; NOTES=""
  # Validate before touching anything, so a broken settings.json leaves no half-done change.
  [ ! -f "$SETTINGS" ] || settings check >/dev/null

  if [ "$ACTION" = uninstall ]; then
    uninstall
    say "warp-claude-resume removed.${CHANGED:-
  - nothing was installed}"
    [ -z "$NOTES" ] || say "$NOTES"
    return 0
  fi

  command -v zsh >/dev/null 2>&1 || die "zsh is required (Warp's default shell on macOS)"
  command -v claude >/dev/null 2>&1 || warn "claude is not on PATH; install Claude Code before using this"

  # Piped (curl | bash) there is no script file, and the current directory may
  # be an old clone: download instead of picking up whatever is lying there.
  local here=""
  if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
  fi
  SRC=""
  if [ -n "$here" ] && [ -f "$here/session_store.py" ] && [ -f "$here/warp-claude.zsh" ]; then
    SRC="$here"
  else
    command -v git >/dev/null 2>&1 || die "git is required to download the files"
    TMP="$(mktemp -d)"
    trap 'rm -rf "$TMP"' EXIT
    git clone --quiet --depth 1 --branch "$REF" "$REPO" "$TMP/src" </dev/null \
      || die "could not download $REPO ($REF)"
    SRC="$TMP/src"
  fi
  local f
  for f in $FILES; do
    [ -f "$SRC/$f" ] || die "missing $f in $SRC"
  done
  COMMIT=unknown
  if [ "$(git -C "$SRC" rev-parse --show-toplevel 2>/dev/null)" = "$(cd "$SRC" && pwd -P)" ]; then
    COMMIT="$(git -C "$SRC" rev-parse HEAD)"
    # shellcheck disable=SC2086  # FILES is a word list
    git -C "$SRC" diff --quiet HEAD -- $FILES || COMMIT="$COMMIT-modified"
  fi

  if ! { sh -n "$SRC/record-session.sh" && zsh -n "$SRC/warp-claude.zsh" \
         && python3 -c 'import ast, sys; [ast.parse(open(p).read(), p) for p in sys.argv[1:]]' \
              "$SRC/session_store.py" "$SRC/terminal_proxy.py"; }; then
    die "the downloaded scripts do not parse; nothing was installed"
  fi
  if [ "$TEST" = 1 ]; then
    say "Running the test suites..."
    (cd "$SRC" && bash test.sh >/dev/null && python3 -m unittest discover -s tests >/dev/null 2>&1) \
      || die "tests failed; nothing was installed (run them in a clone to see why)"
  fi

  install_files
  install_hook
  install_zshrc
  install_agent

  if [ -z "$CHANGED" ]; then
    say "warp-claude-resume is already up to date (${COMMIT:0:12})."
  else
    say "warp-claude-resume installed (${COMMIT:0:12}):$CHANGED"
    [ ! -d "$BACKUP" ] || say "  Replaced files were saved to $BACKUP"
  fi
  [ -z "$NOTES" ] || say "$NOTES"
  say "
Next:
  - In Warp, turn on 'Restore windows, tabs, and panes on startup'.
  - Open a new tab. Claude Code sessions started from now on come back
    in their own pane after Warp restarts."
}

main ${1+"$@"}
