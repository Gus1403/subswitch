# AGENTS.md — set up subswitch

You are an AI coding agent installing subswitch on the user's Mac. This file is
the complete procedure. You have no prior context about this repo, and you don't
need any: follow the phases in order and do not skip gates.

**Read this entire file before running any command.**

---

## What you are building

The user pays for several Claude Code and/or ChatGPT-Codex subscriptions. Right
now they hit a rate limit, stop working, and manually log into another account.
You are installing a daemon that watches every account's usage and switches to
one with headroom before they get blocked.

Four moving parts:

| Part | Role |
|---|---|
| `cswap` (third-party) | swaps which Claude account is active, via the keychain |
| Thin Codex homes | `~/.codex-2`, `~/.codex-3`, … — separate logins, shared sessions |
| `~/.local/bin/codex` shim | launches Codex under the account the daemon selected |
| `subswitchd` (this repo) | polls usage, owns all switching policy, runs under launchd |

---

## Rules you must not break

1. **You cannot do the logins.** Every account needs a human in a browser. When
   you reach a `HUMAN STEP`, stop, print exactly what the user must do, and wait
   for them to confirm. Never fake, skip, or work around one.
2. **Never run bare `codex login` or `codex logout`.** `codex login` writes
   `auth.json` into whatever `CODEX_HOME` is active, and Codex refresh tokens are
   single-use. A wrong login **permanently destroys** another account's session.
   Always pass an explicit `CODEX_HOME=<path>`, or use `bin/codex-relogin.sh`.
   `codex logout` is the only command that explicitly revokes a token — never
   run it.
3. **Never copy `auth.json` between homes.** Single-use tokens; you will break
   both accounts.
4. **Never run `cswap auto`.** It has its own switching loop that fights
   `subswitchd`. The daemon owns policy.
5. **Do not enable enforcement early.** `"enforce": false` until Phase 7's
   verification passes. Enrolling accounts while switching is live causes
   confusing failures.
6. **Do not edit `~/.config/subswitch/state.json`** by hand.
7. **Stop and ask** if any gate fails in a way this document doesn't cover.
   Guessing here costs the user real accounts.

If the user already runs a similar daemon under a different launchd label, tell
them before installing — two switchers will fight.

---

## Phase 1 — Preflight

```sh
bin/preflight.sh
```

Read-only. Every `FAIL` prints its own fix command. Common ones:

| Failure | Fix |
|---|---|
| `cswap not installed` | `uv tool install claude-swap` (uv: https://docs.astral.sh/uv/) |
| `codexbar not installed` | `brew install --cask codexbar` |
| `codex CLI not installed` | `npm i -g @openai/codex` |
| `PATH: ~/.local/bin must precede /opt/homebrew/bin` | add `export PATH="$HOME/.local/bin:$PATH"` to the user's shell profile, then start a new shell |
| `~/.codex does not exist` | HUMAN STEP: the user runs `codex login` once for their **first** account |

**GATE 1 — do not continue until `bin/preflight.sh` exits 0.**

Ask the user now, and write the answers down; you need them in Phases 3 and 4:

- How many **Claude** accounts to enroll?
- How many **Codex/ChatGPT** accounts to enroll?
- Which account should be the **primary** (the one already in `~/.codex` and
  already logged into Claude Code)?

---

## Phase 2 — Install

```sh
./install.sh
```

This installs the `codex` shim, builds the thin Codex homes, renders the
LaunchAgent for this machine, and starts the daemon in **observe-only** mode.

Verify:

```sh
launchctl print "gui/$(id -u)/com.subswitch.daemon" | head -5
grep '"enforce"' ~/.config/subswitch/config.json
```

**GATE 2 — the daemon is loaded AND `"enforce": false`.** If enforce is `true`,
set it to `false` before continuing.

---

## Phase 3 — Enroll Claude accounts

The primary account counts as one. Repeat this for **every** account, including
the primary — a pre-existing snapshot can carry a stale identity label.

For each account, one at a time:

> **HUMAN STEP — Claude account N**
>
> 1. Open a new terminal and run `claude`
> 2. Type `/login`
> 3. Complete the browser login **as the account you want in slot N**
> 4. Type `/exit`
> 5. Tell me when that's done

Then you run:

```sh
cswap add
cswap list --json
```

**GATE 3 (per account) — in `cswap list --json`, confirm all of:**

- a new entry exists with the expected `email`
- its `organizationUuid` is **different from every other enrolled account's**
  (two entries sharing one UUID means the same subscription was added twice —
  tell the user, and have them redo the login with the correct account)
- `usageStatus` is `ok` and the usage percentages look plausible

Only after the gate passes, move to the next account.

When all Claude accounts are enrolled:

```sh
cswap switch 1 --json
cswap list --json
```

**GATE 3-FINAL — the expected number of accounts, all `usageStatus: ok`, all
distinct `organizationUuid`s.**

---

## Phase 4 — Enroll Codex accounts

`~/.codex` is the primary and is already logged in. `install.sh` created the
thin homes. For the **second and each subsequent** account:

> **HUMAN STEP — Codex account N**
>
> I'm going to run a login pinned to `~/.codex-N`. A browser will open.
> Sign in **as the account you want in that home** — not your primary.
> Tell me when the browser flow is finished.

Then you run — note the explicit `CODEX_HOME`, which is what makes this safe:

```sh
CODEX_HOME="$HOME/.codex-2" codex login
```

Verify that home landed the right, distinct account:

```sh
CODEX_HOME="$HOME/.codex-2" codexbar usage --provider codex --format json
```

**GATE 4 (per home) — confirm all of:**

- the reported account email is the one the user intended
- it is **different** from every other home's account
- usage numbers are present (not an auth error)

If two homes report the same account, a login went to the wrong home. Do **not**
copy files to fix it. Re-run the login for the affected home with the correct
`CODEX_HOME` (or use `bin/codex-relogin.sh <home>`, which refuses unsafe cases).

Repeat for `~/.codex-3`, and so on.

If the user wants more homes than exist, create them first:

```sh
SUBSWITCH_HOMES="$HOME/.codex-2 $HOME/.codex-3 $HOME/.codex-4" bin/make-codex-homes.sh
```

Then list every home in `~/.config/subswitch/config.json` under `codex.homes`.

---

## Phase 5 — Visibility (CodexBar)

> **HUMAN STEP — CodexBar**
>
> 1. Open CodexBar → Settings → Providers
> 2. Under **Claude**: enable claude-swap and set the executable path to
>    `~/.local/bin/cswap` — each Claude account should appear as its own card
> 3. Under **Codex**: confirm each account home is listed (add `~/.codex-2`,
>    `~/.codex-3`, … if they aren't auto-detected)
> 4. Tell me when every account is visible

This is how the user sees usage at a glance, and the daemon reads Codex usage
through the same `codexbar` CLI.

**GATE 5 — the user confirms every enrolled account appears.**

---

## Phase 6 — Verify while still observe-only

```sh
bin/doctor.sh --quick
daemon/subswitchd.py --once --dry-run
tail -30 ~/.config/subswitch/logs/subswitchd.log
```

**GATE 6 — confirm all of:**

- `doctor.sh --quick` reports no `FAIL` (every failure prints its own fix)
- the dry-run tick reads usage for **every** enrolled account — no account is
  `unknown`. An account stuck unknown means the daemon can't see it, and it will
  never be selected. Fix that before arming.
- decisions in the log are marked `DRY-RUN`

If `doctor.sh` reports a computer-use failure, that's optional functionality and
does not block switching. Everything else should be clean.

---

## Phase 7 — Arm it

Only now. Stop the daemon, flip the flag, restart:

```sh
launchctl bootout "gui/$(id -u)/com.subswitch.daemon" 2>/dev/null || true
/usr/bin/python3 - <<'PY'
import json, pathlib
p = pathlib.Path.home() / ".config/subswitch/config.json"
cfg = json.loads(p.read_text())
cfg["enforce"] = True
p.write_text(json.dumps(cfg, indent=2) + "\n")
print("enforce =", cfg["enforce"])
PY
/usr/bin/python3 -m json.tool ~/.config/subswitch/config.json >/dev/null && echo "config valid"
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.subswitch.daemon.plist
launchctl kickstart "gui/$(id -u)/com.subswitch.daemon"
```

Now prove a real switch works, with the user watching:

```sh
cswap list --json          # note the active account
cswap switch 2 --json      # move to slot 2
cswap list --json          # confirm active changed
cswap switch 1 --json      # and back
cswap list --json          # confirm it returned
```

**GATE 7 — the active account changed and came back, and `cswap list --json`
still shows every account healthy after the round trip.**

Confirm the Codex pointer mechanism too:

```sh
cat ~/.config/subswitch/codex-current   # absent is fine: means default home
```

---

## Done — tell the user

Report back, concretely:

- how many Claude accounts and Codex homes are enrolled, and that each is a
  distinct account
- that enforcement is **on** and the switch round-trip passed
- where to look: CodexBar for usage, `bin/doctor.sh --quick` for health,
  `~/.config/subswitch/logs/subswitchd.log` for decisions
- the two rules they must personally remember: **never run a bare `codex login`
  or `codex logout`**, and use `bin/codex-app-switch.sh <home>` if they open the
  ChatGPT desktop app — otherwise it silently overwrites a login
- that running Codex processes keep their account until they exit; only new
  launches pick up a rotation

---

## Troubleshooting

| Symptom | Cause | Action |
|---|---|---|
| An account shows `unknown` usage forever | daemon can't read it | Check it's logged in; for Codex run `CODEX_HOME=<home> codexbar usage --provider codex --format json` |
| Two homes report the same account | a login went to the wrong home | Re-login the affected home with explicit `CODEX_HOME`. Never copy `auth.json` |
| `cswap switch` reports a lock timeout | Claude Code is refreshing credentials | Harmless; the daemon retries next tick |
| `codex` runs the wrong account | shim not on PATH first | `bin/preflight.sh`, fix PATH ordering, open a new shell |
| Daemon isn't running | launchd `KeepAlive` restarts it | `launchctl kickstart "gui/$(id -u)/com.subswitch.daemon"`; check `~/.config/subswitch/logs/launchd.err.log` |
| Switching does nothing | still observe-only | `grep '"enforce"' ~/.config/subswitch/config.json` |
| CodexBar says "usage fetch failed" | CodexBar's own fetch | Cosmetic; the daemon has its own path. Confirm with a dry-run tick |

Deeper operational detail: [`docs/runbook.md`](docs/runbook.md).
Destructive-command rules: [`docs/danger-commands.md`](docs/danger-commands.md).
The human-readable version of this procedure: [`docs/ceremony.md`](docs/ceremony.md).

---

## If you are modifying this repo, not installing it

- Run `tests/run-all.sh` — all tests are hermetic and must stay that way. A test
  that touches a real account home, `~/.config/subswitch`, or launchd is a bug.
- Run `bin/audit-privacy.sh --history` before any commit. Never commit a
  rendered `.plist`, an absolute home path, a real email address, or any runtime
  state.
- The daemon must keep running on macOS system Python (3.9) with **no**
  third-party dependencies.
- Keep policy decisions in `daemon/policy.py` pure and unit-tested.
