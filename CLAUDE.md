# warp-claude-resume

Per-pane Claude Code session resume for Warp. Two shell scripts; no build step.

Several decisions in the scripts look simplifiable and aren't — reverting them
reintroduces bugs that were found by executing the code, not by reading it.
Each one has a comment in the source explaining why, and usually a regression
test. Read the comment before changing the code.

## Layout

| File | Role |
|---|---|
| `record-session.sh` | Claude Code `SessionStart` hook. POSIX sh. Writes a pane→session record. |
| `warp-claude.zsh` | Sourced from `.zshrc`. Registers a one-shot `precmd` hook that offers to resume. |
| `test.sh` | Regression suite. 61 checks. Run after every edit. |
| `README.md` | User-facing docs |
| `LICENSE` | MIT |

## Working on this

```sh
bash test.sh        # must be 61/61 before and after any change
```

The suite runs in a temp directory with a stubbed `claude` and touches nothing
in `$HOME`. Several tests exist because of a specific bug that was live at some
point — if one starts failing, read the comment above it before "fixing" the test.

## Non-obvious constraints

- `record-session.sh` must stay **POSIX sh** — Claude Code runs hook commands
  through `sh -c`, and `/bin/sh` is dash on many systems. `set -o pipefail` is
  not available. No `[[`, no `local`, no arrays.
- `warp-claude.zsh` runs inside the user's interactive shell. It must be silent
  and side-effect-free unless a record for the current pane exists.
- Untrusted data (`cwd` from the hook payload, anything in a record file) must
  never reach `print -P`, `eval`, or an unquoted command line.

## Status

Verified on macOS (Aug 24 2026): 43/43 on zsh 5.9 / Darwin 25, and a real
end-to-end Warp restart resumed the right session, with both prompt and
`WARP_CLAUDE_AUTO=1` modes. No open items.
