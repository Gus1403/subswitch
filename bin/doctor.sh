#!/usr/bin/env bash
# Read-only capability checks for the subswitch installation.

set -uo pipefail

repo_dir="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
config_dir="${SUBSWITCH_CONFIG_HOME:-"$HOME/.config/subswitch"}"
quick=0
failures=0

case "${1-}" in
  "") ;;
  --quick) quick=1 ;;
  *)
    printf 'Usage: %s [--quick]\n' "${BASH_SOURCE[0]}" >&2
    exit 2
    ;;
esac

pass() {
  printf 'PASS: %s\n' "$1"
}

fail() {
  printf 'FAIL: %s\n  Fix: %s\n' "$1" "$2"
  failures=$((failures + 1))
}

shell_quote() {
  printf '%q' "$1"
}

shim="$HOME/.local/bin/codex"
expected_shim="$repo_dir/bin/codex"
shim_target=""
if [[ -L "$shim" ]]; then
  shim_target="$(readlink "$shim" 2>/dev/null || :)"
fi
IFS=':' read -r -a path_entries <<< "${PATH:-}"
local_position=-1
homebrew_position=-1
for index in "${!path_entries[@]}"; do
  [[ "${path_entries[$index]}" == "$HOME/.local/bin" ]] && local_position=$index
  [[ "${path_entries[$index]}" == "/opt/homebrew/bin" ]] && homebrew_position=$index
done
if [[ "$shim_target" == "$expected_shim" && $local_position -ge 0 && ( $homebrew_position -lt 0 || $local_position -lt $homebrew_position ) ]]; then
  pass 'Codex shim symlink and PATH precedence'
else
  fail 'Codex shim symlink and PATH precedence' \
    "cd $(shell_quote "$repo_dir") && ./install.sh; export PATH=\"$HOME/.local/bin:\$PATH\""
fi

pointer="$config_dir/codex-current"
if [[ ! -e "$pointer" ]]; then
  pass 'Codex pointer (absent; default home is in use)'
else
  mapfile -t pointer_lines < "$pointer" 2>/dev/null || pointer_lines=()
  if (( ${#pointer_lines[@]} == 1 )) && [[ "${pointer_lines[0]}" == /* && -d "${pointer_lines[0]}" && -f "${pointer_lines[0]}/auth.json" ]]; then
    pass "Codex pointer (${pointer_lines[0]})"
  else
    fail 'Codex pointer names an authenticated directory' \
      "home=\"$HOME/.codex-2\"; test -f \"\$home/auth.json\" && printf '%s\\n' \"\$home\" > $(shell_quote "$pointer")"
  fi
fi

launchd_target="gui/$(id -u)/com.subswitch.daemon"
if launchctl print "$launchd_target" >/dev/null 2>&1; then
  pass 'launchd daemon com.subswitch.daemon is running'
else
  fail 'launchd daemon com.subswitch.daemon is running' \
    "launchctl bootstrap gui/$(id -u) \"$HOME/Library/LaunchAgents/com.subswitch.daemon.plist\" && launchctl kickstart $launchd_target"
fi

cswap_json=""
if cswap_json="$(cswap list --json 2>/dev/null)"; then
  if cswap_report="$(/usr/bin/python3 -c '
import json
import sys
try:
    payload = json.load(sys.stdin)
    accounts = payload["accounts"]
    if not isinstance(accounts, list):
        raise ValueError("accounts is not a list")
except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
    print("invalid cswap JSON: %s" % error)
    sys.exit(2)
bad = []
for account in accounts:
    if not isinstance(account, dict) or account.get("usageStatus") != "ok":
        slot = account.get("number", "?") if isinstance(account, dict) else "?"
        status = account.get("usageStatus", "missing") if isinstance(account, dict) else "invalid account"
        bad.append("slot %s usageStatus=%s" % (slot, status))
if bad:
    print("; ".join(bad))
    sys.exit(1)
' <<< "$cswap_json")"; then
    pass 'cswap usage JSON (all accounts usageStatus=ok)'
  else
    fail "cswap usage JSON (${cswap_report:-unparseable})" \
      'Run a fresh /login for the affected account, then: cswap add --slot N'
  fi
else
  fail 'cswap list --json' 'Run a fresh /login for the affected account, then: cswap add --slot N'
fi

usage_failures=0
usage_notes=()
for home in "$HOME/.codex" "$HOME/.codex-2" "$HOME/.codex-3"; do
  [[ -f "$home/auth.json" ]] || continue
  usage_json=""
  if usage_json="$(CODEX_HOME="$home" /opt/homebrew/bin/codexbar usage --provider codex --format json 2>/dev/null)" \
    && /usr/bin/python3 -c 'import json, sys; json.load(sys.stdin)' <<< "$usage_json" >/dev/null 2>&1; then
    usage_notes+=("$home")
  else
    usage_failures=$((usage_failures + 1))
    usage_notes+=("$home (failed)")
  fi
done
if (( usage_failures == 0 )); then
  if (( ${#usage_notes[@]} == 0 )); then
    pass 'CodexBar usage JSON (no authenticated Codex homes to check)'
  else
    pass "CodexBar usage JSON (${usage_notes[*]})"
  fi
else
  fail "CodexBar usage JSON (${usage_notes[*]})" \
    'Verify the affected home has a fresh auth.json, then rerun: CODEX_HOME=<home> /opt/homebrew/bin/codexbar usage --provider codex --format json'
fi

config_toml="$HOME/.codex/config.toml"
config_status="missing"
if [[ -f "$config_toml" ]]; then
  config_status="$(/usr/bin/python3 -c '
import sys
path = sys.argv[1]
section = False
for line in open(path, encoding="utf-8"):
    bare = line.rstrip()
    if bare == "[mcp_servers.computer-use]":
        section = True
        continue
    if section and bare.startswith("["):
        break
    if section and bare == "enabled = true":
        print("enabled")
        raise SystemExit(0)
print("not enabled")
raise SystemExit(1)
' "$config_toml" 2>/dev/null || :)"
fi
if [[ "$config_status" == "enabled" ]]; then
  pass 'computer-use MCP server is enabled'
else
  fail 'computer-use MCP server is enabled' \
    "The subswitch daemon auto-reasserts it; run a tick: $(shell_quote "$repo_dir/daemon/subswitchd.py") --once --dry-run"
fi

canonical_pairing="$HOME/.codex/chrome-native-hosts-v2.json"
pairing_failures=0
pairing_notes=()
for home in "$HOME/.codex-2" "$HOME/.codex-3"; do
  pairing="$home/chrome-native-hosts-v2.json"
  if [[ -L "$pairing" && "$(readlink "$pairing" 2>/dev/null || :)" == "$canonical_pairing" ]]; then
    pairing_notes+=("$home")
  else
    pairing_failures=$((pairing_failures + 1))
    pairing_notes+=("$home (not canonical symlink)")
  fi
done
if (( pairing_failures == 0 )); then
  pass "Chrome pairing symlinks (${pairing_notes[*]})"
else
  fail "Chrome pairing symlinks (${pairing_notes[*]})" \
    "$(shell_quote "$repo_dir/bin/make-codex-homes.sh")"
fi

if (( quick )); then
  pass 'computer-use screenshot probe (skipped by --quick)'
elif "$repo_dir/bin/probe-computer-use.sh"; then
  pass 'computer-use screenshot probe'
else
  fail 'computer-use screenshot probe' "$(shell_quote "$repo_dir/bin/probe-computer-use.sh")"
fi

if (( failures > 0 )); then
  printf 'FAIL: capability doctor found %d failing check(s)\n' "$failures"
  exit 1
fi
printf 'PASS: capability doctor\n'
