#!/usr/bin/env bash
# Full verification suite. Every test is hermetic: it builds its own fixture
# root under $TMPDIR and overrides HOME / SUBSWITCH_CONFIG_HOME, so running
# this never reads or writes your real accounts, homes, config, or launchd.
#
# Uses macOS's system Python (3.9) deliberately -- the daemon must run under
# /usr/bin/python3 with no third-party dependencies.

set -euo pipefail

repo_dir="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"

/usr/bin/python3 -m unittest tests/test_policy.py tests/test_subswitchd_unit.py
tests/test_shim.sh
tests/test-codex-homes.sh
tests/test-plist-render.sh

printf 'PASS: subswitch verification suite\n'
