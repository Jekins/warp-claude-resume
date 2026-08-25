# ---------------------------------------------------------------------------
# Warp + Claude Code: reattach a restored pane to its own Claude session.
#
# Source this from ~/.zshrc:
#     WARP_CLAUDE_RESUME_FLAGS=(--permission-mode bypassPermissions)   # optional
#     source ~/.warp-claude/warp-claude.zsh
#
# How it tells a restored pane from a new one:
#   - Warp exports $WARP_TERMINAL_SESSION_UUID into every local pane.
#   - A RESTORED pane comes back with the UUID it had before the restart, so a
#     record written by the SessionStart hook already exists for it.
#   - A BRAND NEW pane gets a freshly generated random UUID, so no record
#     exists and this does nothing at all.
#
# Nothing runs during .zshrc itself. The check is deferred to a one-shot
# precmd hook, so a prompt framework (powerlevel10k's instant prompt in
# particular, which redirects stdio during init) finishes first, and a Ctrl-C
# at the prompt can't abort the rest of your .zshrc.
#
# Knobs (set BEFORE the source line):
#   WARP_CLAUDE_RESUME_FLAGS
#         zsh ARRAY of flags always passed on resume. Mirror your own launch
#         alias here, e.g.
#             WARP_CLAUDE_RESUME_FLAGS=(--permission-mode bypassPermissions)
#         Must be an array. A plain string is rejected with a warning.
#   WARP_CLAUDE_MAX_AGE   seconds; ignore records older than this. Default 7d.
#   WARP_CLAUDE_AUTO      1 = resume without asking. Default 0 (prompt).
#   WARP_CLAUDE_DISABLE   1 = turn the whole thing off.
# ---------------------------------------------------------------------------

# Exported so the SessionStart hook writes where this reads. Without the
# export the two halves can silently disagree.
# Quoted: under GLOB_SUBST (set shell-wide by `emulate sh`) an unquoted
# default containing [ would be globbed and abort the source.
: "${WARP_CLAUDE_STATE_DIR:=$HOME/.warp-claude/panes}"
export WARP_CLAUDE_STATE_DIR
: ${WARP_CLAUDE_MAX_AGE:=604800}
: ${WARP_CLAUDE_AUTO:=0}
: ${WARP_CLAUDE_DISABLE:=0}

# Accept an array; reject a scalar loudly rather than silently discarding it,
# which is what `typeset -ga` would do to WARP_CLAUDE_RESUME_FLAGS="...".
if [[ ${(t)WARP_CLAUDE_RESUME_FLAGS-} == array* ]]; then
  : # good
elif [[ -n ${WARP_CLAUDE_RESUME_FLAGS-} ]]; then
  print -u2 -- "warp-claude: WARP_CLAUDE_RESUME_FLAGS must be an array, e.g."
  print -u2 -- "             WARP_CLAUDE_RESUME_FLAGS=(--permission-mode bypassPermissions)"
  print -u2 -- "             ignoring the string value you set."
  typeset -ga WARP_CLAUDE_RESUME_FLAGS=()
else
  typeset -ga WARP_CLAUDE_RESUME_FLAGS=()
fi

# Manually reattach the current pane, ignoring the age limit. Handy if you
# said "no" at the prompt and changed your mind.
warp-claude-resume() { _warp_claude_reattach force; }

# Show what's recorded for every pane.
warp-claude-list() {
  emulate -L zsh
  setopt localoptions no_nomatch
  local f sid cwd ts mode found=0
  [[ -d $WARP_CLAUDE_STATE_DIR ]] || { print -- "no records"; return 0; }
  for f in $WARP_CLAUDE_STATE_DIR/*; do
    [[ -f $f ]] || continue
    found=1
    IFS=$'\t' read -r sid cwd ts mode < "$f"
    # (V): show control characters visibly. A tampered record must not be able
    # to put terminal escapes on screen via a diagnostic command.
    printf '%s  %-40s %s\n' "${${f:t}:0:8}" "${(V)sid}" "${(V)cwd}"
  done
  (( found )) || print -- "no records"
}

# Forget this pane's binding, so it stops offering to resume.
warp-claude-forget() {
  emulate -L zsh
  local pane=${WARP_TERMINAL_SESSION_UUID:-}
  [[ -n $pane && -z ${pane//[0-9a-fA-F]/} ]] || { print -- "not a Warp pane"; return 1; }
  rm -f -- "$WARP_CLAUDE_STATE_DIR/$pane" && print -- "forgotten"
}

_warp_claude_reattach() {
  # Local option scope: correct under nounset, ksh_arrays, sh_word_split etc.
  emulate -L zsh
  zmodload -F zsh/datetime +p:EPOCHSECONDS 2>/dev/null
  local force=${1:-}

  # Guard against a nested shell inside a resumed session (exported), and
  # against this file being sourced twice in one shell (shell-scoped).
  [[ -z ${WARP_CLAUDE_ACTIVE:-} ]] || return 0

  local pane=${WARP_TERMINAL_SESSION_UUID:-}
  [[ -n $pane && -z ${pane//[0-9a-fA-F]/} && ${#pane} -le 64 ]] || return 0

  local rec="$WARP_CLAUDE_STATE_DIR/$pane"
  # -r as well as -f: an unreadable record must not print an error on every
  # shell start. Silence is the contract for anything we can't use.
  [[ -f $rec && -r $rec ]] || return 0

  local sid cwd ts mode
  IFS=$'\t' read -r sid cwd ts mode < $rec || return 0

  # The record is local, but it is still input: $sid reaches a command line
  # and $cwd reaches your terminal and `cd`.
  [[ -n $sid && -z ${sid//[0-9a-zA-Z_-]/} && ${#sid} -le 64 && $sid != -* ]] || return 0
  [[ $cwd == /* && $cwd != *[[:cntrl:]]* ]] || return 0
  # <-> alone accepts any digit run; zsh arithmetic warns past 19 digits.
  [[ $ts == <-> && ${#ts} -le 12 ]] || return 0

  local now=$EPOCHSECONDS
  [[ -n $now ]] || now=$(date +%s)
  if [[ $force != force ]]; then
    (( now - ts < WARP_CLAUDE_MAX_AGE )) || return 0
  fi

  # Flags to pass on resume: your standing choice, verbatim.
  local -a extra_args
  extra_args=("${WARP_CLAUDE_RESUME_FLAGS[@]}")

  # What permission mode we'll actually land in, for display. Handles both
  # `--permission-mode x` and `--permission-mode=x`.
  local shown_mode="" i
  i=${extra_args[(I)--permission-mode]}
  if (( i )); then
    shown_mode="${extra_args[i+1]-}"
  else
    i=${extra_args[(I)--permission-mode=*]}
    (( i )) && shown_mode="${${extra_args[i]-}#--permission-mode=}"
  fi

  [[ -d $cwd ]] || {
    print -Pn "%F{3}warp-claude:%f recorded directory is gone: "
    print -r -- "$cwd"
    return 0
  }

  if [[ $WARP_CLAUDE_AUTO != 1 ]]; then
    # No controlling terminal means no way to ask. Fail closed, but say so --
    # a silent no-op here is indistinguishable from "not installed".
    if [[ ! -t 0 || ! -t 1 ]]; then
      print -u2 -- "warp-claude: no terminal to prompt on; run 'warp-claude-resume' to reattach."
      return 0
    fi

    # Display the path with $HOME abbreviated only on a component boundary,
    # so /Users/bobby does not render as ~by.
    local disp=$cwd
    if [[ $cwd == $HOME || $cwd == $HOME/* ]]; then disp="~${cwd#$HOME}"; fi

    # Colour codes go through prompt expansion; the DATA does not -- print -P
    # on an untrusted path would expand %-escapes and, under PROMPT_SUBST,
    # execute $(...) inside it.
    # $sid goes through -P; that is safe only because of the charset check
    # above (no %, no $, no backslash can be in it). $disp never goes through -P.
    print -Pn "%F{6}claude%f %F{3}${sid[1,8]}%f  %F{8}"
    print -rn -- "$disp"
    print -Pn "%f"
    if [[ -n $shown_mode ]]; then
      print -Pn "  %F{8}"
      print -rn -- "$shown_mode"
      print -Pn "%f"
    fi
    print

    local ans
    printf 'resume? [Y/n] '
    read -r ans || { print; return 0; }
    [[ $ans == [nN]* ]] && return 0
  fi

  # Resolve the binary BEFORE changing directory, to an absolute path, and
  # call that path: a bare `claude` here would be (a) alias-expanded at source
  # time, silently baking in flags the consent prompt never showed, and
  # (b) PATH-resolved after the cd, so with `.` in PATH a record could steer
  # the shell into a directory containing its own `claude`. Same reason for
  # `builtin cd`: a cd alias/function (zoxide etc.) must not intercept this.
  # $commands ignores relative/empty PATH entries, so the /* check is a second
  # layer, not the only one. Claude Code's local installer may define claude
  # only as `alias claude=/abs/path`; honour exactly that shape (an absolute
  # path with no spaces, i.e. no flags), nothing else. -x: a stale hash entry
  # must fail silently, not print "no such file" on every restore.
  local bin=${commands[claude]:-}
  if [[ $bin != /* ]]; then
    bin=${aliases[claude]:-}
    [[ $bin == /* && $bin != *[[:space:]]* ]] || bin=
  fi
  [[ -n $bin && -x $bin ]] || {
    print -u2 -- "warp-claude: claude not found in PATH"
    return 0
  }

  # Only move the shell once you've agreed to resume.
  builtin cd -- "$cwd" || {
    print -Pn "%F{1}warp-claude:%f could not enter "
    print -r -- "$cwd"
    return 0
  }

  local rc=0
  WARP_CLAUDE_ACTIVE=1 "$bin" "${extra_args[@]}" --resume "$sid" || rc=$?
  # 130 is Ctrl-C, 1 is often a normal quit. Only a clear failure to start is
  # worth commenting on, and even then don't assert why.
  if (( rc != 0 && rc != 130 && rc != 1 )); then
    print -Pn "%F{1}warp-claude:%f claude exited ${rc} resuming ${sid[1,8]}."
    print
    print -P "%F{8}run 'claude' for a fresh session, or 'warp-claude-forget' to stop offering.%f"
  fi
}

# Defer the check to just before the first prompt, then unregister. Running
# during .zshrc breaks powerlevel10k's instant prompt (stdio is redirected
# there, so the tty test fails and this would silently never fire) and would
# let a Ctrl-C at the prompt abort the rest of your .zshrc.
_warp_claude_precmd() {
  add-zsh-hook -d precmd _warp_claude_precmd 2>/dev/null
  unfunction _warp_claude_precmd 2>/dev/null
  _warp_claude_reattach
}

if [[ $WARP_CLAUDE_DISABLE != 1 && -o interactive \
      && -n ${WARP_TERMINAL_SESSION_UUID:-} && -z ${WARP_CLAUDE_ACTIVE:-} \
      && -z ${WARP_CLAUDE_RAN:-} ]]; then
  typeset -g WARP_CLAUDE_RAN=1          # shell-scoped: survives a second source
  autoload -Uz add-zsh-hook
  add-zsh-hook precmd _warp_claude_precmd
fi
