#!/usr/bin/python3
"""subswitch usage poller, policy runner, and Codex session watcher."""

import argparse
import base64
import dataclasses
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from policy import (
    AccountEvaluation,
    AccountSnapshot,
    Decision,
    NoneDecision,
    NotifyDecision,
    PolicyConfig,
    PolicyResult,
    PolicyState,
    ProviderSnapshot,
    RedeemDecision,
    ResetCredit,
    SwitchDecision,
    Window,
    WindowKind,
    evaluate,
    evaluate_credits,
)


REPO_DIR = Path(__file__).resolve().parent.parent
CONFIG_ROOT = Path(
    os.environ.get("SUBSWITCH_CONFIG_HOME", "~/.config/subswitch")
).expanduser()
CONFIG_PATH = CONFIG_ROOT / "config.json"
STATE_PATH = CONFIG_ROOT / "state.json"
LOG_PATH = CONFIG_ROOT / "logs" / "subswitchd.log"
CODEX_AUTH_BACKUP_DIR = CONFIG_ROOT / "auth-backups" / "codex"
EXAMPLE_CONFIG = REPO_DIR / "config.example.json"
LOG_LIMIT_BYTES = 5 * 1024 * 1024
USAGE_LIMIT_TEXT = "You've hit your usage limit"
CMUX_APP_BIN = "/Applications/cmux.app/Contents/Resources/bin/cmux"
STOP_REQUESTED = False
MISSING_COMPUTER_USE_SECTION_LOGGED = False


def utc_stamp(now: Optional[float] = None) -> str:
    value = time.time() if now is None else now
    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".%s." % path.name, dir=str(path.parent))
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, str(path))
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


class Log:
    def __init__(self, path: Path) -> None:
        self.path = path

    def write(self, message: str, level: str = "INFO") -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._rotate_if_needed()
        line = "%s %-5s %s\n" % (utc_stamp(), level, message.replace("\n", "\\n"))
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)

    def _rotate_if_needed(self) -> None:
        try:
            if self.path.stat().st_size < LOG_LIMIT_BYTES:
                return
        except FileNotFoundError:
            return
        rotated = self.path.with_name(self.path.name + ".1")
        try:
            rotated.unlink()
        except FileNotFoundError:
            pass
        os.replace(str(self.path), str(rotated))


def default_state() -> Dict[str, Any]:
    return {
        "schemaVersion": 1,
        "updatedAt": None,
        "providers": {
            "claude": dataclasses.asdict(PolicyState()),
            "codex": dataclasses.asdict(PolicyState()),
        },
        "snapshots": {},
        "notifications": {},
        "codexBackoff": {},
        "codexIdentities": {},
        "claudeIdentities": {},
        "claudeInvisible": {},
        "deadAuth": {},
        "panes": {},
        "walledPids": {},
        "lastDecisions": [],
    }


def load_json(path: Path, fallback: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return fallback


def load_json_str(content: str) -> Any:
    """Parse a JSON string, returning {} on any error (never raises)."""
    try:
        return json.loads(content)
    except (ValueError, TypeError):
        return {}


def merge_state(loaded: Any) -> Dict[str, Any]:
    state = default_state()
    if not isinstance(loaded, dict):
        return state
    if loaded.get("updatedAt") is None or isinstance(loaded.get("updatedAt"), (int, float)):
        state["updatedAt"] = loaded.get("updatedAt")
    for key in (
        "snapshots",
        "notifications",
        "codexBackoff",
        "codexIdentities",
        "claudeIdentities",
        "claudeInvisible",
        "deadAuth",
        "panes",
        "walledPids",
        "lastDecisions",
    ):
        if key in loaded and isinstance(loaded[key], type(state[key])):
            state[key] = loaded[key]
    providers = loaded.get("providers")
    if isinstance(providers, dict):
        for provider in ("claude", "codex"):
            value = providers.get(provider)
            if isinstance(value, dict):
                state["providers"][provider].update(value)
    return state


def ensure_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        with EXAMPLE_CONFIG.open("r", encoding="utf-8") as handle:
            content = handle.read()
        atomic_write(CONFIG_PATH, content)
    config = load_json(CONFIG_PATH, None)
    if not isinstance(config, dict):
        raise RuntimeError("config is not a JSON object: %s" % CONFIG_PATH)
    return config


def run_command(
    arguments: Sequence[str],
    env: Optional[Dict[str, str]] = None,
    timeout: float = 30.0,
) -> Tuple[int, str, str]:
    try:
        completed = subprocess.run(
            list(arguments),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            # `ps eww` reproduces other processes' argv/environment verbatim, which
            # is arbitrary bytes — a single non-UTF-8 byte anywhere on the machine
            # would otherwise raise UnicodeDecodeError and kill the whole tick.
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return completed.returncode, completed.stdout, completed.stderr
    except (OSError, UnicodeDecodeError, subprocess.TimeoutExpired) as error:
        return 127, "", str(error)


def number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def iso_epoch(value: Any) -> Optional[float]:
    """Parse provider reset timestamps without requiring Python 3.11's Z support."""

    if not isinstance(value, str):
        return None
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            return None
        return parsed.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def guard_claude_identities(
    state: Dict[str, Any], now: float, log: Log, notifications_enabled: bool = True
) -> None:
    """Detect a cswap slot changing account, or two slots sharing one org.

    `cswap add` snapshots the ACTIVE keychain account into a slot; done at the
    wrong moment it can overwrite a slot with a different account or capture a
    duplicate (the identity registry that turned up `activeAccountNumber: None`
    while slot 5 was added). This surfaces both, mirroring the codex guard.
    """
    code, stdout, _ = run_command(["cswap", "list", "--json"])
    if code != 0:
        return
    try:
        payload = json.loads(stdout)
    except ValueError:
        return
    registry = state["claudeIdentities"]
    accounts = payload.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        return  # empty/partial response — never erase the baseline from it
    by_org: Dict[str, List[str]] = {}
    seen_slots = set()
    for raw in accounts:
        number = raw.get("number") if isinstance(raw, dict) else None
        if not isinstance(number, int):
            continue  # a missing/non-numeric slot must not become the "None" slot
        slot = str(number)
        org = raw.get("organizationUuid")
        email = raw.get("email") or raw.get("emailAddress")
        if not org:
            continue
        seen_slots.add(slot)
        by_org.setdefault(org, []).append(email or slot)
        previous = registry.get(slot)
        if isinstance(previous, dict) and previous.get("org") and previous["org"] != org:
            message = "Claude slot %s changed account: %s -> %s (a cswap add/login overwrote it)" % (
                slot, previous.get("email") or previous["org"], email or org,
            )
            log.write(message, "WARN")
            if notifications_enabled:
                notify(message, "claude-identity:%s:%s" % (slot, org), state, now, log)
        registry[slot] = {"org": org, "email": email or ""}
    for slot in list(registry):
        if slot not in seen_slots:
            registry.pop(slot, None)
    for org, holders in by_org.items():
        if len(holders) > 1:
            message = "Claude DUPLICATE account across slots %s (org %s) — one is redundant." % (
                ", ".join(holders), org[:8],
            )
            log.write(message, "WARN")
            if notifications_enabled:
                notify(message, "claude-duplicate:%s" % org, state, now, log)


# Per-slot cache of the last successful direct usage fetch + failure backoff, so
# a prolonged cswap glitch can't make the daemon hammer the rate-limited usage
# endpoint every 60s tick. {slot: {"at": epoch, "windows": (...), "backoff_until": epoch}}
_CLAUDE_DIRECT_CACHE: Dict[str, Dict[str, Any]] = {}
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_OAUTH_BETA = "oauth-2025-04-20"
CLAUDE_KEYCHAIN_SERVICE = "claude-swap"


def _claude_backup_access_token(slot: str, email: str, now: float) -> Optional[str]:
    """Read a cswap per-account backup token from the login Keychain.

    cswap stores each managed account under service ``claude-swap`` /
    ``account-<slot>-<email>``. Returns the access token only when it is present
    and not near expiry — a running/inactive account's refresh is owned by
    Claude Code / cswap, so we never refresh here, only read.
    """
    if not email:
        return None
    code, out, _ = run_command(
        ["/usr/bin/security", "find-generic-password", "-s", CLAUDE_KEYCHAIN_SERVICE,
         "-a", "account-%s-%s" % (slot, email), "-w"],
        timeout=10,
    )
    if code != 0 or not out.strip():
        return None
    try:
        oauth = json.loads(out).get("claudeAiOauth") or {}
    except (ValueError, AttributeError):
        return None
    token = oauth.get("accessToken")
    expires = oauth.get("expiresAt")  # epoch milliseconds
    if not token:
        return None
    if isinstance(expires, (int, float)) and expires / 1000.0 <= now + 60:
        return None  # expired/near-expiry; can't safely refresh from here
    return str(token)


def _claude_direct_windows(token: str) -> Tuple[Window, ...]:
    """Fetch usage straight from the Anthropic endpoint and parse it fully.

    The schema exposes the universal windows at the TOP LEVEL (``five_hour`` /
    ``seven_day``) and per-model windows in ``limits`` (weekly_scoped→Fable).
    Both sources are read (limits preferred, top-level as backstop). A result is
    accepted ONLY if it contains the 5h session window — otherwise a response
    that omitted 5h could make a session-blocked account look healthy. Returns
    an empty tuple on an incomplete/uncertain parse, so the caller keeps the
    account UNKNOWN rather than inventing headroom.
    """
    request = urllib.request.Request(
        CLAUDE_USAGE_URL,
        headers={
            "Authorization": "Bearer %s" % token,
            "anthropic-beta": CLAUDE_OAUTH_BETA,
            "User-Agent": "subswitch/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        data = json.loads(response.read().decode())
    if not isinstance(data, dict):
        return tuple()

    five: Optional[Window] = None
    weekly: Optional[Window] = None
    scoped: List[Window] = []
    limits = data.get("limits")
    for limit in limits if isinstance(limits, list) else []:
        if not isinstance(limit, dict):
            continue
        pct = number(limit.get("percent"))
        if pct is None:
            continue
        resets = iso_epoch(limit.get("resets_at"))
        kind = limit.get("kind")
        if kind == "session":
            five = Window("fiveHour", float(pct), WindowKind.FIVE_HOUR, resets)
        elif kind == "weekly_all":
            weekly = Window("sevenDay", float(pct), WindowKind.WEEKLY, resets)
        elif kind == "weekly_scoped":
            scope = limit.get("scope") if isinstance(limit.get("scope"), dict) else {}
            model = scope.get("model") if isinstance(scope.get("model"), dict) else {}
            name = model.get("display_name")
            if name:  # never invent an unidentified scoped window
                scoped.append(Window("scoped:%s" % name, float(pct), WindowKind.WEEKLY, resets))

    # Backstop from the top-level universal fields if limits omitted them.
    def _top(field: str, name: str, kind: WindowKind) -> Optional[Window]:
        block = data.get(field)
        if not isinstance(block, dict):
            return None
        pct = number(block.get("utilization"))
        if pct is None:
            return None
        return Window(name, float(pct), kind, iso_epoch(block.get("resets_at")))

    if five is None:
        five = _top("five_hour", "fiveHour", WindowKind.FIVE_HOUR)
    if weekly is None:
        weekly = _top("seven_day", "sevenDay", WindowKind.WEEKLY)

    # Reject an incomplete read: without the session window we cannot tell a
    # session-blocked account from a healthy one.
    if five is None:
        return tuple()
    windows = [five]
    if weekly is not None:
        windows.append(weekly)
    windows.extend(scoped)
    return tuple(windows)


def scan_invisible_claude_accounts(
    config: Dict[str, Any], state: Dict[str, Any], snapshot: ProviderSnapshot,
    now: float, log: Log, notifications_enabled: bool = True,
) -> None:
    """Alert when a Claude account stays unusable despite the direct fallback.

    The fallback recovers the observed cswap-cache glitch, but if some FUTURE
    failure mode ever escapes it (endpoint down, token expired past refresh,
    Keychain locked), a good account must never sit invisible to rotation
    SILENTLY. This surfaces any account that is unavailable for longer than a
    threshold, and clears the moment it recovers.
    """
    if not config.get("invisibleAccountNotify", True):
        return
    tracked = state["claudeInvisible"]
    threshold = float(config.get("invisibleThresholdSeconds", 300))
    stale_max = float(config.get("staleMaxSeconds", 900))
    identities = state.get("claudeIdentities", {})
    present = set()
    for account in snapshot.accounts:
        slot = account.account_id
        present.add(slot)
        # "Invisible" mirrors the policy's UNKNOWN test — an account is unusable
        # for rotation if it errored, has no windows, OR its data is stale — not
        # only when cswap set an explicit error.
        invisible = (
            account.error is not None
            or not account.windows
            or account.observed_at is None
            or (now - account.observed_at) > stale_max
        )
        if invisible:
            record = tracked.get(slot)
            if not isinstance(record, dict):
                tracked[slot] = {"since": now, "notified": False}
            elif not record.get("notified") and now - float(record.get("since", now)) >= threshold:
                email = (identities.get(slot) or {}).get("email") or slot
                minutes = int((now - float(record["since"])) / 60)
                message = (
                    "Claude account %s (%s) has been unavailable for %d min despite the "
                    "direct fallback — it cannot be used for rotation. Check it." % (slot, email, minutes)
                )
                log.write("INVISIBLE CLAUDE ACCOUNT: %s" % message, "WARN")
                if notifications_enabled:
                    notify(message, "claude-invisible:%s" % slot, state, now, log)
                record["notified"] = True
        else:
            tracked.pop(slot, None)  # recovered → allow a future alert
    for slot in list(tracked):
        if slot not in present:
            tracked.pop(slot, None)


def collect_claude(config: Dict[str, Any], now: float, log: Log) -> ProviderSnapshot:
    code, stdout, stderr = run_command(["cswap", "list", "--json"])
    if code != 0:
        log.write("Claude collection failed rc=%s error=%s" % (code, stderr.strip()), "WARN")
        return ProviderSnapshot(provider="claude", active_account_id="", accounts=tuple())
    try:
        payload = json.loads(stdout)
    except ValueError as error:
        log.write("Claude collection returned invalid JSON: %s" % error, "WARN")
        return ProviderSnapshot(provider="claude", active_account_id="", accounts=tuple())

    active = payload.get("activeAccountNumber")
    active_id = str(active) if active is not None else None
    accounts: List[AccountSnapshot] = []
    for raw in payload.get("accounts", []):
        identifier = str(raw.get("number"))
        status = raw.get("usageStatus")
        age = number(raw.get("usageAgeSeconds"))
        error = None if status == "ok" else "usageStatus=%s" % status
        windows: List[Window] = []
        usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
        if error is None:
            five = usage.get("fiveHour") if isinstance(usage.get("fiveHour"), dict) else {}
            weekly = usage.get("sevenDay") if isinstance(usage.get("sevenDay"), dict) else {}
            if number(five.get("pct")) is not None:
                windows.append(
                    Window(
                        "fiveHour", float(five["pct"]), WindowKind.FIVE_HOUR,
                        iso_epoch(five.get("resetsAt")),
                    )
                )
            if number(weekly.get("pct")) is not None:
                windows.append(
                    Window(
                        "sevenDay", float(weekly["pct"]), WindowKind.WEEKLY,
                        iso_epoch(weekly.get("resetsAt")),
                    )
                )
            scoped = usage.get("scoped") if isinstance(usage.get("scoped"), list) else []
            for item in scoped:
                if isinstance(item, dict) and number(item.get("pct")) is not None:
                    name = str(item.get("name") or "scoped")
                    windows.append(
                        Window(
                            "scoped:%s" % name, float(item["pct"]), WindowKind.WEEKLY,
                            iso_epoch(item.get("resetsAt")),
                        )
                    )
        if error is None and not windows:
            error = "missing policy usage windows"
        # Missing age is NOT treated as fresh — undated data could be arbitrarily
        # old, so it is aged past the policy stale window unless the fallback
        # produces a genuinely current reading below.
        stale_max = float(config.get("staleMaxSeconds", 900))
        observed_at = now - (age if age is not None else stale_max + 1.0)
        # Direct fallback runs on BOTH exception paths that hide a good account
        # from failover: (a) cswap reports it "unavailable" (list-cache glitch),
        # and (b) cswap reports it ok but with STALE/undated data — which the
        # policy would drop as unknown. Either way, read usage straight from the
        # endpoint so a usable account is never invisible to rotation.
        stale = age is None or (age or 0.0) > float(config.get("staleRefreshSeconds", 600))
        if (error is not None or stale) and config.get("directFallback", True):
            email = raw.get("email") or raw.get("emailAddress") or ""
            reason = status if error is not None else "stale %.0fs" % (age or 0.0)
            # Cache is keyed by (slot, email): a slot reassigned to a different
            # account never serves the prior account's cached usage.
            cache_key = "%s|%s" % (identifier, email)
            cached = _CLAUDE_DIRECT_CACHE.get(cache_key) or {}
            min_interval = float(config.get("directMinIntervalSeconds", 180))
            cache_windows = cached.get("windows")
            cache_at = float(cached.get("at", 0.0))
            cache_valid = cache_windows and (now - cache_at) <= stale_max
            fetched = False
            # Fetch only when the cache is not recent AND we're not backed off.
            if (now - cache_at) >= min_interval and now >= float(cached.get("backoff_until", 0.0)):
                try:
                    token = _claude_backup_access_token(identifier, str(email), now)
                    if token:
                        fallback = _claude_direct_windows(token)
                        if fallback:
                            windows = list(fallback)
                            error = None
                            observed_at = now
                            _CLAUDE_DIRECT_CACHE[cache_key] = {"at": now, "windows": fallback}
                            fetched = True
                            log.write(
                                "Claude direct fallback refreshed account %s (%s) from '%s'"
                                % (identifier, email, reason), "WARN"
                            )
                        else:
                            # Valid JSON but incomplete (e.g. missing 5h) — back
                            # off rather than hammer the endpoint every tick.
                            _CLAUDE_DIRECT_CACHE.setdefault(cache_key, {})["backoff_until"] = now + 300.0
                            log.write("Claude direct fallback incomplete for account %s — backing off"
                                      % identifier, "WARN")
                except urllib.error.HTTPError as http_error:
                    backoff = 600.0 if http_error.code == 429 else 120.0
                    _CLAUDE_DIRECT_CACHE.setdefault(cache_key, {})["backoff_until"] = now + backoff
                    log.write("Claude direct fallback HTTP %s for account %s — backing off %ss"
                              % (http_error.code, identifier, int(backoff)), "WARN")
                except (urllib.error.URLError, TimeoutError, OSError, ValueError) as fallback_error:
                    _CLAUDE_DIRECT_CACHE.setdefault(cache_key, {})["backoff_until"] = now + 120.0
                    log.write("Claude direct fallback failed for account %s: %s"
                              % (identifier, fallback_error), "WARN")
            # If we didn't fetch fresh, serve a still-valid cached observation so a
            # backoff never hides a policy-valid account.
            if not fetched and cache_valid:
                windows = list(cache_windows)
                error = None
                observed_at = cache_at
        accounts.append(
            AccountSnapshot(
                account_id=identifier,
                windows=tuple(windows),
                observed_at=observed_at,
                error=error,
            )
        )
    return ProviderSnapshot(provider="claude", active_account_id=active_id or "", accounts=tuple(accounts))


def codex_active_home(homes: Sequence[str]) -> str:
    default = str(Path(homes[0]).expanduser().resolve())
    try:
        lines = (CONFIG_ROOT / "codex-current").read_text(encoding="utf-8").splitlines()
        if len(lines) == 1 and os.path.isabs(lines[0]):
            return str(Path(lines[0]).resolve())
    except OSError:
        pass
    return default


def _codex_identity_from_auth(data: Any) -> Optional[Dict[str, str]]:
    """Extract {accountId, email} from a parsed codex auth.json payload.

    ``account_id`` comes from tokens.account_id, falling back to the id_token
    JWT claim; email is best-effort from the same JWT. Returns None when no
    account id can be recovered.
    """
    tokens = data.get("tokens") if isinstance(data, dict) else None
    tokens = tokens if isinstance(tokens, dict) else {}
    account_id = tokens.get("account_id")
    email = None
    parts = str(tokens.get("id_token") or "").split(".")
    if len(parts) == 3:
        try:
            payload = parts[1] + "=" * (-len(parts[1]) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
            email = claims.get("email")
            if not account_id:
                auth_claim = claims.get("https://api.openai.com/auth") or {}
                account_id = auth_claim.get("chatgpt_account_id")
        except (ValueError, TypeError):
            pass
    if not account_id:
        return None
    return {"accountId": str(account_id), "email": str(email or "")}


def codex_home_identity(home: str) -> Optional[Dict[str, str]]:
    """Read the account identity a codex home is logged into (no network).

    Returns None when the home has no parseable auth.json.
    """
    try:
        with (Path(home) / "auth.json").open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return _codex_identity_from_auth(data)


def resolve_codex_homes(config: Dict[str, Any]) -> List[str]:
    """Deduped, resolved codex home paths (aliases/symlinks collapse to one)."""
    homes: List[str] = []
    seen: set = set()
    for item in config.get("homes", []):
        resolved = str(Path(item).expanduser().resolve())
        if resolved not in seen:
            seen.add(resolved)
            homes.append(resolved)
    return homes


def backup_codex_auth_pass(
    config: Dict[str, Any], log: Log, skip_homes: "frozenset[str]" = frozenset()
) -> None:
    """Recompute homes + identities and refresh their backups.

    Runs AFTER usage collection (which can trigger a token refresh) so the
    backup captures the freshest single-use token available this tick.
    ``skip_homes`` are homes whose collection just reported an auth failure —
    their credential is not trustworthy enough to promote over a good backup.
    """
    homes = resolve_codex_homes(config)
    identities: Dict[str, Dict[str, str]] = {}
    for home in homes:
        if home in skip_homes:
            continue
        identity = codex_home_identity(home)
        if identity is not None:
            identities[home] = identity
    backup_codex_auth(homes, identities, log)


def codex_auth_backup_path(account_id: str) -> Path:
    """On-disk location of a codex account's protected auth.json backup.

    Keyed by a hash of the full account_id so distinct ids can never collide
    onto one file (e.g. after character sanitization).
    """
    digest = hashlib.sha256(account_id.encode("utf-8")).hexdigest()
    return CODEX_AUTH_BACKUP_DIR / (digest + ".json")


def _codex_auth_is_complete(data: Any) -> bool:
    """True only when the payload carries a usable, refreshable credential.

    A backup or restore candidate must have an account_id AND both a refresh
    and access token — otherwise ``{"tokens": {"account_id": "X"}}`` would pass
    as a "valid" backup and later overwrite a real login with garbage.
    """
    tokens = data.get("tokens") if isinstance(data, dict) else None
    if not isinstance(tokens, dict):
        return False
    return bool(
        tokens.get("account_id")
        and tokens.get("refresh_token")
        and tokens.get("access_token")
    )


def backup_codex_auth(homes: Sequence[str], identities: Dict[str, Dict[str, str]], log: Log) -> None:
    """Keep a fresh protected copy of every SOLE-holder home's auth.json.

    Backups are keyed by account_id and refreshed every tick. Only a complete
    credential (account_id + refresh + access token) that uniquely holds its
    account is ever snapshotted — never a duplicate, ambiguous, or token-less
    payload. NOTE: a codex refresh token is single-use, so a backup is a
    best-effort recovery aid for MANUAL restore (bin/codex-restore.sh), not an
    automatic guarantee — the daemon never writes auth.json itself.
    """
    holders: Dict[str, List[str]] = {}
    for home in homes:
        identity = identities.get(home)
        if identity is not None:
            holders.setdefault(identity["accountId"], []).append(home)
    for home in homes:
        identity = identities.get(home)
        if identity is None:
            continue
        account_id = identity["accountId"]
        if len(holders.get(account_id, ())) != 1:
            continue
        try:
            content = (Path(home) / "auth.json").read_text(encoding="utf-8")
        except OSError:
            continue
        data = load_json_str(content)
        parsed = _codex_identity_from_auth(data)
        if parsed is None or parsed["accountId"] != account_id:
            continue
        if not _codex_auth_is_complete(data):
            continue  # never back up a credential-less payload
        backup = codex_auth_backup_path(account_id)
        existed = backup.exists()
        if existed:
            # Keep one previous generation so a rolled-back/bogus (but complete)
            # credential can never destroy the last known-good backup outright.
            try:
                prev = backup.read_text(encoding="utf-8")
                if prev != content:
                    atomic_write(backup.with_suffix(".prev.json"), prev)
            except OSError:
                pass
        try:
            atomic_write(backup, content)
        except OSError as exc:
            log.write("codex auth backup for %s failed: %s" % (account_id, exc), "WARN")
            continue
        if not existed:
            log.write(
                "codex auth backup created for %s (%s)"
                % (identity.get("email") or account_id, home)
            )


def guard_codex_identities(
    config: Dict[str, Any],
    state: Dict[str, Any],
    now: float,
    log: Log,
    notifications_enabled: bool = True,
) -> Dict[str, str]:
    """Detect login overwrites and collapse duplicate-account homes.

    ``codex login`` writes auth.json into whatever CODEX_HOME it runs under,
    silently replacing that home's account -- this is a real, observed way to
    lose a login permanently, since Codex refresh tokens are single-use and the
    overwritten one cannot be recovered without a fresh browser login.
    Track each home's identity across ticks and notify the moment
    one changes; when two homes hold the SAME account, exclude the non-active
    one so rotation never "fails over" onto the account it just left.
    Returns {home: reason} for homes to exclude this tick.
    """
    homes = resolve_codex_homes(config)  # deduped: aliases/symlinks -> real dup-count
    active = codex_active_home(homes) if homes else None
    registry = state["codexIdentities"]
    identities: Dict[str, Dict[str, str]] = {}
    for home in homes:
        identity = codex_home_identity(home)
        if identity is not None:
            identities[home] = identity

    for home, identity in identities.items():
        previous = registry.get(home)
        if (
            isinstance(previous, dict)
            and previous.get("accountId")
            and previous["accountId"] != identity["accountId"]
        ):
            message = "codex home %s changed account: %s -> %s (a login overwrote auth.json)" % (
                home,
                previous.get("email") or previous["accountId"],
                identity["email"] or identity["accountId"],
            )
            log.write(message, "WARN")
            if notifications_enabled:
                notify(message, "codex-identity:%s:%s" % (home, identity["accountId"]), state, now, log)
        registry[home] = identity

    excluded: Dict[str, str] = {}
    canonical: Dict[str, str] = {}
    for home in homes:
        if home not in identities:
            continue
        account_id = identities[home]["accountId"]
        holder = canonical.get(account_id)
        if holder is None:
            canonical[account_id] = home
            continue
        keep, drop = (home, holder) if home == active else (holder, home)
        canonical[account_id] = keep
        email = identities[drop]["email"] or account_id
        excluded[drop] = "duplicate of %s (%s)" % (keep, email)
        log.write(
            "codex duplicate homes: %s and %s hold the same account (%s); %s excluded from rotation"
            % (keep, drop, email, drop),
            "WARN",
        )
        if notifications_enabled:
            notify(
                "codex homes %s and %s are the SAME account (%s) — %s excluded from rotation. "
                "This is the token-revoking case: recover the clobbered account now with "
                "bin/codex-restore.sh %s (or re-login it fresh)." % (keep, drop, email, drop, drop),
                "codex-duplicate:%s:%s" % (drop, account_id),
                state,
                now,
                log,
            )
    return excluded


def _parse_reset_credits(usage: Any) -> Tuple[ResetCredit, ...]:
    """Extract available reset credits from a codexbar usage record."""
    if not isinstance(usage, dict):
        return tuple()
    block = usage.get("codexResetCredits")
    if not isinstance(block, dict):
        return tuple()
    out: List[ResetCredit] = []
    for credit in block.get("credits") or []:
        if not isinstance(credit, dict):
            continue
        if str(credit.get("status")) != "available":
            continue
        cid = credit.get("id")
        if not cid:
            continue
        out.append(
            ResetCredit(
                credit_id=str(cid),
                expires_at=iso_epoch(credit.get("expires_at")),
                reset_type=str(credit.get("reset_type") or credit.get("title") or ""),
            )
        )
    return tuple(out)


def collect_codex(
    config: Dict[str, Any],
    state: Dict[str, Any],
    now: float,
    log: Log,
    excluded: Optional[Dict[str, str]] = None,
) -> ProviderSnapshot:
    expanded_homes = resolve_codex_homes(config)  # deduped: never poll an alias twice
    active_id = codex_active_home(expanded_homes) if expanded_homes else None
    accounts: List[AccountSnapshot] = []
    backoffs = state["codexBackoff"]
    for home in expanded_homes:
        auth_path = Path(home) / "auth.json"
        if not auth_path.is_file():
            backoffs.pop(home, None)
            continue
        if excluded and home in excluded:
            backoffs.pop(home, None)
            accounts.append(
                AccountSnapshot(home, tuple(), None, "duplicate account: %s" % excluded[home])
            )
            continue
        backoff = backoffs.get(home, {})
        if float(backoff.get("nextAttemptAt", 0)) > now:
            accounts.append(AccountSnapshot(home, tuple(), None, "collector backoff active"))
            continue
        environment = os.environ.copy()
        environment["CODEX_HOME"] = home
        code, stdout, stderr = run_command(
            ["/opt/homebrew/bin/codexbar", "usage", "--provider", "codex", "--format", "json"],
            env=environment,
            timeout=60.0,
        )
        windows: List[Window] = []
        error = None
        if code == 0:
            try:
                payload = json.loads(stdout)
                record = payload[0] if isinstance(payload, list) and payload else {}
                if isinstance(record, dict) and record.get("error"):
                    error = str(record.get("error"))
                usage = record.get("usage") if isinstance(record, dict) else None
                if isinstance(usage, dict):
                    primary = usage.get("primary") if isinstance(usage.get("primary"), dict) else {}
                    secondary = usage.get("secondary") if isinstance(usage.get("secondary"), dict) else {}
                    if number(primary.get("usedPercent")) is not None:
                        windows.append(
                            Window(
                                "primary", float(primary["usedPercent"]), WindowKind.FIVE_HOUR,
                                iso_epoch(primary.get("resetsAt")),
                            )
                        )
                    if number(secondary.get("usedPercent")) is not None:
                        windows.append(
                            Window(
                                "secondary", float(secondary["usedPercent"]), WindowKind.WEEKLY,
                                iso_epoch(secondary.get("resetsAt")),
                            )
                        )
                else:
                    error = error or "missing usage"
            except (ValueError, TypeError, KeyError) as parse_error:
                error = "invalid JSON: %s" % parse_error
        else:
            error = "rc=%s %s" % (code, stderr.strip())

        if error or not windows:
            failures = int(backoff.get("failures", 0)) + 1
            delay = min(900, 60 * (2 ** min(failures - 1, 4)))
            backoffs[home] = {
                "failures": failures,
                "nextAttemptAt": now + delay,
                "lastError": error or "no known policy windows",
            }
            log.write("Codex collection failed home=%s retry=%ss error=%s" % (home, delay, error), "WARN")
            accounts.append(AccountSnapshot(home, tuple(), None, error or "no known policy windows"))
        else:
            backoffs.pop(home, None)
            accounts.append(
                AccountSnapshot(
                    home, tuple(windows), now, None, _parse_reset_credits(usage)
                )
            )
    return ProviderSnapshot(provider="codex", active_account_id=active_id or "", accounts=tuple(accounts))


def policy_config(raw: Dict[str, Any]) -> PolicyConfig:
    harvest = raw.get("harvest", {})
    if not isinstance(harvest, dict):
        harvest = {}
    return PolicyConfig(
        five_hour_threshold=float(raw.get("thresholds", {}).get("fiveHour", 95)),
        weekly_threshold=float(raw.get("thresholds", {}).get("weekly", 95)),
        ladder_percent=float(raw.get("ladderPct", 99)),
        cooldown_seconds=float(raw.get("cooldownSeconds", 300)),
        hysteresis=float(raw.get("hysteresis", 0.05)),
        unknown_ticks=int(raw.get("unknownTicks", 3)),
        stale_max_seconds=float(raw.get("staleMaxSeconds", 900)),
        rideable_five_hour_pct=float(raw.get("rideableFiveHourPct", 75)),
        require_five_hour=bool(raw.get("requireFiveHour", True)),
        ladder_rotation_seconds=60.0,
        harvest_enabled=bool(harvest.get("enabled", True)),
        harvest_window_seconds=float(harvest.get("windowHours", 24)) * 3600.0,
        harvest_min_weekly_left_pct=float(harvest.get("minWeeklyLeftPct", 40)),
        harvest_max_five_hour_pct=float(harvest.get("maxFiveHourPct", 50)),
        harvest_cooldown_seconds=float(harvest.get("cooldownSeconds", 10800)),
        **_redeem_config(raw.get("redeem", {})),
    )


def _redeem_config(raw: Any) -> Dict[str, Any]:
    """Map the optional `redeem` config block; defaults keep auto-redeem OFF."""
    if not isinstance(raw, dict):
        raw = {}
    return {
        "redeem_enabled": bool(raw.get("enabled", False)),
        "redeem_starvation": bool(raw.get("starvation", True)),
        "redeem_expiry_harvest": bool(raw.get("expiryHarvest", True)),
        "redeem_reserve": int(raw.get("reserve", 0)),
        "redeem_starvation_breach_reserve": bool(raw.get("starvationBreachReserve", True)),
        "redeem_starvation_pct": float(raw.get("starvationPct", 99)),
        "redeem_harvest_min_weekly_pct": float(raw.get("harvestMinWeeklyPct", 15)),
        "redeem_expiry_notify_seconds": float(raw.get("expiryNotifyHours", 24)) * 3600.0,
        "redeem_expiry_safety_lead_seconds": float(raw.get("expirySafetyLeadMinutes", 60)) * 60.0,
        "redeem_request_margin_seconds": float(raw.get("requestMarginSeconds", 300)),
        "redeem_cooldown_seconds": float(raw.get("cooldownSeconds", 3600)),
    }


def policy_state(raw: Dict[str, Any]) -> PolicyState:
    fields = {field.name for field in dataclasses.fields(PolicyState)}
    return PolicyState(**{key: value for key, value in raw.items() if key in fields})


def snapshot_json(snapshot: ProviderSnapshot) -> Dict[str, Any]:
    return dataclasses.asdict(snapshot)


def decision_json(decision: Decision, now: float, dry_run: bool) -> Dict[str, Any]:
    value = dataclasses.asdict(decision)
    if isinstance(decision, SwitchDecision):
        value["kind"] = "switch"
    elif isinstance(decision, NotifyDecision):
        value["kind"] = "notify"
    elif isinstance(decision, RedeemDecision):
        value["kind"] = "redeem"
    else:
        value["kind"] = "none"
    value["at"] = now
    value["atIso"] = utc_stamp(now)
    value["dryRun"] = dry_run
    return value


def decision_attr(decision: Decision, name: str, default: Any = None) -> Any:
    return getattr(decision, name, default)


def notify(
    message: str,
    key: str,
    state: Dict[str, Any],
    now: float,
    log: Log,
    rotation: bool = False,
) -> None:
    previous = float(state["notifications"].get(key, 0))
    if not rotation and now - previous < 1800:
        return
    script_message = message.replace("\\", "\\\\").replace('"', '\\"')
    script = 'display notification "%s" with title "subswitch" sound name "Sosumi"' % script_message
    code, _, stderr = run_command(["osascript", "-e", script], timeout=10)
    if code == 0:
        state["notifications"][key] = now
    else:
        log.write("notification failed key=%s error=%s" % (key, stderr.strip()), "WARN")


def ensure_computer_use_enabled(log: Log, state: Dict[str, Any], now: float) -> bool:
    """Re-enable the computer-use MCP server without reformatting config.toml.

    Codex app updates have occasionally flipped this one capability off.  This
    deliberately works on the text rather than parsing and reserializing TOML:
    only an ``enabled = false`` line in the exact computer-use section may be
    changed, and every other byte remains intact.
    """

    global MISSING_COMPUTER_USE_SECTION_LOGGED
    path = Path("~/.codex/config.toml").expanduser()
    try:
        # newline="" is essential: read_text() uses universal-newline mode,
        # which would silently turn CRLF config files into LF files.
        with path.open("r", encoding="utf-8", newline="") as handle:
            content = handle.read()
        mode = path.stat().st_mode & 0o777
    except OSError as error:
        log.write("computer-use config guard could not read %s: %s" % (path, error), "WARN")
        return False

    section_start: Optional[int] = None
    lines = content.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.rstrip("\r\n") == "[mcp_servers.computer-use]":
            section_start = index + 1
            break
    if section_start is None:
        if not MISSING_COMPUTER_USE_SECTION_LOGGED:
            log.write(
                "computer-use config guard: [mcp_servers.computer-use] section absent; no edit",
                "WARN",
            )
            MISSING_COMPUTER_USE_SECTION_LOGGED = True
        return False

    for index in range(section_start, len(lines)):
        line = lines[index]
        bare = line.rstrip("\r\n")
        if bare.startswith("["):
            break
        if bare == "enabled = false":
            newline = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            lines[index] = "enabled = true" + newline
            try:
                atomic_write(path, "".join(lines), mode=mode)
            except OSError as error:
                log.write("computer-use config guard could not write %s: %s" % (path, error), "ERROR")
                return False
            log.write("COMPUTER-USE CONFIG GUARD: enabled = false -> enabled = true", "WARN")
            notify(
                "computer-use was disabled (app update?) — re-enabled automatically",
                "config-guard",
                state,
                now,
                log,
            )
            return True
    return False


COMPUTER_USE_APPROVALS = Path(
    "~/Library/Group Containers/2DC432GLL2.com.openai.sky.CUAService/Library/"
    "Application Support/Software/ComputerUseAppApprovals.json"
).expanduser()
_BUNDLE_TAIL = (
    "Codex Computer Use.app/Contents/SharedSupport/"
    "SkyComputerUseClient.app/Contents/MacOS/SkyComputerUseClient"
)
# The Codex CLI moved the bundle out of the versioned plugin cache and into a
# flat per-home directory (observed 2026-07-23: the legacy path still exists but
# now holds only assets/bin/scripts/skills, so globbing it alone reported EVERY
# home as broken while computer-use was perfectly healthy). Accept either layout
# — a canary that cries wolf every tick is worse than no canary, because a real
# breakage becomes indistinguishable from the noise.
COMPUTER_USE_BUNDLE_GLOBS = (
    "computer-use/" + _BUNDLE_TAIL,
    "plugins/cache/openai-bundled/computer-use/*/" + _BUNDLE_TAIL,
)


def check_computer_use_ready(
    config: Dict[str, Any], state: Dict[str, Any], now: float, log: Log,
    notifications_enabled: bool = True,
) -> bool:
    """Cheap structural canary for computer-use — no LLM, runs every tick.

    Computer-use survives account swaps as long as three things hold: the MCP is
    enabled (handled by ensure_computer_use_enabled), the plugin bundle exists,
    and Chrome is approved in the headless CUA store. An app update or a TCC
    reset can break the latter two silently; this surfaces them with the fix.
    The full end-to-end LLM probe (bin/probe-computer-use.sh) stays on-demand
    because it costs tokens; this catches the structural breakages for free.
    """
    if not config.get("computerUseCanary", True):
        return True
    ok = True
    homes = [str(Path(h).expanduser().resolve()) for h in config.get("homes", [])]
    if not homes:
        homes = [str((Path.home() / ".codex").resolve())]
    # Every switch-eligible home must have the bundle — a home missing it would
    # break computer-use the moment failover lands there.
    for home in homes:
        if not any(list(Path(home).glob(pattern)) for pattern in COMPUTER_USE_BUNDLE_GLOBS):
            ok = False
            message = "computer-use bundle missing under %s — reopen the Codex app to reinstall it" % home
            log.write("COMPUTER-USE CANARY: %s" % message, "WARN")
            if notifications_enabled:
                notify(message, "cu-canary:bundle:%s" % home, state, now, log)
    # The approval store lives under ~/Library/Group Containers, which a launchd
    # daemon cannot read without Full Disk Access. Fail OPEN when the file is
    # unreadable (unknown, not a defect) — only alarm when it reads cleanly AND
    # Chrome is genuinely absent, which is the real "re-grant needed" signal.
    try:
        with COMPUTER_USE_APPROVALS.open("r", encoding="utf-8") as handle:
            store = json.load(handle)
        # A syntactically valid non-object ([], null, ...) is corrupt, not fatal.
        identifiers = store.get("approvedBundleIdentifiers") or [] if isinstance(store, dict) else []
    except OSError:
        identifiers = None  # cannot tell — do not alarm
    except ValueError:
        identifiers = []  # readable but corrupt → treat as not approved
    if identifiers is not None and "com.google.Chrome" not in identifiers:
        ok = False
        message = (
            "computer-use: Chrome is NOT approved (TCC/CUA store) — re-grant by "
            "running a computer-use action once and approving Chrome"
        )
        log.write("COMPUTER-USE CANARY: %s" % message, "WARN")
        if notifications_enabled:
            notify(message, "cu-canary:approval", state, now, log)
    return ok


def atomic_pointer(target: str) -> None:
    atomic_write(CONFIG_ROOT / "codex-current", target + "\n")


def execute_switch(decision: Decision, enforce: bool, log: Log) -> bool:
    provider = decision_attr(decision, "provider")
    target = str(decision_attr(decision, "to_account_id"))
    prefix = "" if enforce else "DRY-RUN "
    log.write("%sswitch provider=%s target=%s reason=%s" % (prefix, provider, target, decision.reason))
    if not enforce:
        return False
    if provider == "claude":
        code, stdout, stderr = run_command(["cswap", "switch", target, "--json"])
        if code != 0:
            log.write("Claude switch deferred until next tick rc=%s error=%s" % (code, stderr.strip()), "WARN")
            return False
        log.write("Claude switch completed payload=%s" % stdout.strip())
        return True
    if provider == "codex":
        atomic_pointer(target)
        log.write("Codex pointer switched target=%s" % target)
        return True
    log.write("unknown switch provider=%s" % provider, "ERROR")
    return False


CONSUME_URL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume"
# Short backoff before re-POSTing the same (deterministic-UUID) request after an
# ambiguous outcome, so a persistent 429/5xx isn't hammered every tick.
REDEEM_AMBIGUOUS_BACKOFF = 300.0


def execute_redeem(
    decision: RedeemDecision, enforce: bool, state: Dict[str, Any], now: float,
    log: Log, notifications_enabled: bool = True,
) -> str:
    """Consume one reset credit. Returns one of:
      'spent'            — reset/already_redeemed: record the spend (+cooldown).
      'ambiguous'        — 401/429/5xx/timeout: request may have reached the
                           server; retry the SAME deterministic UUID after a
                           short backoff (server de-dupes).
      'failed_not_spent' — local validation, hard 4xx, nothing_to_reset,
                           no_credit, unknown code: nothing was consumed. Do NOT
                           mark redeemed (that would strand a still-valid credit);
                           back off and retry later.
      'dryrun'           — enforce is False; no POST, no state change.

    Only 'spent' is a real consumption. Dry-run is checked BEFORE credentials so
    an unreadable auth.json can never mutate spend state. Contract (Sol review):
    POST wham consume, Bearer + ChatGPT-Account-ID, {redeem_request_id, credit_id}.
    """
    home = decision.account_id
    label = os.path.basename(home)
    if not enforce:
        log.write("DRY-RUN redeem home=%s credit=%s" % (home, decision.credit_id))
        return "dryrun"

    try:
        with (Path(home) / "auth.json").open("r", encoding="utf-8") as handle:
            tokens = json.load(handle).get("tokens") or {}
    except (OSError, ValueError) as error:
        log.write("redeem: cannot read auth for %s: %s" % (home, error), "WARN")
        return "failed_not_spent"
    access, account_id = tokens.get("access_token"), tokens.get("account_id")
    if not access or not account_id:
        log.write("redeem: missing token/account_id for %s" % home, "WARN")
        return "failed_not_spent"

    # Guard the reauth race: the credit was observed under the account the
    # identity guard recorded THIS tick. If auth.json now holds a different
    # account, the home was re-logged between collection and now — refuse rather
    # than send one account's credit with another account's token.
    observed = (state.get("codexIdentities", {}).get(home) or {}).get("accountId")
    if observed and str(observed) != str(account_id):
        log.write(
            "redeem: account changed for %s (%s -> %s) — refusing" % (home, observed, account_id),
            "WARN",
        )
        return "failed_not_spent"

    body = json.dumps(
        {"redeem_request_id": decision.idempotency_key, "credit_id": decision.credit_id}
    ).encode("utf-8")
    request = urllib.request.Request(
        CONSUME_URL, data=body, method="POST",
        headers={
            "Authorization": "Bearer %s" % access,
            "ChatGPT-Account-ID": str(account_id),
            "User-Agent": "subswitch/1.0",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code == 401 or error.code == 429 or 500 <= error.code < 600:
            log.write("redeem: HTTP %s for %s — ambiguous, retry same UUID" % (error.code, home), "WARN")
            return "ambiguous"
        log.write("redeem: HTTP %s for %s — not spent, backing off" % (error.code, home), "ERROR")
        if notifications_enabled:
            notify("reset-credit redeem failed (HTTP %s) on %s" % (error.code, label),
                   "redeem-fail:%s" % decision.credit_id, state, now, log)
        return "failed_not_spent"
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        log.write("redeem: transport error for %s: %s — ambiguous, retry same UUID" % (home, error), "WARN")
        return "ambiguous"

    code = str(data.get("code") or "")
    windows = data.get("windows_reset")
    if code in ("reset", "already_redeemed"):
        message = "redeemed a reset credit on %s (%s, windows_reset=%s)" % (label, code, windows)
        log.write("REDEEM OK: %s" % message, "WARN")
        if notifications_enabled:
            notify("subswitch " + message, "redeem-ok:%s" % decision.credit_id, state, now, log, rotation=True)
        if code == "reset" and (windows is None or windows <= 0):
            log.write("redeem: WARNING windows_reset=%s — may not have freed a window" % windows, "WARN")
        return "spent"
    log.write("redeem: outcome=%s on %s — not spent; refetch next tick" % (code or "?", home), "WARN")
    if notifications_enabled:
        notify("reset-credit redeem returned '%s' on %s" % (code or "?", label),
               "redeem-noop:%s" % decision.credit_id, state, now, log)
    return "failed_not_spent"


def pane_message_offset(capture: str, history_size: int) -> Optional[str]:
    lines = capture.splitlines()
    matching = [index for index, line in enumerate(lines) if USAGE_LIMIT_TEXT in line]
    if not matching:
        return None
    # -S -40 begins at roughly history_size-40. Combining that origin with
    # the captured line index stays stable as the same history line scrolls.
    absolute_line = max(0, history_size - 40) + matching[-1]
    return hashlib.sha256(str(absolute_line).encode("ascii")).hexdigest()


def cmux_message_fingerprint(capture: str) -> Optional[str]:
    matching = [line.strip() for line in capture.splitlines() if USAGE_LIMIT_TEXT in line]
    if not matching:
        return None
    return hashlib.sha256(matching[-1].encode("utf-8")).hexdigest()


ROLLOUT_ID_RE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.jsonl$"
)


def _pane_process_tree(pane_pid: str) -> List[str]:
    pids = [pane_pid]
    frontier = [pane_pid]
    for _ in range(3):
        children: List[str] = []
        for pid in frontier:
            code, out, _ = run_command(["pgrep", "-P", pid], timeout=5)
            if code == 0:
                children.extend(out.split())
        if not children:
            break
        pids.extend(children)
        frontier = children
    return pids


def _proc_start_epoch(pid: str, now: float) -> Optional[float]:
    code, out, _ = run_command(["ps", "-o", "etime=", "-p", pid], timeout=5)
    text = out.strip()
    if code != 0 or not text:
        return None
    days = 0
    if "-" in text:
        day_part, text = text.split("-", 1)
        try:
            days = int(day_part)
        except ValueError:
            return None
    try:
        fields = [int(part) for part in text.split(":")]
    except ValueError:
        return None
    while len(fields) < 3:
        fields.insert(0, 0)
    return now - (days * 86400 + fields[0] * 3600 + fields[1] * 60 + fields[2])


def match_rollout_by_birth(
    candidates: Sequence[Tuple[str, float]], start_epoch: float
) -> Optional[str]:
    """Return the unique session id born within the pane-start window.

    Refusing on ambiguity is deliberate: resuming the WRONG session (e.g. a
    concurrent goal-runner's) is far worse than not resuming at all.
    """

    matches = [
        session_id
        for session_id, born_at in candidates
        if start_epoch - 30.0 <= born_at <= start_epoch + 180.0
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def pane_codex_session(pane_pid: str, now: float) -> Optional[str]:
    """Identify the codex session id owned by a tmux pane's process tree."""

    pids = _pane_process_tree(pane_pid)
    # Precise path: a rollout file held open by the process (mid-turn).
    for pid in pids:
        code, out, _ = run_command(["lsof", "-p", pid, "-Fn"], timeout=10)
        if code != 0:
            continue
        for line in out.splitlines():
            if line.startswith("n") and "/sessions/" in line and line.endswith(".jsonl"):
                found = ROLLOUT_ID_RE.search(line)
                if found:
                    return found.group(1)
    # Fallback: unique rollout file whose creation time matches process start.
    start_epoch = None
    for pid in pids:
        epoch = _proc_start_epoch(pid, now)
        if epoch is not None:
            start_epoch = epoch if start_epoch is None else max(start_epoch, epoch)
    if start_epoch is None:
        return None
    sessions_root = Path.home() / ".codex" / "sessions"
    candidates: List[Tuple[str, float]] = []
    for day_offset in (0, 86400):
        moment = datetime.fromtimestamp(now - day_offset)
        day_dir = sessions_root / moment.strftime("%Y") / moment.strftime("%m") / moment.strftime("%d")
        if not day_dir.is_dir():
            continue
        for rollout in day_dir.glob("rollout-*.jsonl"):
            found = ROLLOUT_ID_RE.search(rollout.name)
            if not found:
                continue
            try:
                born_at = rollout.stat().st_birthtime
            except (OSError, AttributeError):
                continue
            candidates.append((found.group(1), born_at))
    return match_rollout_by_birth(candidates, start_epoch)


def scan_codex_panes(state: Dict[str, Any], now: float, log: Log) -> List[Dict[str, str]]:
    code, stdout, stderr = run_command(
        [
            "tmux",
            "list-panes",
            "-a",
            "-F",
            "#{pane_id}\t#{pane_current_command}\t#{pane_pid}\t#{history_size}",
        ],
        timeout=10,
    )
    if code != 0:
        normal_no_server = (
            "no server running",
            "failed to connect",
            "error connecting to",
        )
        if any(marker in stderr.lower() for marker in normal_no_server):
            state["panes"].clear()
        else:
            log.write("tmux pane scan skipped: %s" % stderr.strip(), "WARN")
        return []
    hits: List[Dict[str, str]] = []
    seen = set()
    for line in stdout.splitlines():
        parts = line.split("\t", 3)
        # codex 0.144.4+ processes report the raw binary name, truncated by
        # tmux (e.g. "codex-aarch64-a" for codex-aarch64-apple-darwin), so an
        # exact match on "codex" misses real sessions.
        if len(parts) != 4 or not parts[1].startswith("codex"):
            continue
        pane = parts[0]
        pane_pid = parts[2]
        try:
            history_size = int(parts[3])
        except ValueError:
            history_size = 0
        seen.add(pane)
        capture_code, capture, capture_error = run_command(
            ["tmux", "capture-pane", "-p", "-S", "-40", "-t", pane], timeout=10
        )
        if capture_code != 0:
            log.write("tmux capture failed pane=%s error=%s" % (pane, capture_error.strip()), "WARN")
            continue
        fingerprint = pane_message_offset(capture, history_size)
        pane_state = state["panes"].setdefault(pane, {})
        if fingerprint is None:
            pane_state.pop("messageFingerprint", None)
            continue
        # Mark the message seen at scan time: one usage-limit message yields
        # exactly one hard-limit signal + resume attempt, no matter how long
        # it lingers in the pane scrollback (prevents pointer ping-pong).
        if pane_state.get("messageFingerprint") == fingerprint:
            continue
        pane_state["messageFingerprint"] = fingerprint
        session_id = pane_codex_session(pane_pid, now)
        if session_id is None:
            log.write(
                "usage-limit pane %s: could not pin its codex session id; refusing auto-resume"
                % pane,
                "ERROR",
            )
        hits.append({
            "pane": pane, "fingerprint": fingerprint, "sessionId": session_id or "",
            "home": _pane_codex_home(pane_pid) or "",
        })
    for pane in list(state["panes"]):
        if not pane.startswith("cmux:") and pane not in seen:
            del state["panes"][pane]
    return hits


def cmux_binary() -> str:
    return CMUX_APP_BIN if Path(CMUX_APP_BIN).is_file() else "cmux"


def cmux_env() -> Dict[str, str]:
    """Environment for cmux socket calls from the launchd context.

    The socket path is deterministic (cmux-<uid>.sock) and the password-mode
    secret lives in a 0600 file OUTSIDE the repo. Requires cmux >= 0.64.19
    (socketControlMode = password); older servers refuse non-cmux peers.
    """

    environment = os.environ.copy()
    environment.setdefault(
        "CMUX_SOCKET_PATH",
        str(Path.home() / ".local" / "state" / "cmux" / ("cmux-%d.sock" % os.getuid())),
    )
    secret_path = CONFIG_ROOT / "cmux-socket-password"
    try:
        secret = secret_path.read_text(encoding="utf-8").strip()
        if secret:
            environment.setdefault("CMUX_SOCKET_PASSWORD", secret)
    except OSError:
        pass
    return environment


def _cmux_codex_process(processes: Sequence[Dict[str, Any]]) -> Tuple[bool, Optional[str]]:
    for process in processes:
        name = str(process.get("name") or "").rsplit("/", 1)[-1]
        if name.startswith("codex"):
            pid = process.get("pid")
            if isinstance(pid, int) and not isinstance(pid, bool):
                return True, str(pid)
            if isinstance(pid, str) and pid.isdigit():
                return True, pid
            return True, None
        children = process.get("children")
        if isinstance(children, list):
            found, pid = _cmux_codex_process(
                [item for item in children if isinstance(item, dict)]
            )
            if found:
                return found, pid
    return False, None


def _cmux_top_surfaces(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    surfaces: List[Dict[str, Any]] = []
    windows = payload.get("windows") if isinstance(payload.get("windows"), list) else []
    for window in windows:
        if not isinstance(window, dict):
            continue
        workspaces = window.get("workspaces") if isinstance(window.get("workspaces"), list) else []
        for workspace in workspaces:
            if not isinstance(workspace, dict):
                continue
            panes = workspace.get("panes") if isinstance(workspace.get("panes"), list) else []
            for pane in panes:
                if not isinstance(pane, dict):
                    continue
                raw_surfaces = pane.get("surfaces") if isinstance(pane.get("surfaces"), list) else []
                surfaces.extend(item for item in raw_surfaces if isinstance(item, dict))
    return surfaces


def scan_cmux_codex_surfaces(
    state: Dict[str, Any], now: float, log: Log
) -> List[Dict[str, str]]:
    binary = cmux_binary()
    code, stdout, stderr = run_command(
        [binary, "--json", "top", "--all", "--processes"], timeout=15, env=cmux_env()
    )
    if code != 0:
        log.write("cmux surface scan skipped: %s" % stderr.strip(), "WARN")
        return []
    try:
        payload = json.loads(stdout)
    except ValueError as error:
        log.write("cmux surface scan returned invalid JSON: %s" % error, "WARN")
        return []
    if not isinstance(payload, dict):
        log.write("cmux surface scan returned a non-object payload", "WARN")
        return []

    hits: List[Dict[str, str]] = []
    seen = set()
    for surface in _cmux_top_surfaces(payload):
        processes = surface.get("processes")
        if not isinstance(processes, list):
            continue
        is_codex, process_pid = _cmux_codex_process(
            [item for item in processes if isinstance(item, dict)]
        )
        if not is_codex:
            continue
        surface_handle = str(surface.get("id") or surface.get("ref") or "")
        if not surface_handle:
            log.write("cmux Codex surface has no usable id/ref; skipping", "WARN")
            continue
        pane_key = "cmux:%s" % surface_handle
        seen.add(pane_key)
        capture_code, capture, capture_error = run_command(
            [binary, "read-screen", "--surface", surface_handle, "--scrollback", "--lines", "200"],
            timeout=10,
        )
        if capture_code != 0:
            log.write(
                "cmux capture failed surface=%s error=%s"
                % (surface_handle, capture_error.strip()),
                "WARN",
            )
            continue
        fingerprint = cmux_message_fingerprint(capture)
        pane_state = state["panes"].setdefault(pane_key, {})
        if fingerprint is None:
            pane_state.pop("messageFingerprint", None)
            continue
        if pane_state.get("messageFingerprint") == fingerprint:
            continue
        pane_state["messageFingerprint"] = fingerprint
        session_id = pane_codex_session(process_pid, now) if process_pid else None
        if session_id is None:
            log.write(
                "usage-limit cmux surface %s: could not pin its codex session id; "
                "refusing auto-resume" % surface_handle,
                "ERROR",
            )
        hits.append(
            {
                "pane": pane_key,
                "surface": surface_handle,
                "transport": "cmux",
                "fingerprint": fingerprint,
                "sessionId": session_id or "",
                "home": (_pane_codex_home(process_pid) or "") if process_pid else "",
            }
        )
    for pane in list(state["panes"]):
        if pane.startswith("cmux:") and pane not in seen:
            del state["panes"][pane]
    return hits


def healthy_codex_target(
    result: PolicyResult, preferred: Optional[str]
) -> Optional[str]:
    """Best account to resume a walled Codex session onto.

    Prefers a strictly-healthy account; when none exists (all red), falls back
    to the best red-but-serviceable one (5h not spent) — mirroring the policy's
    escape target — so an all-red usage-limit event still resumes the pane
    instead of stranding it.
    """
    healthy = [
        (account.score, account.account_id)
        for account in result.accounts
        if account.healthy and account.score is not None
    ]
    if not healthy:
        healthy = [
            (account.score, account.account_id)
            for account in result.accounts
            if account.known and account.score is not None and not account.fully_blocked
        ]
    if not healthy:
        return None
    if preferred is not None and any(item[1] == preferred for item in healthy):
        return preferred
    return min(healthy)[1]


CODEX_TUI_PROCESS_PATTERN = "codex-aarch64|/opt/homebrew/bin/codex"
CODEX_EXEC_RE = re.compile(
    r"(?:^|\s)(?:\S*/)?(?:codex|codex-aarch64\S*)\s+exec(?:\s|$)"
)
CODEX_HOME_RE = re.compile(r"(?:^|\s)CODEX_HOME=([^\s]+)")

# Unambiguous dead-auth signatures. These never self-heal (a revoked/invalidated
# refresh token stays dead until re-login), so we surface them immediately —
# unlike transient network/SSL/timeout errors, which the collector's backoff
# retries silently.
DEAD_AUTH_RE = re.compile(
    # Only phrases that specifically mean the refresh token is permanently dead.
    # A bare 401/"unauthorized"/"revoked" can be a transient or endpoint-scope
    # error, so those are left to the collector's backoff, not flagged as dead.
    r"token[_ ]?invalidated|has been invalidated|refresh token was revoked"
    r"|refresh token .*revoked|sign(?:ing)? in again|log out and sign in",
    re.IGNORECASE,
)


def scan_dead_codex_auth(
    config: Dict[str, Any],
    state: Dict[str, Any],
    snapshot: ProviderSnapshot,
    now: float,
    log: Log,
    notifications_enabled: bool = True,
) -> None:
    """Notify the moment a codex home's login is dead, with the exact fix.

    A revoked/invalidated token never recovers on its own, so the collector's
    silent backoff would hide it for days (this is exactly how .codex and
    .codex-2 sat dead unnoticed). We notify once per death and clear the marker
    when the home comes back, so a later death re-notifies. Transient
    network/timeout errors are ignored — only unambiguous auth failures fire.
    """
    if not config.get("deadAuthNotify", True):
        return
    tracked = state["deadAuth"]
    relogin = str(REPO_DIR / "bin" / "codex-relogin.sh")
    live_homes = {
        str(Path(item).expanduser().resolve()) for item in config.get("homes", [])
    }
    for account in snapshot.accounts:
        home = account.account_id
        dead = bool(account.error) and bool(DEAD_AUTH_RE.search(account.error or ""))
        if dead:
            if home in tracked:
                continue
            tracked[home] = now
            message = (
                "codex %s login is DEAD (%s). Fix: %s %s"
                % (os.path.basename(home), (account.error or "")[:60], relogin, home)
            )
            log.write("DEAD CODEX AUTH: %s" % message, "WARN")
            if notifications_enabled:
                notify(message, "dead-auth:%s" % home, state, now, log)
        elif account.error is None and account.windows:
            # Home is healthy again — allow a future death to re-notify.
            tracked.pop(home, None)
    # Forget homes that no longer exist in config.
    for home in list(tracked):
        if home not in live_homes:
            tracked.pop(home, None)


def _process_codex_home(command: str) -> str:
    match = CODEX_HOME_RE.search(command)
    raw = match.group(1) if match else str(Path.home() / ".codex")
    return str(Path(raw).expanduser().resolve())


def _pane_codex_home(pane_pid: str) -> Optional[str]:
    """The CODEX_HOME the codex process inside a pane runs under (its account).

    A walled session's usage-limit message is that account's — not the current
    pointer's. Attributing the hit to its own home lets the tick avoid
    force-switching an unrelated, healthy active account.

    The actual codex CHILD is identified by its executable name (``comm``), not
    by a substring of the environment — otherwise a shell ancestor whose env
    merely contains ``CODEX_HOME=`` would be matched first and mis-attribute the
    home. The env is read (``ps eww``) only for that confirmed codex process.
    """
    for pid in _pane_process_tree(pane_pid):
        comm_code, comm, _ = run_command(["ps", "-o", "comm=", "-p", pid], timeout=5)
        if comm_code != 0 or "codex" not in comm.rsplit("/", 1)[-1].lower():
            continue
        env_code, command, _ = run_command(["ps", "eww", "-o", "command=", "-p", pid], timeout=5)
        if env_code == 0:
            return _process_codex_home(command)
    return None


def scan_walled_codex_processes(
    config: Dict[str, Any],
    state: Dict[str, Any],
    snapshot: ProviderSnapshot,
    result: PolicyResult,
    now: float,
    log: Log,
    notifications_enabled: bool = True,
) -> None:
    """Warn once for each long-running TUI whose account is hard-walled."""

    tracked = state["walledPids"]
    code, stdout, stderr = run_command(
        ["pgrep", "-f", CODEX_TUI_PROCESS_PATTERN], timeout=10
    )
    if code not in (0, 1):
        log.write("Codex process scan skipped: %s" % stderr.strip(), "WARN")
        return
    live_pids = {pid for pid in stdout.split() if pid.isdigit()} if code == 0 else set()
    for pid in list(tracked):
        if pid not in live_pids:
            del tracked[pid]

    if not config.get("walledProcessNotify", True):
        return

    evaluations = {account.account_id: account for account in result.accounts}
    for pid in sorted(live_pids, key=int):
        if pid in tracked:
            continue
        command_code, command, command_error = run_command(
            ["ps", "eww", "-o", "command=", "-p", pid], timeout=10
        )
        if command_code != 0 or not command.strip():
            log.write(
                "Codex process inspect failed pid=%s error=%s"
                % (pid, command_error.strip()),
                "WARN",
            )
            continue
        if CODEX_EXEC_RE.search(command):
            continue
        home = _process_codex_home(command)
        evaluation = evaluations.get(home)
        observed_hard_limit = snapshot.hard_limit_observed and home == snapshot.active_account_id
        if evaluation is None or not (evaluation.hard_limit or observed_hard_limit):
            continue
        start_epoch = _proc_start_epoch(pid, now)
        if start_epoch is None or now - start_epoch <= 300.0:
            continue
        message = (
            "codex session pid %s (home %s) is on a walled account — "
            "redeem a reset credit or restart it on a fresh account"
        ) % (pid, home)
        tracked[pid] = now
        log.write("WALLED CODEX PROCESS: %s" % message, "WARN")
        if notifications_enabled:
            notify(message, "walled-process:%s" % pid, state, now, log)


def resume_panes(
    hits: Sequence[Dict[str, str]],
    target: Optional[str],
    config: Dict[str, Any],
    state: Dict[str, Any],
    now: float,
    enforce: bool,
    pointer_was_switched: bool,
    log: Log,
) -> None:
    watcher = config.get("watcher", {})
    if not watcher.get("autoResume", True) or target is None:
        if hits:
            log.write("Codex usage-limit pane hit but no healthy auto-resume target")
        return
    debounce = float(watcher.get("debounceSeconds", 600))
    for hit in hits:
        pane = hit["pane"]
        transport = hit.get("transport") or "tmux"
        subject = hit.get("surface") or pane
        fingerprint = hit["fingerprint"]
        pane_state = state["panes"].setdefault(pane, {})
        too_recent = now - float(pane_state.get("lastResumeAt", 0)) < debounce
        if too_recent:
            continue
        session_id = hit.get("sessionId") or ""
        if not session_id:
            # Never resume blind: `resume --last` picks the machine's most
            # recent session GLOBALLY and can hijack a live goal-runner
            # (observed in the 2026-07-16 drill). Leave the pane untouched.
            log.write(
                "AUTO-RESUME refused %s=%s: no pinned session id"
                % ("surface" if transport == "cmux" else "pane", subject),
                "ERROR",
            )
            notify(
                "Codex %s %s hit its usage limit; could not pin its session — resume it manually."
                % ("cmux surface" if transport == "cmux" else "pane", subject),
                "auto-resume-unpinned:%s" % pane,
                state,
                now,
                log,
            )
            continue
        prefix = "" if enforce else "DRY-RUN "
        log.write(
            "%sAUTO-RESUME transport=%s target=%s destination=%s pointerSwitched=%s "
            "command=codex resume <pinned-session>"
            % (prefix, transport, target, subject, pointer_was_switched)
        )
        if not enforce:
            continue
        # Goal-runner panes run codex as their ONLY process: when codex exits,
        # the pane (and session) dies before keys can be typed into it.
        # remain-on-exit keeps the dead pane, respawn-pane relaunches it with
        # the resume command through the shim (fresh process → new pointer).
        shim = str(Path.home() / ".local" / "bin" / "codex")
        if transport == "cmux":
            binary = cmux_binary()
            steps = [
                ([binary, "send-key", "--surface", subject, "ctrl+c"], 1.0),
                ([binary, "send-key", "--surface", subject, "ctrl+c"], 2.0),
                (
                    [
                        binary,
                        "respawn-pane",
                        "--surface",
                        subject,
                        "--command",
                        "%s resume %s" % (shim, session_id),
                    ],
                    1.0,
                ),
            ]
        else:
            steps = [
                (["tmux", "set-option", "-p", "-t", pane, "remain-on-exit", "on"], 0.5),
                (["tmux", "send-keys", "-t", pane, "C-c"], 1.0),
                (["tmux", "send-keys", "-t", pane, "C-c"], 2.0),
                (["tmux", "respawn-pane", "-k", "-t", pane, "%s resume %s" % (shim, session_id)], 1.0),
                (["tmux", "set-option", "-p", "-t", pane, "remain-on-exit", "off"], 0.0),
            ]
        completed = True
        step_env = cmux_env() if transport == "cmux" else None
        for arguments, pause in steps:
            code, _, stderr = run_command(arguments, timeout=10, env=step_env)
            if code != 0:
                log.write(
                    "AUTO-RESUME failed transport=%s destination=%s error=%s"
                    % (transport, subject, stderr.strip()),
                    "ERROR",
                )
                notify(
                    "Codex auto-resume failed for %s %s; session needs manual resume."
                    % ("cmux surface" if transport == "cmux" else "pane", subject),
                    "auto-resume-failed:%s" % pane,
                    state,
                    now,
                    log,
                )
                completed = False
                break
            if pause:
                time.sleep(pause)
        if completed:
            pane_state["lastResumeAt"] = now
            pane_state["target"] = target


def apply_result_state(state: Dict[str, Any], provider: str, next_state: PolicyState) -> None:
    state["providers"][provider] = dataclasses.asdict(next_state)


def tick(config: Dict[str, Any], state: Dict[str, Any], force_dry_run: bool, log: Log) -> Dict[str, Any]:
    now = time.time()
    enforce = bool(config.get("enforce", False)) and not force_dry_run
    snapshots: Dict[str, ProviderSnapshot] = {}
    pane_hits: List[Dict[str, str]] = []
    if config.get("claude", {}).get("enabled", True):
        snapshots["claude"] = collect_claude(config["claude"], now, log)
        if config["claude"].get("identityGuard", True):
            guard_claude_identities(state, now, log, bool(config.get("notify", True)))
        scan_invisible_claude_accounts(
            config["claude"], state, snapshots["claude"], now, log, bool(config.get("notify", True))
        )
    if config.get("codex", {}).get("enabled", True):
        if config["codex"].get("configGuard", True):
            ensure_computer_use_enabled(log, state, now)
        check_computer_use_ready(
            config["codex"], state, now, log, bool(config.get("notify", True))
        )
        pane_hits = scan_codex_panes(state, now, log)
        watcher = config["codex"].get("watcher", {})
        if watcher.get("cmux", True):
            pane_hits.extend(scan_cmux_codex_surfaces(state, now, log))
        duplicate_homes = guard_codex_identities(
            config["codex"], state, now, log, bool(config.get("notify", True))
        )
        codex = collect_codex(config["codex"], state, now, log, duplicate_homes)
        # Refresh auth backups AFTER collection so we capture the freshest
        # single-use token if collection triggered a refresh this tick — but
        # never promote a credential the collector just failed to authenticate.
        errored_homes = frozenset(
            account.account_id for account in codex.accounts if account.error
        )
        backup_codex_auth_pass(config["codex"], log, errored_homes)
        # A usage-limit message only forces the ACTIVE account to switch when the
        # walled pane runs under the active home. A hit on another home is that
        # account's session (still resumed below) and must not force-switch a
        # healthy current account. Hits with no attributable home fall back to
        # the historical behavior (treated as authoritative).
        active_home = codex.active_account_id
        # Only a walled pane KNOWN to be on the active home forces the active
        # account to switch. An unattributable hit (no resolvable home) is NOT
        # evidence the active account is the one walled — treating it as such
        # would force a healthy active off for another account's stale message.
        if pane_hits and any(hit.get("home") == active_home for hit in pane_hits):
            codex = dataclasses.replace(codex, hard_limit_observed=True)
        snapshots["codex"] = codex
        scan_dead_codex_auth(
            config["codex"], state, codex, now, log, bool(config.get("notify", True))
        )

    tick_decisions: List[Dict[str, Any]] = []
    codex_switched = False
    codex_switch_target: Optional[str] = None
    codex_redeemed = False
    results: Dict[str, PolicyResult] = {}
    for provider, snapshot in snapshots.items():
        current_state = policy_state(state["providers"].get(provider, {}))
        provider_config = policy_config(config[provider])

        # Reset-credit redemption runs first and is decoupled from switching:
        # a redeem this tick restores an account, and the NEXT tick's fresh
        # usage drives the switch (never redeem-and-switch off a stale snapshot).
        redeem_marks_credits: Dict[str, float] = {}
        redeem_marks_accounts: Dict[str, float] = {}
        redeem_marks_backoff: Dict[str, float] = {}
        redeem_executed = False
        credit_decisions: Tuple[Decision, ...] = tuple()
        if snapshot.accounts and any(a.reset_credits for a in snapshot.accounts):
            credit_decisions, current_state = evaluate_credits(
                snapshot, provider_config, current_state, now
            )
            for decision in credit_decisions:
                tick_decisions.append(decision_json(decision, now, not enforce))
                if isinstance(decision, RedeemDecision):
                    outcome = execute_redeem(
                        decision, enforce, state, now, log, bool(config.get("notify", True))
                    )
                    if outcome == "spent":
                        redeem_executed = True
                        redeem_marks_credits[decision.credit_id] = now
                        redeem_marks_accounts[decision.account_id] = now
                    elif outcome == "ambiguous":
                        redeem_executed = True
                        redeem_marks_backoff[decision.credit_id] = now + REDEEM_AMBIGUOUS_BACKOFF
                    elif outcome == "failed_not_spent":
                        # Nothing was consumed and the POST hit no server-state
                        # change, so the snapshot is still valid — do NOT suppress
                        # this tick's switch; only back the credit off.
                        redeem_marks_backoff[decision.credit_id] = (
                            now + provider_config.redeem_cooldown_seconds
                        )
                elif isinstance(decision, NotifyDecision) and config.get("notify", True):
                    notify(decision.message, "%s:%s" % (provider, decision.key), state, now, log)
        if redeem_executed and provider == "codex":
            codex_redeemed = True

        result = evaluate(snapshot, current_state, provider_config, now)
        results[provider] = result
        # evaluate() rebuilds PolicyState without the redemption fields; carry
        # them forward, folding in this tick's spend / backoff bookkeeping.
        next_state = dataclasses.replace(
            result.next_state,
            last_redeem_at={**current_state.last_redeem_at, **redeem_marks_accounts},
            redeemed_credits={**current_state.redeemed_credits, **redeem_marks_credits},
            redeem_backoff={**current_state.redeem_backoff, **redeem_marks_backoff},
        )
        switch_failed = False
        for decision in result.decisions:
            entry = decision_json(decision, now, not enforce)
            tick_decisions.append(entry)
            prefix = "DRY-RUN " if not enforce else ""
            if isinstance(decision, SwitchDecision):
                # A redemption this tick used pre-redemption usage; do not act on
                # a switch computed from that stale snapshot. Preserve state (via
                # switch_failed) so next tick decides on fresh usage.
                if redeem_executed:
                    log.write("switch suppressed after redeem this tick provider=%s" % provider)
                    switch_failed = True
                    continue
                switched = execute_switch(decision, enforce, log)
                codex_switched = codex_switched or (provider == "codex" and switched)
                if provider == "codex":
                    codex_switch_target = decision.to_account_id
                switch_failed = switch_failed or not switched
            elif isinstance(decision, NotifyDecision):
                message = "%s%s" % (prefix, decision.message)
                log.write("%snotify provider=%s reason=%s" % (prefix, provider, decision.reason))
                if config.get("notify", True):
                    notify(
                        message,
                        "%s:%s" % (provider, decision.key),
                        state,
                        now,
                        log,
                        rotation=bool(decision.bypass_dedupe),
                    )
            elif isinstance(decision, NoneDecision):
                log.write("%snone provider=%s reason=%s" % (prefix, provider, decision.reason))
        if switch_failed:
            # Preserve transition flags/counters, but do not consume switch,
            # ladder rotation, or harvest timing until an executor actually acts.
            next_state = dataclasses.replace(
                next_state,
                last_switch_at=current_state.last_switch_at,
                last_ladder_rotation_at=current_state.last_ladder_rotation_at,
                last_harvest_at=current_state.last_harvest_at,
                harvested_resets=current_state.harvested_resets,
            )
        apply_result_state(state, provider, next_state)

    if "codex" in snapshots:
        scan_walled_codex_processes(
            config["codex"],
            state,
            snapshots["codex"],
            results["codex"],
            now,
            log,
            notifications_enabled=bool(config.get("notify", True)),
        )

    if pane_hits and "codex" in snapshots:
        target = healthy_codex_target(results["codex"], codex_switch_target)
        pointer_switched = codex_switched
        current = snapshots["codex"].active_account_id
        # A redemption this tick used pre-redemption telemetry; don't move the
        # pointer off that stale evaluation (mirrors the policy switch suppression).
        if target is not None and target != current and not pointer_switched and not codex_redeemed:
            prefix = "DRY-RUN " if not enforce else ""
            log.write("%swatcher pointer rotation target=%s" % (prefix, target))
            if enforce:
                atomic_pointer(target)
                pointer_switched = True
        if codex_redeemed:
            # A redemption may have restored an account, but the pointer/target
            # here were computed from pre-redemption telemetry and the resume
            # shim reads the (still-stale) pointer. Defer the respawn one tick so
            # it lands on the post-redemption fresh account, not the exhausted one.
            log.write("pane resume deferred: redeem this tick (respawn next tick on fresh usage)")
        else:
            resume_panes(
                pane_hits,
                target,
                config["codex"],
                state,
                now,
                enforce,
                pointer_switched,
                log,
            )

    state["snapshots"] = {name: snapshot_json(value) for name, value in snapshots.items()}
    state["updatedAt"] = now
    state["lastDecisions"] = (state["lastDecisions"] + tick_decisions)[-50:]
    atomic_write(STATE_PATH, json.dumps(state, indent=2, sort_keys=True) + "\n")
    return {"at": utc_stamp(now), "enforce": enforce, "decisions": tick_decisions}


def handle_signal(signum: int, frame: Any) -> None:
    del signum, frame
    global STOP_REQUESTED
    STOP_REQUESTED = True


def parse_args(arguments: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="run one collection and policy tick")
    parser.add_argument("--dry-run", action="store_true", help="force enforcement off")
    parser.add_argument("--status", action="store_true", help="pretty-print persisted state and exit")
    return parser.parse_args(arguments)


def main(arguments: Sequence[str]) -> int:
    args = parse_args(arguments)
    if args.status:
        print(json.dumps(merge_state(load_json(STATE_PATH, {})), indent=2, sort_keys=True))
        return 0
    try:
        config = ensure_config()
    except (OSError, ValueError, RuntimeError) as error:
        print("subswitchd: %s" % error, file=sys.stderr)
        return 2
    state = merge_state(load_json(STATE_PATH, {}))
    log = Log(LOG_PATH)
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    log.write("daemon starting once=%s forceDryRun=%s" % (args.once, args.dry_run))
    while not STOP_REQUESTED:
        try:
            summary = tick(config, state, args.dry_run, log)
            if args.once:
                print(json.dumps(summary, indent=2, sort_keys=True))
                break
        except Exception as error:  # daemon boundary: preserve next tick
            log.write("tick failed: %s: %s" % (type(error).__name__, error), "ERROR")
            if args.once:
                print("subswitchd: tick failed: %s" % error, file=sys.stderr)
                return 1
        deadline = time.monotonic() + max(1.0, float(config.get("pollSeconds", 60)))
        while not STOP_REQUESTED and time.monotonic() < deadline:
            time.sleep(min(1.0, deadline - time.monotonic()))
        try:
            config = ensure_config()
        except (OSError, ValueError, RuntimeError) as error:
            log.write("config reload failed; retaining prior config: %s" % error, "WARN")
    log.write("daemon stopped cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
