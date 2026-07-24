#!/usr/bin/env bash
# Build thin Codex account homes: shared state is symlinked, auth stays local.

set -u

canonical="${SUBSWITCH_CODEX_CANONICAL:-"$HOME/.codex"}"
homes_spec="${SUBSWITCH_HOMES:-"$HOME/.codex-2 $HOME/.codex-3"}"

if [[ ! -d "$canonical" ]]; then
  printf 'ERROR: canonical Codex home is not a directory: %s\n' "$canonical" >&2
  exit 1
fi

canonical="$(cd -P "$canonical" && pwd)"

is_private() {
  case "$1" in
    auth.json|installation_id|internal_storage.json|version.json|log|tmp|mcp-oauth-locks|shell_snapshots|process_manager|node_repl|config.toml.bak.*)
      return 0
      ;;
  esac
  return 1
}

is_transient() {
  case "$1" in
    *.sqlite-shm|*.sqlite-wal|.DS_Store|.tmp|*.bak|*.bak.*|.*.tmp-*|*.tmp-*)
      return 0
      ;;
  esac
  return 1
}

remove_transient_symlink() {
  local destination="$1"
  if [[ -L "$destination" ]]; then
    printf 'INFO: removing transient symlink: %s\n' "$destination"
    rm -f "$destination" || printf 'WARN: could not remove transient symlink: %s\n' "$destination" >&2
  fi
}

is_shared_manifest_entry() {
  case "$1" in
    config.toml|AGENTS.md|skills|rules|memories*|goals_*.sqlite*|pets|automations|plugins|sessions|archived_sessions|session_index.jsonl|history.jsonl|models_cache.json|notify-complete.sh|generated_images|attachments|browser|computer-use|vendor_imports|sqlite|state_*.sqlite|logs_*.sqlite|cache|ambient-suggestions|transcription-history.jsonl)
      return 0
      ;;
  esac
  return 1
}

remove_private_symlink() {
  local destination="$1"
  if [[ -L "$destination" ]]; then
    printf 'INFO: removing private symlink: %s\n' "$destination"
    rm -f "$destination" || printf 'WARN: could not remove private symlink: %s\n' "$destination" >&2
  elif [[ -e "$destination" ]]; then
    printf 'WARN: leaving existing private entry untouched: %s\n' "$destination" >&2
  fi
}

link_shared_entry() {
  local source="$1"
  local destination="$2"

  if [[ -L "$destination" ]]; then
    if [[ "$(readlink "$destination")" == "$source" ]]; then
      return 0
    fi
    printf 'WARN: replacing existing symlink: %s\n' "$destination" >&2
    rm -f "$destination" || {
      printf 'WARN: could not replace symlink: %s\n' "$destination" >&2
      return 0
    }
  elif [[ -e "$destination" ]]; then
    printf 'WARN: leaving existing real entry untouched: %s\n' "$destination" >&2
    return 0
  fi

  ln -s "$source" "$destination" || printf 'WARN: could not create symlink: %s\n' "$destination" >&2
}

IFS=' :' read -r -a homes <<< "$homes_spec"
if (( ${#homes[@]} == 0 )); then
  printf 'ERROR: SUBSWITCH_HOMES did not contain any homes\n' >&2
  exit 1
fi

shopt -s dotglob nullglob
entries=("$canonical"/*)

for home in "${homes[@]}"; do
  [[ -n "$home" ]] || continue
  if [[ "$home" == "$canonical" ]]; then
    printf 'ERROR: thin home must not be the canonical home: %s\n' "$home" >&2
    exit 1
  fi

  mkdir -p "$home" || exit 1
  printf 'INFO: syncing thin Codex home: %s\n' "$home"

  for source in "${entries[@]}"; do
    name="${source##*/}"
    destination="$home/$name"

    if is_private "$name"; then
      remove_private_symlink "$destination"
    elif is_transient "$name"; then
      remove_transient_symlink "$destination"
    else
      if ! is_shared_manifest_entry "$name"; then
        printf 'WARN: unlisted canonical entry; sharing by symlink: %s\n' "$name" >&2
      fi
      link_shared_entry "$source" "$destination"
    fi
  done
done
