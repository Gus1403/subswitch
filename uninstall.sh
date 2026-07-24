#!/usr/bin/env bash
# Remove only the shim installed by this checkout.  Account homes are retained.

set -u

repo_dir="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
shim_link="$HOME/.local/bin/codex"
launchd_label="com.subswitch.daemon"
launchd_target="$HOME/Library/LaunchAgents/$launchd_label.plist"

launchctl bootout "gui/$(id -u)/$launchd_label" 2>/dev/null || :
if [[ -e "$launchd_target" ]]; then
  rm "$launchd_target"
  printf 'INFO: removed launchd plist: %s\n' "$launchd_target"
else
  printf 'INFO: no launchd plist to remove at %s\n' "$launchd_target"
fi

if [[ -L "$shim_link" ]]; then
  target="$(readlink "$shim_link")"
  case "$target" in
    /*) resolved_target="$target" ;;
    *) resolved_target="$(cd -P "$(dirname "$shim_link")/$(dirname "$target")" 2>/dev/null && pwd)/$(basename "$target")" ;;
  esac

  if [[ "$resolved_target" == "$repo_dir/"* ]]; then
    rm "$shim_link"
    printf 'INFO: removed Codex shim: %s\n' "$shim_link"
  else
    printf 'INFO: leaving Codex symlink not owned by this repo: %s\n' "$shim_link"
  fi
else
  printf 'INFO: no Codex shim symlink to remove at %s\n' "$shim_link"
fi

printf 'Account homes were intentionally kept. Remove the thin homes (~/.codex-2, ~/.codex-3, ...) manually only after confirming they are no longer needed.\n'
printf 'The canonical ~/.codex was not touched.\n'
printf 'Configuration, state, and logs under ~/.config/subswitch were kept.\n'
