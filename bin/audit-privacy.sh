#!/usr/bin/env bash
# Privacy audit: refuse to publish anything that identifies a specific machine
# or a specific set of accounts.
#
# This repo is published from a working setup. That makes leakage the default
# failure mode, so the check is mechanical and runs against tracked content --
# and, with --history, against every blob ever committed.
#
#   bin/audit-privacy.sh            # audit the working tree (tracked files)
#   bin/audit-privacy.sh --history  # also audit every blob in git history
#
# Exit 0 = clean. Exit 1 = a finding. Findings are printed with file:line.
#
# Add your own patterns to EXTRA_DENY (space-separated ERE) when auditing a
# fork that was extracted from a different personal setup:
#   EXTRA_DENY='myname mycompany' bin/audit-privacy.sh

set -uo pipefail

repo_dir="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"

findings=0
scan_history=0
[[ "${1:-}" == "--history" ]] && scan_history=1

report() {
  findings=$((findings + 1))
  printf 'FAIL: %s\n' "$1"
  [[ -n "${2:-}" ]] && printf '%s\n' "$2" | sed 's/^/      /'
}

# --- pattern set ------------------------------------------------------------
# Real home directories and usernames.
PAT_HOME='/Users/[A-Za-z0-9._-]+'
# Real email addresses, excluding the reserved example domains (RFC 2606).
PAT_EMAIL='[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'
# Credentials of every shape we could plausibly touch.
PAT_SECRET='(sk-[A-Za-z0-9_-]{20,}|sbp_[A-Za-z0-9]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|ey[A-Za-z0-9_-]{30,}\.[A-Za-z0-9_-]{20,}\.|-----BEGIN [A-Z ]*PRIVATE KEY-----|Bearer [A-Za-z0-9._-]{30,})'
# A real UID baked into a launchctl domain or socket path.
PAT_UID='(gui/[0-9]{3,}|cmux-[0-9]{3,}\.sock)'
# Non-synthetic UUIDs (all-repeated-digit UUIDs are test fixtures).
PAT_UUID='[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'

allow_email() { grep -viE '@(example\.(com|org|net)|localhost|.*\.invalid)'; }
# Reserved synthetic values, used by fixtures and safe to publish:
#   home /Users/example   user "example"   UID 777
# Anything else of the same shape is treated as real and fails the audit.
allow_home() { grep -vE '/Users/example([^A-Za-z0-9._-]|$)'; }
allow_uid() { grep -vE '(gui/777|cmux-777\.sock)([^0-9]|$)'; }
allow_uuid() {
  # Drop UUIDs made of a single repeated hex digit per group (fixtures).
  grep -vE '\b(([0-9a-f])\2{7})-' | grep -vE '11111111-2222-3333-4444-555555555555'
}

tracked_files() { git ls-files 2>/dev/null || find . -type f -not -path './.git/*'; }
# This script necessarily contains the patterns it searches for, so it is
# excluded from the pattern checks. It is NOT excluded from the credential
# scan below.
tracked_files_noself() { tracked_files | grep -v '^bin/audit-privacy\.sh$'; }

# --- working tree -----------------------------------------------------------
printf '== auditing tracked files ==\n'

hits="$(tracked_files_noself | xargs grep -nHE "$PAT_HOME" 2>/dev/null | allow_home || :)"
[[ -n "$hits" ]] && report 'absolute home directory (/Users/<name>) present' "$hits"

hits="$(tracked_files_noself | xargs grep -nHoE "$PAT_EMAIL" 2>/dev/null | allow_email || :)"
[[ -n "$hits" ]] && report 'real email address present' "$hits"

hits="$(tracked_files | xargs grep -nHoE "$PAT_SECRET" 2>/dev/null || :)"
[[ -n "$hits" ]] && report 'credential-shaped string present' "$hits"

hits="$(tracked_files_noself | xargs grep -nHoE "$PAT_UID" 2>/dev/null | allow_uid || :)"
[[ -n "$hits" ]] && report 'hardcoded numeric UID present (use $(id -u))' "$hits"

hits="$(tracked_files_noself | xargs grep -nHoE "$PAT_UUID" 2>/dev/null | allow_uuid || :)"
[[ -n "$hits" ]] && report 'non-synthetic UUID present (account/org id?)' "$hits"

if [[ -n "${EXTRA_DENY:-}" ]]; then
  for term in $EXTRA_DENY; do
    hits="$(tracked_files_noself | xargs grep -nHiE "$term" 2>/dev/null || :)"
    [[ -n "$hits" ]] && report "EXTRA_DENY term present: $term" "$hits"
  done
fi

# A rendered plist must never be committed; only the template belongs here.
hits="$(tracked_files | grep -E '^launchd/.*\.plist$' || :)"
[[ -n "$hits" ]] && report 'a rendered .plist is tracked (commit only the .template)' "$hits"

# Live state must never be committed.
hits="$(tracked_files | grep -E '(^|/)(config\.json|state\.json|codex-current|auth\.json|.*\.log)$' || :)"
[[ -n "$hits" ]] && report 'runtime state or logs are tracked' "$hits"

# --- history ----------------------------------------------------------------
if (( scan_history )); then
  printf '== auditing every blob in git history ==\n'
  blobs="$(git rev-list --all --objects 2>/dev/null | awk '{print $1}' | sort -u)"
  hist="$(
    for o in $blobs; do
      [[ "$(git cat-file -t "$o" 2>/dev/null)" == blob ]] || continue
      git cat-file -p "$o" 2>/dev/null
    done | grep -aoE "$PAT_HOME|$PAT_SECRET" | allow_home | sort -u
  )"
  [[ -n "$hist" ]] && report 'history contains a home path or credential' "$hist"

  hist="$(
    for o in $blobs; do
      [[ "$(git cat-file -t "$o" 2>/dev/null)" == blob ]] || continue
      git cat-file -p "$o" 2>/dev/null
    done | grep -aoE "$PAT_EMAIL" | allow_email | sort -u
  )"
  [[ -n "$hist" ]] && report 'history contains a real email address' "$hist"
fi

# --- verdict ----------------------------------------------------------------
printf '\n'
if (( findings == 0 )); then
  printf 'PASS: no identifying information found.\n'
  exit 0
fi
printf 'FAILED: %d finding(s). Do not publish until these are resolved.\n' "$findings"
exit 1
