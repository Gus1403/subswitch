#!/usr/bin/env bash
# Dry-run coverage for the Codex account-selection shim.  The shim execs the
# absolute /opt/homebrew/bin/codex path, so selection is asserted from its
# shim.log rather than attempting to replace the executable through PATH.

set -euo pipefail

repo_dir="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fixture_root="$(mktemp -d /tmp/subswitch-shim.XXXXXX)"
config_root="$fixture_root/config/subswitch"
fake_path="$fixture_root/path"
fake_codex_log="$fixture_root/fake-codex.log"
selected_home="$fixture_root/selected-home"
missing_auth_home="$fixture_root/missing-auth-home"
override_home="$fixture_root/operator-override"

cleanup() {
  rm -rf "$fixture_root"
}
trap cleanup EXIT

mkdir -p "$config_root" "$fixture_root/home" "$fake_path" \
  "$selected_home" "$missing_auth_home" "$override_home"
touch "$selected_home/auth.json"

# Keep a PATH stand-in available to prove the test environment has no need to
# invoke a generic `codex`.  bin/codex intentionally bypasses it by using its
# hard-coded real binary; each invocation below is limited to `--version`.
printf '%s\n' '#!/usr/bin/env bash' \
  'printf "%s\\n" "$*" >> "$SUBSWITCH_FAKE_CODEX_LOG"' > "$fake_path/codex"
chmod +x "$fake_path/codex"

run_default() {
  env -u CODEX_HOME \
    HOME="$fixture_root/home" \
    SUBSWITCH_CONFIG_HOME="$config_root" \
    SUBSWITCH_FAKE_CODEX_LOG="$fake_codex_log" \
    PATH="$fake_path:$PATH" \
    "$repo_dir/bin/codex" --version >/dev/null
}

run_override() {
  HOME="$fixture_root/home" \
    CODEX_HOME="$override_home" \
    SUBSWITCH_CONFIG_HOME="$config_root" \
    SUBSWITCH_FAKE_CODEX_LOG="$fake_codex_log" \
    PATH="$fake_path:$PATH" \
    "$repo_dir/bin/codex" --version >/dev/null
}

last_log_line() {
  tail -n 1 "$config_root/logs/shim.log"
}

assert_default() {
  local line
  line="$(last_log_line)"
  [[ "$line" == *'home=\<default\>'* ]] || {
    printf 'expected default home in shim log, got: %s\n' "$line" >&2
    return 1
  }
}

# No pointer: fall through to the real Codex default (with a temporary HOME).
run_default
assert_default

# A valid one-line pointer selects only a home containing auth.json.
printf '%s\n' "$selected_home" > "$config_root/codex-current"
run_default
[[ "$(last_log_line)" == *"home=$selected_home"* ]]

# A missing auth.json makes the pointer invalid and falls through to default.
printf '%s\n' "$missing_auth_home" > "$config_root/codex-current"
run_default
assert_default

# A second pointer line (including any nonempty value) is corruption.
printf '%s\n%s\n' "$selected_home" 'unexpected second line' > "$config_root/codex-current"
run_default
assert_default

# An explicit operator override is neither replaced nor read from the pointer.
printf '%s\n' "$selected_home" > "$config_root/codex-current"
run_override
[[ "$(last_log_line)" == *"home=$override_home"* ]]

[[ "$(wc -l < "$config_root/logs/shim.log")" -eq 5 ]]
[[ ! -e "$fake_codex_log" ]]

printf 'PASS: S8 Codex shim selection and logging fixtures\n'
