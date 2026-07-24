#!/usr/bin/env bash
# Safe per-home Codex re-login.
#
# The failure that killed .codex-2 (Jul-17) was a login that put an account
# into a SECOND home, and logins that ran while a process still held the home.
# Codex refresh tokens are single-use (a successful refresh exhausts the old
# one), and `codex logout` is the only confirmed explicit token revoker — so
# this helper NEVER logs out, forces an explicit CODEX_HOME, refuses to run
# while the home has live processes, and verifies afterward that the right,
# non-duplicate account landed.
#
# Usage:  codex-relogin.sh <home-path>
#   e.g.  codex-relogin.sh ~/.codex        (expect the account you enrolled there)
#         codex-relogin.sh ~/.codex-2      (expect that home's own account)

set -uo pipefail

REAL_CODEX="/opt/homebrew/bin/codex"
DEFAULT_HOME="$HOME/.codex"

home_arg="${1:-}"
if [[ -z "$home_arg" ]]; then
  echo "usage: codex-relogin.sh <home-path>   (e.g. ~/.codex, ~/.codex-2, ~/.codex-3)" >&2
  exit 2
fi
home="$(cd "$home_arg" 2>/dev/null && pwd)"
if [[ -z "$home" || ! -d "$home" ]]; then
  echo "❌ no such codex home: $home_arg" >&2
  exit 1
fi

# --- Guard 0: one re-login at a time, machine-wide --------------------------
# Two helpers running concurrently could both snapshot clean state, choose the
# same account, and both report success. A mkdir lock is atomic on macOS
# (no flock), covering snapshot -> login -> verify.
config_dir="${SUBSWITCH_CONFIG_HOME:-$HOME/.config/subswitch}"
mkdir -p "$config_dir" 2>/dev/null || true
lock_dir="$config_dir/relogin.lock"
if ! mkdir "$lock_dir" 2>/dev/null; then
  echo "❌ another codex re-login is already in progress ($lock_dir). Wait for it or remove the stale lock." >&2
  exit 1
fi
trap 'rmdir "$lock_dir" 2>/dev/null || true' EXIT

# --- Guard 1: refuse if any live codex process is using this home -----------
# A process uses this home if its CODEX_HOME env equals it, OR it has no
# CODEX_HOME and this home is the default ~/.codex. ps env can carry non-UTF-8
# bytes, so scrub before matching.
live_on_home=()
pending_login=""
while IFS= read -r pid; do
  [[ -z "$pid" ]] && continue
  cmd="$(ps -o command= -p "$pid" 2>/dev/null | tr -c '[:print:]' ' ')"
  env_home="$(ps eww "$pid" 2>/dev/null | tr -c '[:print:]\n' ' ' | tr ' ' '\n' \
              | grep '^CODEX_HOME=' | head -1 | cut -d= -f2-)"
  if [[ -z "$env_home" ]]; then env_home="$DEFAULT_HOME"; fi
  if [[ "$env_home" == "$home" ]]; then
    live_on_home+=("$pid")
    [[ "$cmd" == *" login"* ]] && pending_login="$pid"
  fi
done < <(pgrep -f 'codex-aarch64|/opt/homebrew/bin/codex' 2>/dev/null)

if [[ -n "$pending_login" ]]; then
  port="$(lsof -Pan -p "$pending_login" -iTCP -sTCP:LISTEN 2>/dev/null \
          | awk 'NR>1{sub(/.*:/,"",$9); print $9; exit}')"
  echo "⏳ A codex login for $home is ALREADY in progress (pid $pending_login)." >&2
  [[ -n "$port" ]] && echo "   Finish it: open the browser tab it started (localhost:$port) and pick the account," >&2
  echo "   or cancel it first:  kill $pending_login" >&2
  echo "   Not starting a second login." >&2
  exit 1
fi

if (( ${#live_on_home[@]} > 0 )); then
  echo "❌ REFUSING: ${#live_on_home[@]} live codex process(es) are using $home: ${live_on_home[*]}" >&2
  echo "   Re-login while a process holds the home can clobber its auth.json." >&2
  echo "   Stop those processes first, then re-run." >&2
  exit 1
fi

echo "▶ Logging in home: $home"
echo "  (a browser window will open — pick the account, then WAIT for the terminal to confirm success)"
echo

CODEX_HOME="$home" "$REAL_CODEX" login
login_rc=$?
if (( login_rc != 0 )); then
  echo "❌ codex login exited $login_rc — nothing verified. Re-run when ready." >&2
  exit $login_rc
fi

# --- Guard 2: verify a real account landed and is NOT a duplicate -----------
# Re-reads ALL other homes NOW (not the pre-login snapshot) so a login that
# happened elsewhere during the browser pause is still caught.
/usr/bin/python3 - "$home" <<'PY'
import base64, glob, json, os, sys
home = sys.argv[1]
a = os.path.join(home, "auth.json")
try:
    d = json.load(open(a)); t = d.get("tokens") or {}
except Exception as e:
    print("❌ could not read %s after login: %s" % (a, e)); sys.exit(1)
aid = t.get("account_id")
access = t.get("access_token")
if not aid or not access:
    print("❌ login did not produce a usable account (account_id=%r, token present=%s)."
          % (aid, bool(access)))
    print("   Do NOT trust this home — re-run the login.")
    sys.exit(2)
email = None
parts = str(t.get("id_token", "")).split(".")
if len(parts) == 3:
    try:
        pad = parts[1] + "=" * (-len(parts[1]) % 4)
        email = json.loads(base64.urlsafe_b64decode(pad)).get("email")
    except Exception:
        pass
# Fresh scan of every OTHER home for the same account_id.
before = {}
for p in sorted(glob.glob(os.path.expanduser("~/.codex*"))):
    if not os.path.isdir(p) or os.path.realpath(p) == os.path.realpath(home):
        continue
    try:
        ot = (json.load(open(os.path.join(p, "auth.json"))).get("tokens") or {})
        if ot.get("account_id"):
            before[ot["account_id"]] = p
    except Exception:
        pass
print("✅ %s now logged in as: %s  (account_id %s)" % (os.path.basename(home), email or "?", aid))
if aid in before:
    print("🚨 DUPLICATE ACCOUNT: this same account is ALSO in %s." % before[aid])
    print("   That is the exact condition that revokes tokens. Log this home into a DIFFERENT account.")
    sys.exit(3)
print("   No duplicate across homes. Safe.")
PY
verify_rc=$?
echo
if (( verify_rc == 0 )); then
  echo "Done. Ask the orchestrator to re-verify auth + computer-use on this home."
fi
exit $verify_rc
