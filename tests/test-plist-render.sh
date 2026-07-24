#!/usr/bin/env bash
# Guard: install.sh renders the LaunchAgent template with no leftover tokens,
# and the result is a valid plist.
#
# Regression: the template's own explanatory comment once contained a
# double-underscore token, which the leftover-detector matched, so install.sh
# aborted every single time. A template comment must never look like a token.
#
# Hermetic: renders into $TMPDIR with fake values. Touches nothing real.

set -euo pipefail

repo_dir="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
template="$repo_dir/launchd/subswitch.daemon.plist.template"
fixture_root="$(mktemp -d "${TMPDIR:-/tmp}/subswitch-plist.XXXXXX")"
trap 'rm -rf "$fixture_root"' EXIT
rendered="$fixture_root/rendered.plist"

[[ -f "$template" ]] || { printf 'FAIL: template missing: %s\n' "$template" >&2; exit 1; }

# The template must be valid before rendering, too.
/usr/bin/plutil -lint "$template" >/dev/null

# Render with the exact substitution install.sh performs.
LABEL="com.subswitch.daemon" \
REPO_DIR="/opt/fixture/subswitch" \
HOME_DIR="/Users/example" \
USER_NAME="example" \
UID_NUM="777" \
/usr/bin/python3 - "$template" "$rendered" <<'PY'
import os, re, sys
src, dst = sys.argv[1], sys.argv[2]
text = open(src, encoding="utf-8").read()
for token, value in (
    ("__LABEL__", os.environ["LABEL"]),
    ("__REPO_DIR__", os.environ["REPO_DIR"]),
    ("__HOME__", os.environ["HOME_DIR"]),
    ("__USER__", os.environ["USER_NAME"]),
    ("__UID__", os.environ["UID_NUM"]),
):
    text = text.replace(token, value)
leftover = re.findall(r"__[A-Z_]+__", text)
if leftover:
    sys.stderr.write("unrendered placeholders remain: %s\n" % sorted(set(leftover)))
    sys.exit(1)
open(dst, "w", encoding="utf-8").write(text)
PY

/usr/bin/plutil -lint "$rendered" >/dev/null

# Substitutions actually landed.
grep -q '<string>com.subswitch.daemon</string>' "$rendered"
grep -q '/opt/fixture/subswitch/daemon/subswitchd.py' "$rendered"
grep -q '<string>/Users/example</string>' "$rendered"
grep -q 'cmux-777.sock' "$rendered"

# And nothing token-shaped survived anywhere in the output.
if grep -qE '__[A-Z_]+__' "$rendered"; then
  printf 'FAIL: token-shaped text survived rendering\n' >&2
  grep -nE '__[A-Z_]+__' "$rendered" >&2
  exit 1
fi

printf 'PASS: LaunchAgent template renders cleanly\n'
