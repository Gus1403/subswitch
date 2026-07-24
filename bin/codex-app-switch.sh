#!/usr/bin/env bash
# Launch the ChatGPT desktop app pinned to a specific CODEX_HOME.
#
# The desktop app writes auth.json into whatever CODEX_HOME it runs under, and
# defaults to ~/.codex when none is set. Opening it plainly is exactly how a
# home gets clobbered (a login there overwrites that home's account). This is
# the ONLY safe way to use the desktop app across multiple accounts: it forces
# an explicit CODEX_HOME so the app can only ever touch the home you name.
#
# Usage:  codex-app-switch.sh <home-path>
#   e.g.  codex-app-switch.sh ~/.codex-2      (open the app on that home's account)

set -uo pipefail

APP="/Applications/ChatGPT.app"
DEFAULT_HOME="$HOME/.codex"

home_arg="${1:-}"
if [[ -z "$home_arg" ]]; then
  echo "usage: codex-app-switch.sh <home-path>   (e.g. ~/.codex, ~/.codex-2, ~/.codex-3)" >&2
  exit 2
fi
home="$(cd "$home_arg" 2>/dev/null && pwd)"
if [[ -z "$home" || ! -d "$home" ]]; then
  echo "❌ no such codex home: $home_arg" >&2
  exit 1
fi
if [[ ! -d "$APP" ]]; then
  echo "❌ ChatGPT app not found at $APP" >&2
  exit 1
fi

# Which account currently lives in that home (best-effort, no network).
who="$(/usr/bin/python3 - "$home" <<'PY' 2>/dev/null
import base64, json, os, sys
try:
    t = json.load(open(os.path.join(sys.argv[1], "auth.json")))["tokens"]
    parts = str(t.get("id_token", "")).split(".")
    email = "?"
    if len(parts) == 3:
        pad = parts[1] + "=" * (-len(parts[1]) % 4)
        email = json.loads(base64.urlsafe_b64decode(pad)).get("email", "?")
    print(email)
except Exception:
    print("?")
PY
)"

if [[ "$home" == "$DEFAULT_HOME" ]]; then
  echo "⚠️  This is the DEFAULT home ($DEFAULT_HOME). Keep the least-critical account here —"
  echo "    the desktop app targets it whenever it is opened without CODEX_HOME."
fi

echo "▶ Opening ChatGPT.app pinned to CODEX_HOME=$home  (account: ${who:-?})"
exec open -n -a "$APP" --env CODEX_HOME="$home"
