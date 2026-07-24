#!/usr/bin/env bash
# Ground-truth canary for the ChatGPT app's computer-use capability.

set -uo pipefail

shim="$HOME/.local/bin/codex"
answer_file="$(mktemp /tmp/subswitch-computer-use.answer.XXXXXX)"
log_file="$(mktemp /tmp/subswitch-computer-use.log.XXXXXX)"
timed_out=0

cleanup() {
  rm -f "$answer_file" "$log_file"
}
trap cleanup EXIT

# Google Chrome is the ONLY pre-approved app in the headless approval store
# (ComputerUseAppApprovals.json) — the probe must target it explicitly or the
# model may pick an unapproved app (Finder, etc.) and fail spuriously.
prompt='Use your computer-use tool to take a screenshot of the Google Chrome window (bundle id com.google.Chrome) — do NOT target any other app. Reply exactly CU-OK if the Chrome screenshot succeeded, or exactly CU-FAIL:<reason> if it did not (including if Chrome is not running). Do not open or close any app. Do not reply with anything else.'

if [[ ! -x "$shim" ]]; then
  printf 'FAIL: computer-use screenshot probe\n'
  printf 'answer: CU-FAIL: Codex shim is not executable: %s\n' "$shim"
  printf 'Fix: cd %q && ./install.sh\n' "$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  exit 1
fi

if command -v timeout >/dev/null 2>&1; then
  timeout 240 "$shim" exec -s read-only \
    -c 'model="gpt-5.6-sol"' \
    -c 'model_reasoning_effort="high"' \
    -o "$answer_file" "$prompt" \
    < /dev/null > "$log_file" 2>&1
  probe_status=$?
elif command -v gtimeout >/dev/null 2>&1; then
  gtimeout 240 "$shim" exec -s read-only \
    -c 'model="gpt-5.6-sol"' \
    -c 'model_reasoning_effort="high"' \
    -o "$answer_file" "$prompt" < /dev/null > "$log_file" 2>&1
  probe_status=$?
else
  "$shim" exec -s read-only \
    -c 'model="gpt-5.6-sol"' \
    -c 'model_reasoning_effort="high"' \
    -o "$answer_file" "$prompt" \
    < /dev/null > "$log_file" 2>&1 &
  probe_pid=$!
  probe_status=0
  for _ in {1..240}; do
    if ! kill -0 "$probe_pid" 2>/dev/null; then
      wait "$probe_pid" || probe_status=$?
      break
    fi
    sleep 1
  done
  if kill -0 "$probe_pid" 2>/dev/null; then
    timed_out=1
    kill "$probe_pid" 2>/dev/null || :
    wait "$probe_pid" || :
    probe_status=124
  fi
fi

if grep -qx 'CU-OK' "$answer_file" 2>/dev/null; then
  printf 'PASS: computer-use screenshot probe\n'
  exit 0
fi

printf 'FAIL: computer-use screenshot probe'
if (( timed_out == 1 || probe_status == 124 )); then
  printf ' (timed out after 240s)'
fi
printf '\nanswer:\n'
if [[ -s "$answer_file" ]]; then
  cat "$answer_file"
else
  printf '<empty>\n'
fi
printf 'log tail:\n'
tail -n 40 "$log_file" 2>/dev/null || :
printf '%s\n' 'Fix (in likelihood order):' \
  '  1. Chrome not approved for headless use: check ~/Library/Group Containers/2DC432GLL2.com.openai.sky.CUAService/Library/Application Support/Software/ComputerUseAppApprovals.json contains {"approvedBundleIdentifiers":["com.google.Chrome"]} — or approve via ChatGPT app: Computer Use task on Chrome → "Always allow" (Settings → Computer Use → Always-allowed apps). See docs/runbook.md.' \
  '  2. Google Chrome is not running: start it and retry.' \
  '  3. Screen Recording TCC lost (after a ChatGPT app update): quit+reopen the ChatGPT app, trigger a computer-use action in its UI, approve the prompt (System Settings → Privacy & Security → Screen Recording; remove+re-add stale entry if no prompt).' \
  '  4. computer-use MCP disabled in ~/.codex/config.toml: the subswitch daemon auto-reasserts it; run a tick.'
exit 1
