# subswitch

**Stop babysitting rate limits across your own Claude and Codex subscriptions.**

If you pay for more than one Claude Code or ChatGPT/Codex subscription, you know
the loop: you hit a limit mid-task, you stop, you log into another account, you
lose your place. subswitch removes that loop. A local daemon watches every
account's usage and, before you run out, moves you to one with headroom —
through the official CLIs, on your own machine.

```
   Claude accounts                     Codex accounts
   ┌────┬────┬────┐                    ┌────────┬──────────┬──────────┐
   │ #1 │ #2 │ #3 │  ...               │ ~/.codex│ ~/.codex-2│ ~/.codex-3│ ...
   └──┬─┴────┴────┘                    └────┬───┴──────────┴──────────┘
      │ cswap (keychain swap)               │ CODEX_HOME pointer + shim
      └──────────────┬──────────────────────┘
                     │
              ┌──────▼───────┐   polls usage every 60s, switches at 95%,
              │  subswitchd  │   rides the best account when all are red,
              │  (launchd)   │   spends weekly quota that is about to reset
              └──────┬───────┘
                     │
                 CodexBar  ← see every account's usage in the menu bar
```

## What it does

- **Switches before you're blocked.** 95% on a 5-hour or weekly window moves you
  to the account with the most headroom, with hysteresis and cooldowns so it
  never flaps.
- **Understands per-model limits.** A model-scoped weekly window being exhausted
  doesn't make an account useless — subswitch knows the difference between
  "this account serves nothing" and "this account can't serve *that* model".
- **Never wastes quota.** If a healthy account's weekly window resets in under
  24 hours, it proactively spends it instead of letting it expire.
- **Keeps working when everything is red.** It rides the account with the most
  raw headroom and rotates again at 99% rather than stranding you.
- **Resumes interrupted work.** When a Codex session in tmux or cmux hits its
  limit, it can rotate the account and `codex resume` that exact session.
- **Refuses to guess.** Usage it can't read, or that's more than 15 minutes
  stale, is treated as unknown — never as spare capacity.

## Scope, honestly

This coordinates **subscriptions you personally pay for**, on **one machine**,
for **one person**. It does not pool credentials, does not proxy or relay API
traffic, does not share accounts between people, and does not bypass any limit —
each account's limits apply to that account exactly as the provider sets them.
It automates the account picking you'd otherwise do by hand.

Please don't use it to farm free trials. That's not what it's for and it'll get
your accounts banned.

## Requirements

macOS (it uses launchd and the macOS keychain). Plus:

| | Why | Install |
|---|---|---|
| [Codex CLI](https://github.com/openai/codex) | the Codex side | `npm i -g @openai/codex` |
| [Claude Code](https://claude.com/claude-code) | the Claude side | see docs |
| [claude-swap](https://github.com/realiti4/claude-swap) (`cswap`) | swaps the active Claude account | `uv tool install claude-swap` |
| [CodexBar](https://codexbar.app/) | reads Codex usage; menu-bar view | `brew install --cask codexbar` |

The daemon runs on macOS's system `/usr/bin/python3` with no third-party
Python dependencies.

## Install

**Point your coding agent at this repo.** [`AGENTS.md`](AGENTS.md) is a complete,
ordered setup procedure written for an agent with no prior context. It runs the
mechanical steps, stops and asks you to do the browser logins (which nothing can
automate), and verifies every step before continuing.

> Set up subswitch on my machine by following AGENTS.md in this repo.

Prefer to do it yourself:

```sh
git clone https://github.com/Gus1403/subswitch.git
cd subswitch
bin/preflight.sh     # checks prerequisites, changes nothing
./install.sh         # shim + thin homes + LaunchAgent (observe-only)
```

Then follow [`docs/ceremony.md`](docs/ceremony.md) to enroll your accounts and
turn switching on.

**subswitch starts in observe-only mode** (`"enforce": false`). It watches,
decides, and logs, but changes nothing until you deliberately enable it after
verification.

## How it works

**Claude** — `cswap` stores each account's OAuth credential in the keychain and
swaps which one is active. Running Claude sessions pick up the swap within about
30 seconds, mid-conversation.

**Codex** — Codex reads one `CODEX_HOME`. `bin/make-codex-homes.sh` builds thin
homes (`~/.codex-2`, `~/.codex-3`, …) that symlink shared state — sessions,
config, skills, memories — while keeping `auth.json` and other per-account files
private to each home. A shim at `~/.local/bin/codex` reads
`~/.config/subswitch/codex-current` and launches Codex under the selected home.
Because sessions are shared, a session started on one account can be resumed on
another.

Codex rotation is **process-boundary only** — a running Codex process keeps the
account it started with. That's deliberate: Codex caches auth in memory and
rewrites `auth.json` without locking, so hot-swapping underneath a live process
corrupts it.

**Everything else** — `daemon/subswitchd.py` owns all policy. `daemon/policy.py`
is pure and independently testable. Operational files live in
`~/.config/subswitch/`: `config.json`, `state.json`, `codex-current`, `logs/`.

## Read this before you break something

Codex refresh tokens are **single-use**, and `codex login` writes `auth.json`
into whatever `CODEX_HOME` is active. So a plain `codex login` — or just opening
the ChatGPT desktop app — can silently overwrite one account's login with
another's, and it is **not recoverable** without logging in again.

[`docs/danger-commands.md`](docs/danger-commands.md) is short and mandatory. The
shim intercepts the two commands that cause this and makes you confirm. Use
`bin/codex-relogin.sh <home>` to re-login safely and
`bin/codex-app-switch.sh <home>` to point the desktop app somewhere explicitly.

The daemon **never writes `auth.json`**. It detects drift, excludes duplicate
accounts from rotation, keeps backups, and tells you what to run — recovery is
always a deliberate manual step.

## Everyday commands

```sh
bin/doctor.sh --quick                       # is the install healthy?
daemon/subswitchd.py --status               # state, cooldowns, last 50 decisions
daemon/subswitchd.py --once --dry-run       # one safe collect+decide cycle
tail -f ~/.config/subswitch/logs/subswitchd.log
cswap list --json                           # every Claude account's usage
```

[`docs/runbook.md`](docs/runbook.md) covers forcing a switch, pausing the daemon,
changing enforcement, watcher behavior, notifications, and recovery.

## Configuration

`config.example.json` is the annotated default, copied to
`~/.config/subswitch/config.json` on first run. Common knobs:

| Key | Default | Meaning |
|---|---|---|
| `enforce` | `false` | Master switch. `false` = observe and log only. |
| `pollSeconds` | `60` | Usage poll interval. |
| `<provider>.thresholds.*` | `95` | Percent at which a window turns red. |
| `<provider>.harvest.enabled` | `true` | Spend weekly quota about to reset. |
| `codex.watcher.autoResume` | `true` | Resume a limit-hit session on a fresh account. |
| `codex.redeem.enabled` | `false` | Auto-redeem rate-limit reset credits. Off by default; irreversible. |

## Tests

```sh
tests/run-all.sh
```

126 unit tests plus shim and home-builder fixtures, on system Python. Every test
is hermetic — it builds its own fixture root and overrides `HOME`, so running
the suite never touches your real accounts, homes, config, or launchd.

## Uninstall

```sh
./uninstall.sh
```

Removes the LaunchAgent and the shim it installed. It never deletes an account
home, your config, or `~/.codex`.

## Privacy

This repo was extracted from a working personal setup, so it ships with its own
leak check:

```sh
bin/audit-privacy.sh --history
```

It fails on absolute home paths, real email addresses, credential-shaped
strings, hardcoded UIDs, non-synthetic UUIDs, and any committed runtime state —
across tracked files and every blob in git history. CI runs it on every push.

## Credits

subswitch is a coordination layer. The pieces it stands on:

- **[claude-swap](https://github.com/realiti4/claude-swap)** by [@realiti4](https://github.com/realiti4) — the Claude account switch primitive.
- **[CodexBar](https://codexbar.app/)** by [@steipete](https://github.com/steipete) — Codex/Claude usage reading and the menu bar UI.

Neither project is affiliated with subswitch. subswitch is not affiliated with
or endorsed by Anthropic or OpenAI.

## License

MIT — see [LICENSE](LICENSE).
