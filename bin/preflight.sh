#!/usr/bin/env bash
# Read-only prerequisite check. Run this BEFORE ./install.sh.
#
# Every check prints PASS, WARN, or FAIL. FAIL means install.sh cannot work
# yet; each one carries the exact command that fixes it. WARN means optional
# or degraded, not blocking.
#
# This script never writes anything, never logs in, and never touches an
# account home.

set -uo pipefail

failures=0
warnings=0

pass() { printf 'PASS: %s\n' "$1"; }
warn() { printf 'WARN: %s\n  Note: %s\n' "$1" "$2"; warnings=$((warnings + 1)); }
fail() { printf 'FAIL: %s\n  Fix: %s\n' "$1" "$2"; failures=$((failures + 1)); }

# --- platform ---------------------------------------------------------------
if [[ "$(uname -s)" == "Darwin" ]]; then
  pass "macOS ($(sw_vers -productVersion 2>/dev/null || echo 'version unknown'))"
else
  fail 'macOS required' 'subswitch uses launchd and the macOS keychain; there is no Linux port.'
fi

if [[ -x /usr/bin/python3 ]]; then
  pass "system python3 ($(/usr/bin/python3 --version 2>&1))"
else
  fail 'system python3 at /usr/bin/python3' 'xcode-select --install'
fi

# --- required tooling -------------------------------------------------------
if command -v brew >/dev/null 2>&1; then
  pass "homebrew ($(brew --version 2>/dev/null | head -1))"
else
  warn 'homebrew not found' 'Only needed to install codex/codexbar the easy way. See https://brew.sh'
fi

if command -v codex >/dev/null 2>&1; then
  pass "codex CLI ($(codex --version 2>/dev/null | head -1))"
else
  fail 'codex CLI not installed' 'npm i -g @openai/codex   (or: brew install codex)'
fi

if command -v claude >/dev/null 2>&1; then
  pass "claude CLI ($(claude --version 2>/dev/null | head -1))"
else
  warn 'claude CLI not installed' 'Only needed if you want Claude rotation: https://claude.com/claude-code'
fi

if command -v cswap >/dev/null 2>&1; then
  pass "cswap ($(cswap --version 2>/dev/null | head -1))"
else
  fail 'cswap (claude-swap) not installed -- required for Claude rotation' \
    'uv tool install claude-swap    (uv: https://docs.astral.sh/uv/)'
fi

if command -v codexbar >/dev/null 2>&1; then
  pass 'codexbar CLI'
else
  fail 'codexbar not installed -- the daemon reads Codex usage through it' \
    'brew install --cask codexbar'
fi

# --- PATH ordering ----------------------------------------------------------
local_bin="$HOME/.local/bin"
IFS=':' read -r -a path_entries <<< "${PATH:-}"
local_position=-1
homebrew_position=-1
for index in "${!path_entries[@]}"; do
  [[ "${path_entries[$index]}" == "$local_bin" ]] && local_position=$index
  [[ "${path_entries[$index]}" == "/opt/homebrew/bin" ]] && homebrew_position=$index
done
if (( local_position >= 0 )) && (( homebrew_position < 0 || local_position < homebrew_position )); then
  pass "PATH: $local_bin precedes /opt/homebrew/bin"
else
  fail "PATH: $local_bin must precede /opt/homebrew/bin (the codex shim lives there)" \
    "add   export PATH=\"\$HOME/.local/bin:\$PATH\"   to your shell profile, then reopen the shell"
fi

if [[ -e "$local_bin/codex" && ! -L "$local_bin/codex" ]]; then
  fail "$local_bin/codex exists and is a real file, not a symlink" \
    "move it aside: mv \"$local_bin/codex\" \"$local_bin/codex.bak\""
fi

# --- canonical Codex home ---------------------------------------------------
if [[ -d "$HOME/.codex" ]]; then
  if [[ -f "$HOME/.codex/auth.json" ]]; then
    pass '~/.codex exists and is logged in (canonical account)'
  else
    fail '~/.codex exists but has no auth.json' \
      'codex login    (log in your FIRST account before enrolling any others)'
  fi
else
  fail '~/.codex does not exist' 'run codex once and log in: codex login'
fi

# --- conflicts --------------------------------------------------------------
if launchctl print "gui/$(id -u)/com.subswitch.daemon" >/dev/null 2>&1; then
  warn 'com.subswitch.daemon is already loaded' \
    'install.sh will replace it; that is expected on a re-install.'
fi

existing_homes=0
for home in "$HOME"/.codex-*; do
  [[ -d "$home" ]] && existing_homes=$((existing_homes + 1))
done
if (( existing_homes > 0 )); then
  warn "$existing_homes thin Codex home(s) already exist" \
    'make-codex-homes.sh never overwrites a real file in an existing home; auth.json is always left alone.'
fi

# --- verdict ----------------------------------------------------------------
printf '\n'
if (( failures > 0 )); then
  printf 'PREFLIGHT FAILED: %d blocking issue(s), %d warning(s). Fix the FAILs, then re-run.\n' \
    "$failures" "$warnings"
  exit 1
fi
printf 'PREFLIGHT PASSED (%d warning(s)). Next: ./install.sh, then docs/ceremony.md\n' "$warnings"
exit 0
