# Dangerous Codex commands — what they do and why they're guarded

Read this once. It is the difference between rotating accounts safely and
permanently destroying one.

Evidence base: reverse-engineering of `codex-cli` 0.144.6, plus live forensics
from running multiple accounts side by side. The shim (`bin/codex`) intercepts
the two commands below and requires confirmation; every other command passes
through untouched.

## The one rule that matters

**A Codex account survives when exactly one refresher touches it.**

It breaks when a second refresher hits the same account — a second home holding
the same login, a second process refreshing one home, or a `logout`. Codex
refresh tokens are **single-use**: a successful refresh persists a successor and
replaying the old one fails permanently. An idle, single-login home is stable
indefinitely; a double-touched one dies without warning.

## Guarded command 1 — `codex logout`

- **Confirmed:** `logout` calls `logout_with_revoke` →
  `https://auth.openai.com/oauth/revoke`. It is the **only** action in the CLI
  that explicitly revokes a refresh token.
- **Consequence:** that account stops working immediately. Recovery is a browser
  re-login, nothing less.
- **Extra danger:** a bare `codex logout` resolves the home from the subswitch
  pointer, so it revokes whichever account is *currently active* — quite
  possibly not the one you were thinking of.
- **Guard:** prints the resolved home and account, then requires you to type
  `revoke <home>`. Non-interactive callers (agents, scripts) are refused unless
  `SUBSWITCH_ALLOW_LOGOUT=1`.

## Guarded command 2 — bare `codex login` (no `CODEX_HOME`)

- **Why dangerous:** it writes `auth.json` into the pointer-selected home. If you
  sign in as an account that already lives in another home, that account now
  exists in two homes — and the next refresh invalidates one of them. Sign in as
  a *different* account and you have silently overwritten the login that was
  there. Both outcomes are unrecoverable without re-authenticating.
- **Guard:** prints the target home and its current account, then points you at
  the safe path. Refused non-interactively unless
  `SUBSWITCH_ALLOW_BARE_LOGIN=1`.
- **Safe alternative:** `bin/codex-relogin.sh <home>` — pins `CODEX_HOME`,
  refuses to run while that home has live processes, never logs out, and
  verifies afterward that the intended, non-duplicate account actually landed.

## The ChatGPT desktop app is the same footgun

The desktop app writes `auth.json` into whatever `CODEX_HOME` it inherits, and
defaults to `~/.codex` when none is set. Opening it plainly is a common way to
clobber a home. Use `bin/codex-app-switch.sh <home>`, which launches it with an
explicit `CODEX_HOME` so it can only touch the home you named.

## Not guarded (safe), for reference

- `codex exec`, `codex resume`, and any other run command — pass through
  untouched, so automation is never blocked.
- `CODEX_HOME=<home> codex login` — an *explicit* home is the operator-override
  path. It is not the bare-login footgun, so it isn't gated, though
  `codex-relogin.sh` is still preferred for its post-login duplicate check.

## Overrides (for automation that really means it)

| Command | Bypass env |
|---|---|
| `codex logout` | `SUBSWITCH_ALLOW_LOGOUT=1` |
| bare `codex login` | `SUBSWITCH_ALLOW_BARE_LOGIN=1` |

## Standing rules

1. One refresh-owner per home; never two processes refreshing one `auth.json`.
2. Never copy or share `auth.json` or refresh tokens between homes.
3. Never hot-swap `auth.json` under a running process; stop processes first.
4. Always launch workers with an explicit absolute `CODEX_HOME`.
5. Never `codex logout` an account you still need.
6. Re-login a dead home only when it has no live processes; never log in over a
   healthy home.
7. Keep each home mapped permanently to one distinct account.
8. Fail over by starting a fresh process under another home — never by moving
   credentials.

subswitch itself follows these rules: the daemon **never writes `auth.json`**.
It detects identity drift and duplicates, excludes duplicates from rotation,
keeps per-account backups under `~/.config/subswitch/auth-backups/`, and alerts
you to run the manual recovery command. Recovery is always deliberate.
