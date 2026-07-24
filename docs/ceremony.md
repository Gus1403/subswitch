# Enrollment ceremony

Getting your accounts registered, verified, and switching. Budget ~15 minutes
in one sitting. **Order matters** — do not enable enforcement until the end.

If you'd rather have your coding agent drive this, point it at
[`AGENTS.md`](../AGENTS.md) instead; it's the same procedure with explicit
verification gates.

Before starting, run `bin/preflight.sh` and fix anything it fails on, then
`./install.sh`.

---

## Why the order matters

Two things make this different from a normal install:

1. **Only a human can log in.** Every account needs a browser OAuth flow.
2. **A wrong Codex login is permanent.** `codex login` writes `auth.json` into
   whatever `CODEX_HOME` is active, and Codex refresh tokens are single-use. Log
   in without pinning `CODEX_HOME` and you can silently overwrite another
   account's session with no way back except logging in again.

So: enroll one account at a time, verify each landed correctly before moving on,
and leave switching off until everything is verified.

---

## A. Claude accounts

Do this for **every** account, including the one you already use — a
pre-existing credential snapshot can carry a stale identity label.

For each account:

1. In a fresh terminal: `claude`, then `/login`. Complete the browser login as
   that subscription's account. Then `/exit`.
2. Snapshot it into the next slot:

   ```sh
   cswap add
   cswap list --json
   ```

3. **Verify before continuing:**
   - the new entry shows the email you expect
   - its `organizationUuid` differs from every other entry (identical UUIDs mean
     you added the same subscription twice — redo the login)
   - `usageStatus` is `ok`, and the usage roughly matches that account's real
     `/usage` screen

When every account is in:

```sh
cswap switch 1 --json
cswap list --json
```

You should see all your accounts, all `ok`, all with distinct
`organizationUuid`s.

---

## B. Codex accounts

`~/.codex` is your canonical home and is already logged in. `install.sh` created
the thin homes (`~/.codex-2`, `~/.codex-3`), which share sessions, config, and
skills by symlink but keep `auth.json` private per home.

Need more homes than the default two?

```sh
SUBSWITCH_HOMES="$HOME/.codex-2 $HOME/.codex-3 $HOME/.codex-4" bin/make-codex-homes.sh
```

…then list them all under `codex.homes` in `~/.config/subswitch/config.json`.

For each additional account, **always pin `CODEX_HOME`**:

```sh
CODEX_HOME="$HOME/.codex-2" codex login
```

Verify it landed the account you meant, and that it's distinct:

```sh
CODEX_HOME="$HOME/.codex-2" codexbar usage --provider codex --format json
```

Repeat for `~/.codex-3`, and so on.

> If two homes end up on the same account, **do not copy `auth.json` between
> them** — single-use tokens mean you'll break both. Re-run the login for the
> affected home with the correct `CODEX_HOME`, or use
> `bin/codex-relogin.sh <home>`, which refuses the unsafe cases.

---

## C. Visibility (CodexBar)

1. CodexBar → Settings → Providers → **Claude**: enable claude-swap and set the
   executable path to `~/.local/bin/cswap`. Each Claude account appears as its
   own card.
2. → **Codex**: confirm each account home is listed; add `~/.codex-2`,
   `~/.codex-3`, … if they weren't auto-detected.

Every enrolled account should now be visible. The daemon reads Codex usage
through this same `codexbar` CLI, so if an account is missing here, the daemon
can't see it either.

---

## D. Verify while still observe-only

```sh
bin/doctor.sh --quick
daemon/subswitchd.py --once --dry-run
tail -30 ~/.config/subswitch/logs/subswitchd.log
```

You want: no `FAIL` from the doctor, usage read for **every** account (nothing
stuck `unknown` — an unknown account is never selected), and decisions labelled
`DRY-RUN`.

---

## E. Turn it on

```sh
launchctl bootout "gui/$(id -u)/com.subswitch.daemon" 2>/dev/null || true
# edit ~/.config/subswitch/config.json -> "enforce": true
/usr/bin/python3 -m json.tool ~/.config/subswitch/config.json >/dev/null
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.subswitch.daemon.plist
launchctl kickstart "gui/$(id -u)/com.subswitch.daemon"
```

Then prove a real switch round-trips:

```sh
cswap list --json      # note active
cswap switch 2 --json
cswap list --json      # active changed
cswap switch 1 --json
cswap list --json      # and back, everything still healthy
```

The round trip matters: it proves the outgoing account's rotated token gets
re-snapshotted rather than lost.

---

## Living with it

- Running Codex processes keep the account they started with. Only new launches
  adopt a rotation. Running Claude sessions adopt a swap within ~30s.
- **Never** run a bare `codex login` or `codex logout` again. Use
  `bin/codex-relogin.sh <home>`.
- Opening the ChatGPT desktop app plainly writes into whichever home is active.
  Use `bin/codex-app-switch.sh <home>` to pin it.
- Day-to-day operations — forcing a switch, pausing, changing enforcement,
  watcher behavior — are in [`runbook.md`](runbook.md).
