# subswitch runbook

## What runs where

- `subswitchd` — launchd daemon `com.subswitch.daemon`, polls usage every 60s, owns all switching policy.
- `~/.local/bin/codex` — shim; picks the Codex account home per `~/.config/subswitch/codex-current`.
- `cswap` — Claude account switch mechanism (keychain swap), driven by subswitchd.
- Codex watcher — part of `subswitchd`; detects the usage-limit message in tmux Codex panes and can resume on the selected account.
- CodexBar — menu bar visibility for all accounts.

The operational files live under `~/.config/subswitch`: `config.json`, `state.json`, `codex-current`, and `logs/`. On first run, the daemon copies the repo's `config.example.json` to `config.json`. The safe default is `enforce: false`.

## Status and decisions

Run these from the repository checkout:

```sh
./daemon/subswitchd.py --status
tail -50 ~/.config/subswitch/logs/subswitchd.log
cswap list --json
cat ~/.config/subswitch/codex-current
launchctl print gui/$(id -u)/com.subswitch.daemon
```

`--status` pretty-prints the persisted state, including cooldowns, ladder flags, notification/debounce state, and the last 50 decisions. The daemon log is the human-readable decision trail. `DRY-RUN` on a decision means the full policy ran but no switch, pointer write, or tmux input occurred.

For one safe diagnostic collection/policy cycle, force enforcement off regardless of config:

```sh
./daemon/subswitchd.py --once --dry-run
```

## Capability doctor & canary

Run the read-only doctor before a work session or after a Codex/ChatGPT update:

```sh
bin/doctor.sh --quick
```

It checks the local shim and PATH order, pointer validity, launchd, Claude usage health, authenticated CodexBar reads, the computer-use MCP setting, and thin-home Chrome pairing symlinks. Every failure includes its exact repair command. `--quick` deliberately skips the final screenshot canary.

The full doctor performs one small ground-truth Codex call that asks its `computer-use` tool to take a screenshot:

```sh
bin/doctor.sh
```

The canary has a 240-second bound and succeeds only when the model's saved answer is exactly `CU-OK`. It does not use a pipe, so Codex MCP child output cannot wedge the check. If it fails, quit and reopen the ChatGPT app, trigger a computer-use action in its UI, and approve Screen Recording under **System Settings → Privacy & Security**. If no approval prompt appears, remove and re-add the stale ChatGPT entry, then retry.

While Codex is enabled, the daemon also guards `~/.codex/config.toml`: with the default `codex.configGuard: true`, it changes only `enabled = false` within `[mcp_servers.computer-use]` back to `enabled = true`, atomically and without reserializing TOML. The action is logged and notification-deduplicated for 30 minutes. To apply that self-heal immediately, run one dry-run tick:

```sh
./daemon/subswitchd.py --once --dry-run
```

## Force a provider switch

Pause the daemon first if the manual selection must remain fixed. To select a Claude account explicitly:

```sh
cswap switch 1 --json
```

Replace `1` with the registered account number only during an authorized account operation. Never run `cswap auto`; `subswitchd` owns the policy.

To select an authenticated Codex home for future launches, update the pointer atomically without touching `auth.json`:

```sh
home="$HOME/.codex-2"
test -f "$home/auth.json" && {
  tmp="$HOME/.config/subswitch/.codex-current.$$"
  printf '%s\n' "$home" > "$tmp" && mv "$tmp" "$HOME/.config/subswitch/codex-current"
}
```

Existing Codex processes retain their in-memory authentication. Start a new `codex` process, or use `codex resume --last`, to adopt the selected home.

## Pause and resume the daemon

Pause switching and polling while retaining the installed plist:

```sh
launchctl bootout gui/$(id -u)/com.subswitch.daemon
```

Resume it:

```sh
launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/com.subswitch.daemon.plist"
launchctl kickstart gui/$(id -u)/com.subswitch.daemon
```

`KeepAlive` restarts a loaded daemon, so use `bootout`—not `kill`—for a deliberate pause.

## Change enforcement

Stop the daemon, edit `~/.config/subswitch/config.json`, change only the top-level `enforce` value, validate the JSON, and resume:

```sh
/usr/bin/python3 -m json.tool "$HOME/.config/subswitch/config.json" >/dev/null
```

- `"enforce": false` keeps collection, decisions, logs, and notifications active but prevents Claude switches, Codex pointer writes, and watcher keystrokes.
- `"enforce": true` enables those actions. Turn it on only after every intended Claude account and Codex home has been enrolled and verified.

Config is read on daemon ticks, but pausing around an enforcement edit avoids racing a poll. `--dry-run` always forces enforcement off for that invocation.

## Reset harvesting

Reset harvesting spends an otherwise-unused weekly allowance shortly before it resets: while the active account is healthy and no red, hard-limit, unknown, or ladder path applies, the daemon may select a different healthy account with an imminent weekly reset. Per-provider `harvest` settings control it: `enabled`, `windowHours` (24 by default), `minWeeklyLeftPct` (40), `maxFiveHourPct` (50), and `cooldownSeconds` (10,800). Existing configurations without `harvest` retain these defaults.

The state records every `(account, resetsAt)` harvest and a provider harvest timestamp. Therefore a reported weekly reset is harvested at most once, while actual cross-account harvest switches are separated by the configured cooldown. If the active account has an equal-or-sooner eligible reset, the daemon records that target and stays put; it consumes no cooldown. This keeps bounded proactive switching even when telemetry repeats across polls.

## Watcher behavior

Each tick, the watcher inspects tmux panes whose current command is `codex`. No tmux server is a normal no-op. When it finds `You've hit your usage limit`, a healthy candidate exists, `enforce` is true, and `codex.watcher.autoResume` is true, it rotates the pointer if needed and sends two interrupts followed by a pinned `codex resume <session-id>` command. It never changes an account's `auth.json`.

The state file prevents refiring on the same pane/message and applies the configured debounce (600 seconds by default). Every watcher action is logged loudly. To disable only automatic pane input while retaining proactive account selection, set `codex.watcher.autoResume` to `false`.

### cmux coverage

cmux is enabled by default with `codex.watcher.cmux: true`. The watcher uses the socket CLI shipped at `/Applications/cmux.app/Contents/Resources/bin/cmux` (falling back to `cmux` on `PATH`) as follows:

- `cmux --json top --all --processes` enumerates windows, workspaces, panes, terminal surfaces, and their process trees. A process whose name starts with `codex` marks that surface as a Codex surface; the process PID is used by the same rollout-file pinning logic as tmux.
- `cmux read-screen --surface <id> --scrollback --lines 200` reads terminal text. The literal usage-limit message is fingerprinted and deduplicated in the same `state.panes` store as tmux.
- With enforcement and auto-resume enabled, cmux receives two `send-key --surface <id> ctrl+c` calls followed by `respawn-pane --surface <id> --command '<shim> resume <session-id>'`. Despite its tmux-compatible name, cmux's `respawn-pane` is its run-command primitive for the existing terminal surface.

If cmux reports a Codex process without a PID, or the PID cannot be matched uniquely to a rollout, detection and notification still work but automatic input is refused rather than risking the wrong session. A stopped/unreachable cmux socket is a logged no-op for that tick. Set `codex.watcher.cmux` to `false` to disable all cmux scanning and input.

cmux 0.64.16 also exposes reconnectable JSONL events and Codex hooks. Installed Codex hooks publish lifecycle events such as `agent.hook.Stop`; cmux derives error/rate-limit status from the Codex transcript. Those hooks are optional and write Codex configuration, so subswitch does not install or depend on them. The watcher deliberately scans the literal terminal text each tick, matching the tmux contract even when hooks are absent.

## Notifications and logs

Notifications use the title `subswitch` and the macOS `Sosumi` sound. Ordinary notification keys are deduplicated for at least 30 minutes; ladder rotations are announced each time. Entering an all-red zone is announced once, and recovery is announced when an account becomes healthy again.

The daemon self-rotates `~/.config/subswitch/logs/subswitchd.log` at 5 MB. launchd process output is separate:

```sh
tail -50 ~/.config/subswitch/logs/launchd.out.log
tail -50 ~/.config/subswitch/logs/launchd.err.log
```

## Common operations

- Change polling frequency with top-level `pollSeconds`.
- Disable one provider by setting its `enabled` field to `false`.
- Inspect candidate eligibility in state before forcing a switch; missing `auth.json`, collector errors, and usage older than `staleMaxSeconds` are unknown and are never selected.
- If `cswap switch` reports a lock timeout while Claude Code is refreshing credentials, leave the daemon running; it retries on the next tick.

## Computer-use headless approval

Headless (`codex exec`) computer-use keeps a per-app approval store SEPARATE from app-session grants:
`~/Library/Group Containers/2DC432GLL2.com.openai.sky.CUAService/Library/Application Support/Software/ComputerUseAppApprovals.json`
Schema: `{"approvedBundleIdentifiers": ["com.google.Chrome"]}` (no expiry; user-owned plain JSON).
Supported route: ChatGPT app → Computer Use task → "Always allow" → Settings → Computer Use → Always-allowed apps.
Fallback: write the JSON atomically while no computer-use task runs. `bin/probe-computer-use.sh` is ground truth; the doctor runs it.

## Browser lanes after rotation

- Rotation-proof by design: computer-use approvals + Chrome pairing are machine-level; account swaps never touch them.
- Headless codex (exec/tmux) does NOT get the Chrome extension DOM channel — it is app-attached-only by design. Headless browser work drives Chrome via computer-use VISUAL actions (screenshot → click → type). Verified working.
- ChatGPT desktop app = single-account surface (auth internal, not switchable programmatically). Playbook: pin it to one account and leave it there; when it walls, redeem a rate-limit reset credit (account menu → resets available). Long autonomous runs belong in the tmux lane, never app tasks. Use `bin/codex-app-switch.sh` if you must point the app at a different home — opening it plainly writes into whichever home is active.
- claude-in-chrome (fallback lane) needs browser claude.ai login == active CLI account; to use it deterministically pin the terminal: `cswap run <slot-of-browser-account> -- claude ...`.
- In-flight semantics: running codex sessions keep their account until exit (memory-cached auth); running Claude sessions adopt swaps mid-conversation ≤30s; exhausted tmux codex sessions get watcher-resumed on the fresh account.
