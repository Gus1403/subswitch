#!/usr/bin/env bash
# Install the Codex shim, build the thin Codex account homes, and load the
# subswitch LaunchAgent.
#
# Safe to re-run. The daemon starts in observe-only mode: on first run it
# creates ~/.config/subswitch/config.json from config.example.json, which has
# "enforce": false. Nothing is switched until you turn enforcement on
# deliberately (see docs/ceremony.md).

set -u

repo_dir="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
shim_source="$repo_dir/bin/codex"
local_bin="$HOME/.local/bin"
shim_link="$local_bin/codex"
label="com.subswitch.daemon"

if [[ "$(uname -s)" != "Darwin" ]]; then
  printf 'ERROR: subswitch is macOS-only (it uses launchd and the macOS keychain).\n' >&2
  exit 1
fi

mkdir -p "$HOME/.config/subswitch/logs" "$local_bin"

# --- Codex shim -------------------------------------------------------------
if [[ -L "$shim_link" && "$(readlink "$shim_link")" == "$shim_source" ]]; then
  printf 'INFO: Codex shim is already installed: %s\n' "$shim_link"
elif [[ -e "$shim_link" || -L "$shim_link" ]]; then
  printf 'WARN: not replacing existing %s\n' "$shim_link" >&2
  printf 'WARN: remove it yourself if you want subswitch to manage the codex entrypoint.\n' >&2
else
  ln -s "$shim_source" "$shim_link"
  printf 'INFO: installed Codex shim: %s\n' "$shim_link"
fi

IFS=':' read -r -a path_entries <<< "${PATH:-}"
local_position=-1
homebrew_position=-1
for index in "${!path_entries[@]}"; do
  [[ "${path_entries[$index]}" == "$local_bin" ]] && local_position=$index
  [[ "${path_entries[$index]}" == "/opt/homebrew/bin" ]] && homebrew_position=$index
done
if (( local_position < 0 || (homebrew_position >= 0 && local_position > homebrew_position) )); then
  printf 'WARN: %s does not precede /opt/homebrew/bin in PATH; the shim may not be used.\n' "$local_bin" >&2
  printf 'WARN: add   export PATH="$HOME/.local/bin:$PATH"   to your shell profile.\n' >&2
fi

# --- Thin Codex account homes ----------------------------------------------
"$repo_dir/bin/make-codex-homes.sh"

# --- LaunchAgent ------------------------------------------------------------
launch_agents="$HOME/Library/LaunchAgents"
template="$repo_dir/launchd/subswitch.daemon.plist.template"
target="$launch_agents/$label.plist"

if [[ ! -f "$template" ]]; then
  printf 'ERROR: missing plist template: %s\n' "$template" >&2
  exit 1
fi

mkdir -p "$launch_agents"

# Render the template for this machine. Absolute paths and the username exist
# only in the rendered file under ~/Library/LaunchAgents, never in the repo.
rendered="$(mktemp "${TMPDIR:-/tmp}/subswitch-plist.XXXXXX")" || exit 1
trap 'rm -f "$rendered"' EXIT

if ! LABEL="$label" REPO_DIR="$repo_dir" HOME_DIR="$HOME" \
     USER_NAME="$(id -un)" UID_NUM="$(id -u)" \
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
    sys.stderr.write("ERROR: unrendered placeholders remain: %s\n" % sorted(set(leftover)))
    sys.exit(1)
open(dst, "w", encoding="utf-8").write(text)
PY
then
  printf 'ERROR: failed to render the launchd plist.\n' >&2
  exit 1
fi

/usr/bin/plutil -lint "$rendered" >/dev/null || {
  printf 'ERROR: rendered plist is invalid.\n' >&2
  exit 1
}
cp "$rendered" "$target"
printf 'INFO: installed launchd plist: %s\n' "$target"

launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || :
if launchctl bootstrap "gui/$(id -u)" "$target"; then
  launchctl kickstart "gui/$(id -u)/$label"
  printf 'INFO: daemon loaded: %s\n' "$label"
else
  printf 'ERROR: launchctl bootstrap failed for %s\n' "$label" >&2
  exit 1
fi

cat <<EOF

subswitch is installed and running in OBSERVE-ONLY mode ("enforce": false).

Next:
  1. Enroll your accounts        -> docs/ceremony.md
  2. Check the install           -> bin/doctor.sh --quick
  3. Watch it decide (no action) -> tail -f ~/.config/subswitch/logs/subswitchd.log
  4. Turn on switching only after the ceremony verification passes.

Read docs/danger-commands.md before running any codex login/logout command.
EOF
