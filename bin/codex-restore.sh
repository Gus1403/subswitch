#!/usr/bin/env bash
# Manual, lock-coordinated restore of a codex home's auth.json from the daemon's
# protected backups. Recovers an account after a clobber (two homes on the same
# account = the token-revoking case).
#
# The daemon NEVER writes auth.json itself — auto-restore against concurrent
# logins/refreshes is unsafe. This is the human-in-the-loop recovery path: it
# takes the SAME lock as codex-relogin.sh, refuses to run while the home is
# live, and refuses any restore that would create a duplicate. A codex refresh
# token is single-use, so if the account had re-authed since the backup you may
# still need codex-relogin.sh — this is best-effort.
#
# Usage:
#   codex-restore.sh <home>            # list recoverable backups
#   codex-restore.sh <home> <email>    # restore that account into <home>

set -uo pipefail

# Match every process that can write a home's auth.json: the codex CLI, the
# codexbar usage poller, and the ChatGPT desktop app's bundled codex app-server.
REAL_CODEX_GLOB='codex-aarch64|/opt/homebrew/bin/codex|codexbar|ChatGPT.app/Contents/Resources/codex'
DEFAULT_HOME="$HOME/.codex"
config_dir="${SUBSWITCH_CONFIG_HOME:-$HOME/.config/subswitch}"
backup_dir="$config_dir/auth-backups/codex"

home_arg="${1:-}"
email_arg="${2:-}"
if [[ -z "$home_arg" ]]; then
  echo "usage: codex-restore.sh <home> [email]   (e.g. ~/.codex-2)" >&2
  exit 2
fi
home="$(cd "$home_arg" 2>/dev/null && pwd)"
if [[ -z "$home" || ! -d "$home" ]]; then
  echo "❌ no such codex home: $home_arg" >&2
  exit 1
fi

# --- Guard 0: one login/restore at a time, machine-wide (shared with relogin) -
mkdir -p "$config_dir" 2>/dev/null || true
lock_dir="$config_dir/relogin.lock"
if ! mkdir "$lock_dir" 2>/dev/null; then
  echo "❌ a codex login/restore is already in progress ($lock_dir). Wait or remove the stale lock." >&2
  exit 1
fi
trap 'rmdir "$lock_dir" 2>/dev/null || true' EXIT

# --- Guard 1: refuse if any live codex process is using this home -------------
live_on_home=()
while IFS= read -r pid; do
  [[ -z "$pid" ]] && continue
  env_home="$(ps eww "$pid" 2>/dev/null | tr -c '[:print:]\n' ' ' | tr ' ' '\n' \
              | grep '^CODEX_HOME=' | head -1 | cut -d= -f2-)"
  [[ -z "$env_home" ]] && env_home="$DEFAULT_HOME"
  env_home="$(cd "$env_home" 2>/dev/null && pwd)"  # canonicalize -> symlink-safe
  [[ "$env_home" == "$home" ]] && live_on_home+=("$pid")
done < <(pgrep -f "$REAL_CODEX_GLOB" 2>/dev/null)
if (( ${#live_on_home[@]} > 0 )); then
  echo "❌ REFUSING: ${#live_on_home[@]} live codex process(es) are using $home: ${live_on_home[*]}" >&2
  echo "   Restoring while a process holds the home can clobber it again. Stop them first." >&2
  exit 1
fi

# --- List / restore (python: reads backups + all homes, avoids duplicates) ----
/usr/bin/python3 - "$home" "$email_arg" "$backup_dir" "$config_dir" <<'PY'
import base64, glob, json, os, sys, tempfile

home, email_arg, backup_dir, config_dir = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]

def identity(data):
    t = (data or {}).get("tokens") if isinstance(data, dict) else None
    t = t if isinstance(t, dict) else {}
    aid = t.get("account_id")
    email = None
    parts = str(t.get("id_token") or "").split(".")
    if len(parts) == 3:
        try:
            pad = parts[1] + "=" * (-len(parts[1]) % 4)
            claims = json.loads(base64.urlsafe_b64decode(pad))
            email = claims.get("email")
            if not aid:
                aid = (claims.get("https://api.openai.com/auth") or {}).get("chatgpt_account_id")
        except Exception:
            pass
    complete = bool(t.get("account_id") and t.get("refresh_token") and t.get("access_token"))
    return aid, (email or "?"), complete

def load(path):
    try:
        return json.load(open(path))
    except Exception:
        return None

# The universe of homes = configured homes UNION the ~/.codex* legacy glob,
# canonicalized. Scanning only the glob would miss a configured home elsewhere
# and let restore create the very duplicate we must avoid.
home_set = set()
for h in glob.glob(os.path.expanduser("~/.codex*")):
    home_set.add(os.path.realpath(h))
try:
    cfg = json.load(open(os.path.join(config_dir, "config.json")))
    for item in (cfg.get("codex", {}) or {}).get("homes", []) or []:
        home_set.add(os.path.realpath(os.path.expanduser(item)))
except Exception:
    pass
home_set.add(os.path.realpath(home))

# Accounts currently live across every home (to refuse duplicate-creating restores).
live = {}  # account_id -> set[canonical_home]  (a set so existing dups stay visible)
for h in sorted(home_set):
    if not os.path.isdir(h):
        continue
    aid, _, _ = identity(load(os.path.join(h, "auth.json")))
    if aid:
        live.setdefault(aid, set()).add(os.path.realpath(h))

# Available backups.
backups = []  # (account_id, email, path)
for b in sorted(glob.glob(os.path.join(backup_dir, "*.json"))):
    aid, email, complete = identity(load(b))
    if aid and complete:
        backups.append((aid, email, b))

cur_aid, cur_email, _ = identity(load(os.path.join(home, "auth.json")))
print("target home: %s  (currently: %s)" % (home, cur_email))

if not email_arg:
    print("\nRecoverable backups (not currently live anywhere):")
    any_free = False
    for aid, email, _ in backups:
        where = live.get(aid)
        tag = ("  LIVE in %s" % ", ".join(sorted(where))) if where else "  (free)"
        if not where:
            any_free = True
        print("  - %-28s [%s]%s" % (email, aid, tag))
    if not any_free:
        print("  (none free — every backed-up account is already live in a home)")
    print("\nTo restore:  codex-restore.sh %s <email-or-account_id>" % home)
    sys.exit(0)

# Select by exact account_id OR by email — but email must map to exactly one id.
match = [(aid, email, path) for (aid, email, path) in backups if aid == email_arg]
if not match:
    by_email = [(aid, email, path) for (aid, email, path) in backups
                if email == email_arg and email != "?"]
    ids = {aid for aid, _, _ in by_email}
    if len(ids) > 1:
        print("❌ %r maps to multiple accounts %s — re-run with the exact account_id."
              % (email_arg, sorted(ids)))
        sys.exit(1)
    match = by_email
if not match:
    print("❌ no complete backup for %r. Run without an argument to list options." % email_arg)
    sys.exit(1)
aid, email, path = match[0]
others = live.get(aid, set()) - {os.path.realpath(home)}
if others:
    print("❌ %s is ALREADY live in %s — restoring it here would create the duplicate "
          "we are trying to avoid. Refusing." % (email, ", ".join(sorted(others))))
    sys.exit(3)

content = open(path, "r", encoding="utf-8").read()
dest = os.path.join(home, "auth.json")
fd, tmp = tempfile.mkstemp(prefix=".auth.", dir=home)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, dest)
except BaseException:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise

back_aid, back_email, _ = identity(load(dest))
if back_aid != aid:
    print("❌ restore verify failed (home now shows %s)." % back_email)
    sys.exit(4)
print("✅ restored %s into %s." % (email, home))
print("   Single-use token caveat: if this account had re-authed since the backup,")
print("   the restored token may be stale — if codex rejects it, run codex-relogin.sh %s." % home)
PY
verify_rc=$?
echo
if (( verify_rc == 0 )); then
  echo "Done. The daemon will re-pin this home on its next tick."
fi
exit $verify_rc
