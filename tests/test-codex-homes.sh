#!/usr/bin/env bash
# Non-destructive fixture coverage for S4.  It never reads or writes ~/.codex.

set -euo pipefail

repo_dir="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fixture_root="$(mktemp -d /tmp/subswitch-s4.XXXXXX)"
trap 'rm -rf "$fixture_root"' EXIT

canonical="$fixture_root/canonical"
thin_one="$fixture_root/thin-one"
thin_two="$fixture_root/thin-two"
config_root="$fixture_root/config/subswitch"
mkdir -p "$canonical/skills" "$canonical/log" "$canonical/tmp" "$canonical/sessions" "$canonical/cache"
mkdir -p "$thin_one"
touch "$canonical/auth.json" "$canonical/installation_id" "$canonical/config.toml" \
  "$canonical/AGENTS.md" "$canonical/state_usage.sqlite" "$canonical/example.sqlite-wal" \
  "$canonical/config.toml.bak.1" "$canonical/unlisted-new-entry"
printf 'local override\n' > "$thin_one/config.toml"
canonical_real="$(cd -P "$canonical" && pwd)"

# Stub Codex binary: the shim assertions below only inspect its own trace, so
# this suite must not depend on a real Codex install (CI runners have none).
printf '%s\n' '#!/usr/bin/env bash' 'exit 0' > "$fixture_root/stub-codex"
chmod +x "$fixture_root/stub-codex"

builder_output="$(SUBSWITCH_CODEX_CANONICAL="$canonical" SUBSWITCH_HOMES="$thin_one $thin_two" \
  "$repo_dir/bin/make-codex-homes.sh" 2>&1)"
printf '%s\n' "$builder_output"

[[ -f "$thin_one/config.toml" && ! -L "$thin_one/config.toml" ]]
[[ -L "$thin_two/config.toml" ]]
[[ "$(readlink "$thin_two/config.toml")" == "$canonical_real/config.toml" ]]
[[ -L "$thin_two/skills" && -L "$thin_two/sessions" && -L "$thin_two/state_usage.sqlite" ]]
[[ -L "$thin_two/unlisted-new-entry" ]]
[[ ! -e "$thin_two/auth.json" && ! -e "$thin_two/installation_id" ]]
[[ ! -e "$thin_two/log" && ! -e "$thin_two/tmp" ]]
[[ ! -e "$thin_two/example.sqlite-wal" && ! -e "$thin_two/config.toml.bak.1" ]]
[[ "$builder_output" == *'WARN: unlisted canonical entry; sharing by symlink: unlisted-new-entry'* ]]

SUBSWITCH_CODEX_CANONICAL="$canonical" SUBSWITCH_HOMES="$thin_one $thin_two" \
  "$repo_dir/bin/make-codex-homes.sh" >/dev/null

mkdir -p "$fixture_root/selected-home" "$config_root"
touch "$fixture_root/selected-home/auth.json"
printf '%s\n' "$fixture_root/selected-home" > "$config_root/codex-current"

shim_trace="$(HOME="$fixture_root/home" SUBSWITCH_CONFIG_HOME="$config_root" SUBSWITCH_REAL_CODEX="$fixture_root/stub-codex" bash -x "$repo_dir/bin/codex" --version 2>&1 || :)"
printf '%s\n' "$shim_trace"
[[ "$shim_trace" == *"export CODEX_HOME=$fixture_root/selected-home"* ]]
[[ -f "$config_root/logs/shim.log" ]]
[[ "$(wc -l < "$config_root/logs/shim.log")" -eq 1 ]]
[[ "$(< "$config_root/logs/shim.log")" == *"home=$fixture_root/selected-home"* ]]

printf '%s\n' "$fixture_root/missing-home" > "$config_root/codex-current"
invalid_trace="$(HOME="$fixture_root/home" SUBSWITCH_CONFIG_HOME="$config_root" SUBSWITCH_REAL_CODEX="$fixture_root/stub-codex" bash -x "$repo_dir/bin/codex" --version 2>&1 || :)"
[[ "$invalid_trace" != *'export CODEX_HOME='* ]]

printf '%s\n%s\n' "$fixture_root/selected-home" 'unexpected second line' > "$config_root/codex-current"
corrupt_trace="$(HOME="$fixture_root/home" SUBSWITCH_CONFIG_HOME="$config_root" SUBSWITCH_REAL_CODEX="$fixture_root/stub-codex" bash -x "$repo_dir/bin/codex" --version 2>&1 || :)"
[[ "$corrupt_trace" != *'export CODEX_HOME='* ]]

override_trace="$(HOME="$fixture_root/home" SUBSWITCH_CONFIG_HOME="$config_root" SUBSWITCH_REAL_CODEX="$fixture_root/stub-codex" CODEX_HOME="$fixture_root/explicit" bash -x "$repo_dir/bin/codex" --version 2>&1 || :)"
[[ "$override_trace" != *'read -r selected_home'* ]]

printf 'PASS: S4 Codex home builder and shim pointer fixtures\n'
