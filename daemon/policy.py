"""Pure subscription failover policy for subswitchd.

The module deliberately performs no I/O and reads no ambient time.  Callers
provide an observation, prior state, configuration, and ``now``; ``evaluate``
returns typed decisions plus a new state without mutating any input.

Window names are data, not policy.  A collector classifies each reported
window as either FIVE_HOUR or WEEKLY, so scoped model windows (for example,
"Fable") automatically receive the weekly threshold.
"""

import hashlib
import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Iterable, List, Mapping, Optional, Sequence, Tuple


class WindowKind(str, Enum):
    """Threshold family for a provider usage window."""

    FIVE_HOUR = "five_hour"
    WEEKLY = "weekly"


@dataclass(frozen=True)
class Window:
    """One known usage window, expressed as a percentage from 0 upward."""

    name: str
    used_percent: float
    kind: WindowKind
    resets_at: Optional[float] = None


@dataclass(frozen=True)
class ResetCredit:
    """One available rate-limit reset credit for an account.

    ``credit_id`` is the opaque server id passed to the consume endpoint.
    ``expires_at`` is when the credit is lost if unused (use-it-or-lose-it).
    """

    credit_id: str
    expires_at: Optional[float] = None
    reset_type: str = ""


@dataclass(frozen=True)
class AccountSnapshot:
    """Usage observation for one account.

    ``observed_at`` is when the provider usage was fresh, not merely when the
    collector ran.  For cswap, callers should use ``now-usageAgeSeconds``.
    Any error, absent windows, or observation older than ``stale_max_seconds``
    makes the account UNKNOWN.  UNKNOWN accounts are never switch targets.

    ``reset_credits`` carries the account's available reset credits (empty for
    providers/accounts without them), used by the redemption policy.
    """

    account_id: str
    windows: Tuple[Window, ...] = ()
    observed_at: Optional[float] = None
    error: Optional[str] = None
    reset_credits: Tuple[ResetCredit, ...] = ()


@dataclass(frozen=True)
class ProviderSnapshot:
    """A provider's candidate accounts and currently active account."""

    provider: str
    active_account_id: str
    accounts: Tuple[AccountSnapshot, ...]
    hard_limit_observed: bool = False


@dataclass(frozen=True)
class PolicyConfig:
    """Policy knobs for one provider."""

    five_hour_threshold: float = 95.0
    weekly_threshold: float = 95.0
    ladder_percent: float = 99.0
    cooldown_seconds: float = 300.0
    hysteresis: float = 0.05
    unknown_ticks: int = 3
    stale_max_seconds: float = 900.0
    ladder_rotation_seconds: float = 60.0
    rideable_five_hour_pct: float = 75.0
    # Claude always reports a 5h session window; a missing one there is a data
    # hole to distrust. Codex reports only a weekly window, so it opts out.
    require_five_hour: bool = True
    harvest_enabled: bool = True
    harvest_window_seconds: float = 86400.0
    harvest_min_weekly_left_pct: float = 40.0
    harvest_max_five_hour_pct: float = 50.0
    harvest_cooldown_seconds: float = 10800.0
    # Reset-credit redemption. Defaults OFF (notify-only) — turning it on is the
    # user's explicit opt-in to spend a finite resource autonomously. Thresholds
    # follow the Sol design review: starvation is a HIGH bar (true unavailability,
    # never ambiguous-unknown), and expiry-harvest executes near the last safe
    # moment so more usage accrues first.
    redeem_enabled: bool = False
    redeem_starvation: bool = True
    redeem_expiry_harvest: bool = True
    redeem_reserve: int = 0
    redeem_starvation_breach_reserve: bool = True
    redeem_starvation_pct: float = 99.0
    redeem_harvest_min_weekly_pct: float = 15.0
    redeem_expiry_notify_seconds: float = 86400.0     # 24h: notify horizon
    redeem_expiry_safety_lead_seconds: float = 3600.0  # 1h: execute window before expiry
    redeem_request_margin_seconds: float = 300.0       # require expires_at > now + margin
    redeem_cooldown_seconds: float = 3600.0


@dataclass(frozen=True)
class PolicyState:
    """Persistable policy state shared across provider evaluations."""

    last_switch_at: Mapping[str, float] = field(default_factory=dict)
    last_ladder_rotation_at: Mapping[str, float] = field(default_factory=dict)
    ladder_active: Mapping[str, bool] = field(default_factory=dict)
    unknown_active_ticks: Mapping[str, int] = field(default_factory=dict)
    last_harvest_at: Mapping[str, float] = field(default_factory=dict)
    harvested_resets: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    last_redeem_at: Mapping[str, float] = field(default_factory=dict)
    redeemed_credits: Mapping[str, float] = field(default_factory=dict)
    redeem_backoff: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class AccountEvaluation:
    """Derived account health exposed for runtime status and watcher use."""

    account_id: str
    known: bool
    score: Optional[float]
    max_raw_percent: Optional[float]
    hard_limit: bool
    unknown_reason: Optional[str] = None
    five_hour_percent: Optional[float] = None
    universal_blocked: bool = False

    @property
    def healthy(self) -> bool:
        return self.known and self.score is not None and self.score < 1.0

    @property
    def red(self) -> bool:
        return self.known and self.score is not None and self.score >= 1.0

    @property
    def session_blocked(self) -> bool:
        """The 5h session window is fully spent — the account cannot serve ANY
        request right now, even models whose weekly/scoped window has headroom."""
        return self.five_hour_percent is not None and self.five_hour_percent >= 100.0

    @property
    def fully_blocked(self) -> bool:
        """A UNIVERSAL window (5h or account-wide weekly, not a per-model scoped
        window) is at 100% — the account can serve NOTHING. Distinct from a
        merely scope-blocked account (e.g. Fable at 100% but other models free)."""
        return self.session_blocked or self.universal_blocked


@dataclass(frozen=True)
class Decision:
    """Base class for typed policy decisions."""

    provider: str
    reason: str


@dataclass(frozen=True)
class SwitchDecision(Decision):
    """Request an account switch.  The runtime decides how to execute it."""

    from_account_id: str
    to_account_id: str
    bypass_cooldown: bool = False


@dataclass(frozen=True)
class NotifyDecision(Decision):
    """Request a deduplicated operator notification."""

    key: str
    message: str
    bypass_dedupe: bool = False


@dataclass(frozen=True)
class RedeemDecision(Decision):
    """Request redemption of one reset credit on an account.

    ``idempotency_key`` is deterministic from ``credit_id`` so a retry of the
    same logical redeem reuses it and the server de-dupes (credits are
    single-use). The runtime performs the HTTP consume.
    """

    account_id: str
    credit_id: str
    idempotency_key: str


@dataclass(frozen=True)
class NoneDecision(Decision):
    """Explain why no switch or notification is requested this tick."""

    pass


@dataclass(frozen=True)
class PolicyResult:
    """Complete, deterministic result of one provider evaluation."""

    decisions: Tuple[Decision, ...]
    next_state: PolicyState
    accounts: Tuple[AccountEvaluation, ...]


def evaluate_account(
    account: AccountSnapshot, config: PolicyConfig, now: float
) -> AccountEvaluation:
    """Classify one account without consulting any other account."""

    if account.error:
        return AccountEvaluation(
            account.account_id, False, None, None, False, "error: %s" % account.error
        )
    if account.observed_at is None:
        return AccountEvaluation(
            account.account_id, False, None, None, False, "missing usage timestamp"
        )
    age = max(0.0, now - account.observed_at)
    if age > config.stale_max_seconds:
        return AccountEvaluation(
            account.account_id,
            False,
            None,
            None,
            False,
            "stale usage (%.0fs)" % age,
        )
    if not account.windows:
        return AccountEvaluation(
            account.account_id, False, None, None, False, "missing usage windows"
        )

    scores = []
    raw = []
    five_hour_percent: Optional[float] = None
    universal_blocked = False
    for window in account.windows:
        threshold = (
            config.five_hour_threshold
            if window.kind == WindowKind.FIVE_HOUR
            else config.weekly_threshold
        )
        # A non-positive threshold is invalid configuration.  Treat the
        # account as unknown rather than inventing headroom or dividing by 0.
        if threshold <= 0.0:
            return AccountEvaluation(
                account.account_id,
                False,
                None,
                None,
                False,
                "invalid threshold for %s" % window.kind.value,
            )
        scores.append(window.used_percent / threshold)
        raw.append(window.used_percent)
        if window.kind == WindowKind.FIVE_HOUR:
            five_hour_percent = (
                window.used_percent if five_hour_percent is None
                else max(five_hour_percent, window.used_percent)
            )
        # A UNIVERSAL window at 100% blocks everything; a scoped per-model window
        # (name "scoped:...") at 100% only blocks that model.
        if window.used_percent >= 100.0 and not window.name.startswith("scoped:"):
            universal_blocked = True

    # For providers that always report a 5h session window (Claude), its absence
    # means we can't tell a session-blocked account from a healthy one, so a
    # weekly-only snapshot is UNKNOWN. Providers WITHOUT a 5h concept (Codex only
    # reports a weekly window) set require_five_hour=False and are unaffected.
    if config.require_five_hour and five_hour_percent is None:
        return AccountEvaluation(
            account.account_id, False, None, None, False, "missing 5h session window"
        )

    return AccountEvaluation(
        account.account_id,
        True,
        max(scores),
        max(raw),
        any(value >= 100.0 for value in raw),
        None,
        five_hour_percent,
        universal_blocked,
    )


def healthy_candidates(result: PolicyResult) -> Tuple[AccountEvaluation, ...]:
    """Return known sub-threshold accounts, ordered by score then id."""

    return tuple(
        sorted(
            (account for account in result.accounts if account.healthy),
            key=_score_key,
        )
    )


def _soonest_weekly_reset(account: AccountSnapshot) -> Optional[Window]:
    """Return an account's next known weekly reset, if it has one."""

    weekly = [
        window
        for window in account.windows
        if window.kind == WindowKind.WEEKLY and window.resets_at is not None
    ]
    if not weekly:
        return None
    return min(weekly, key=lambda window: window.resets_at or float("inf"))


def _reset_score_key(
    account: AccountEvaluation,
    snapshots: Mapping[str, AccountSnapshot],
    config: PolicyConfig,
    now: float,
) -> Tuple[int, float, float, str]:
    """Prefer rideable accounts whose weekly quota is about to reset.

    Two tiers: an account whose 5h window is mostly spent (>= rideable
    cutoff) is not worth switching TO even if its weekly resets soonest —
    it would go red again within minutes. Such accounts rank behind every
    rideable one, still ordered by reset then score.
    """

    snapshot = snapshots[account.account_id]
    five_hour = [
        window.used_percent
        for window in snapshot.windows
        if window.kind == WindowKind.FIVE_HOUR
    ]
    tier = 1 if five_hour and max(five_hour) >= config.rideable_five_hour_pct else 0
    window = _soonest_weekly_reset(snapshot)
    resets_at = window.resets_at if window is not None else None
    # UNCAPPED on purpose: when a switch happens anyway, always prefer the
    # candidate whose weekly quota expires soonest (use-it-or-lose-it), no
    # matter how far away the reset is. The 24h harvest window only gates
    # PROACTIVE harvest switches, never target ranking.
    if resets_at is None or resets_at < now:
        resets_at = float("inf")
    assert account.score is not None
    return (tier, resets_at, account.score, account.account_id)


def _harvest_window(
    account: AccountEvaluation,
    snapshot: AccountSnapshot,
    config: PolicyConfig,
    now: float,
) -> Optional[Window]:
    """Return the reset window when an otherwise healthy account can harvest."""

    if not account.healthy:
        return None
    weekly = _soonest_weekly_reset(snapshot)
    if weekly is None or weekly.resets_at is None:
        return None
    if weekly.resets_at < now or weekly.resets_at - now > config.harvest_window_seconds:
        return None
    if weekly.used_percent > 100.0 - config.harvest_min_weekly_left_pct:
        return None
    if any(
        window.used_percent >= config.harvest_max_five_hour_pct
        for window in snapshot.windows
        if window.kind == WindowKind.FIVE_HOUR
    ):
        return None
    return weekly


def _window_of(snapshot: AccountSnapshot, kind: "WindowKind") -> Optional[Window]:
    for window in snapshot.windows:
        if window.kind == kind:
            return window
    return None


# Only reset types whose reset behaviour is observed/understood may be redeemed
# automatically. 0.144.6 recognizes exactly "codex_rate_limits" (Weekly + 5h);
# any other/unknown type stays notify-only until validated (Sol review).
REDEEMABLE_RESET_TYPES = frozenset({"codex_rate_limits"})


def _idempotency_key(credit_id: str) -> str:
    """Deterministic UUID from a credit id.

    Crash-safe by construction: a retry of the same logical redeem (same credit)
    always yields the same request id, so the server de-dupes it — no need to
    persist a random UUID before the POST to avoid a double-spend.
    """
    digest = hashlib.sha256(("subswitch-redeem:" + credit_id).encode("utf-8")).digest()
    return str(uuid.UUID(bytes=digest[:16]))


def evaluate_credits(
    snapshot: ProviderSnapshot,
    config: PolicyConfig,
    state: PolicyState,
    now: float,
) -> Tuple[Tuple[Decision, ...], PolicyState]:
    """Decide whether to redeem ONE reset credit this tick (pure).

    Two triggers, at most one redeem per tick:
      A. Starvation — EVERY account is truly unavailable (weekly at/over the
         high starvation bar), with no ambiguous-unknown account that might
         still be healthy. The credit type in play resets both weekly and 5h,
         so a maxed 5h is NOT a reason to exclude an account. Selection follows
         Sol's ordering: a credit that would otherwise expire first, then
         earliest expiry, then furthest natural recovery, then the active
         account, then id.
      B. Expiry-harvest — a credit will expire before its account's natural
         reset (use-it-or-lose-it) and the account has used enough quota to
         make the reset worth it. It is NOTIFIED early but only EXECUTED within
         a safety lead of expiry, so more usage accrues first.

    When ``redeem_enabled`` is False (default) every decision is surfaced as a
    notification instead of a redemption. Only allowlisted reset types and
    unexpired credits are ever eligible.
    """
    provider = snapshot.provider
    last_redeem = dict(state.last_redeem_at)
    redeemed = dict(state.redeemed_credits)

    backoff = dict(state.redeem_backoff)
    present_ids = {c.credit_id for a in snapshot.accounts for c in a.reset_credits}
    redeemed = {cid: t for cid, t in redeemed.items() if cid in present_ids}
    backoff = {cid: t for cid, t in backoff.items() if cid in present_ids}
    unchanged = replace(
        state, last_redeem_at=last_redeem, redeemed_credits=redeemed, redeem_backoff=backoff
    )

    def eligible(account: AccountSnapshot) -> Tuple[ResetCredit, ...]:
        return tuple(
            c for c in account.reset_credits
            if c.credit_id not in redeemed
            and backoff.get(c.credit_id, 0.0) <= now
            and c.reset_type in REDEEMABLE_RESET_TYPES
            and (c.expires_at is None
                 or c.expires_at > now + config.redeem_request_margin_seconds)
        )

    infos = [
        (a, _window_of(a, WindowKind.WEEKLY), _window_of(a, WindowKind.FIVE_HOUR), eligible(a))
        for a in snapshot.accounts
    ]

    def cooldown_ok(account_id: str) -> bool:
        return now - last_redeem.get(account_id, float("-inf")) >= config.redeem_cooldown_seconds

    def expires_before_natural(credit: ResetCredit, weekly: Optional[Window]) -> bool:
        # A credit that will be LOST before its account's own weekly reset — the
        # only credits that carry use-it-or-lose-it urgency. Independent of the
        # notification horizon (that only decides WHEN to warn/execute).
        if credit.expires_at is None:
            return False
        return weekly is None or weekly.resets_at is None or credit.expires_at < weekly.resets_at

    def within_notify(credit: ResetCredit) -> bool:
        return credit.expires_at is not None and (
            credit.expires_at - now <= config.redeem_expiry_notify_seconds
        )

    chosen: Optional[Tuple[AccountSnapshot, ResetCredit, str]] = None

    # --- Trigger A: hard starvation --------------------------------------------
    if config.redeem_starvation:
        # Require EVERY account to be a fresh, error-free, weekly observation at
        # or above the bar. A single errored/unknown account might still be
        # healthy, so it blocks starvation rather than being ignored.
        # Any account that is not a fresh, error-free, windowed observation
        # blocks starvation — a stale/errored account might actually be healthy,
        # and spending a finite credit on a false "everyone is dead" is costly.
        blocking = any(
            w is None
            or a.error is not None
            or a.observed_at is None
            or (now - a.observed_at) > config.stale_max_seconds
            for (a, w, _, _) in infos
        )
        starved = (
            bool(infos)
            and not blocking
            and all(w.used_percent >= config.redeem_starvation_pct for (_, w, _, _) in infos)
        )
        if starved:
            pairs = [
                (a, w, c)
                for (a, w, _, cs) in infos if cooldown_ok(a.account_id)
                for c in cs
            ]
            if pairs:
                def _star_key(item):
                    a, w, c = item
                    natural = w.resets_at if (w and w.resets_at is not None) else float("inf")
                    return (
                        0 if expires_before_natural(c, w) else 1,     # save doomed credits first
                        c.expires_at if c.expires_at is not None else float("inf"),
                        -natural,                                     # furthest natural recovery
                        0 if a.account_id == snapshot.active_account_id else 1,
                        c.credit_id,
                    )
                candidate = min(pairs, key=_star_key)
                # Reserve is computed AFTER the candidate is (hypothetically)
                # consumed, and a doomed credit never counts toward it.
                remaining_non_doomed = sum(
                    1 for (_, w2, _, cs) in infos for c2 in cs
                    if not expires_before_natural(c2, w2)
                    and not (c2.credit_id == candidate[2].credit_id)
                )
                reserve_ok = (
                    config.redeem_starvation_breach_reserve
                    or remaining_non_doomed >= config.redeem_reserve
                )
                if reserve_ok:
                    a, w, c = candidate
                    chosen = (a, c, "starvation: all accounts weekly-exhausted; restoring %s" % a.account_id)

    # --- Trigger B: expiry-harvest ---------------------------------------------
    if chosen is None and config.redeem_expiry_harvest:
        doomed = [
            (a, w, c)
            for (a, w, _, cs) in infos
            if w is not None and cooldown_ok(a.account_id)
            and w.used_percent >= config.redeem_harvest_min_weekly_pct
            for c in cs if expires_before_natural(c, w) and within_notify(c)
        ]
        if doomed:
            executable = [
                (a, w, c) for (a, w, c) in doomed
                if c.expires_at is not None
                and c.expires_at - now <= config.redeem_expiry_safety_lead_seconds
            ]
            if executable:
                a, w, c = min(executable, key=lambda t: (t[2].expires_at, t[0].account_id))
                chosen = (a, c, "expiry-harvest: credit expiring now with %d%% weekly used"
                          % int(w.used_percent))
            else:
                # Not yet the safe moment — notify so nothing is lost silently.
                a, w, c = min(doomed, key=lambda t: (t[2].expires_at or float("inf"), t[0].account_id))
                notice = NotifyDecision(
                    provider,
                    "reset credit on %s expires soon; will redeem near expiry" % a.account_id,
                    key="redeem-pending:%s:%s" % (a.account_id, c.credit_id),
                    message="subswitch: a reset credit on %s expires soon and will be "
                    "redeemed near its expiry (%d%% weekly used)." % (a.account_id, int(w.used_percent)),
                )
                return (notice,), unchanged

    if chosen is None:
        return tuple(), unchanged

    account, credit, reason = chosen
    dedupe_key = "redeem:%s:%s" % (account.account_id, credit.credit_id)
    if config.redeem_enabled:
        # State is NOT mutated on emit: the executor records the spend only on a
        # terminal HTTP outcome, so a timeout/5xx re-emits the SAME (deterministic
        # UUID) next tick and the server de-dupes. Marking here would strand a
        # failed POST as "already spent".
        decision = RedeemDecision(
            provider, reason, account.account_id, credit.credit_id,
            _idempotency_key(credit.credit_id),
        )
        return (decision,), unchanged
    notice = NotifyDecision(
        provider,
        "auto-redeem is OFF; " + reason,
        key=dedupe_key,
        message="subswitch WOULD redeem a reset credit on %s (%s). Enable redeem to act."
        % (account.account_id, reason),
    )
    return (notice,), unchanged


def evaluate(
    snapshot: ProviderSnapshot,
    state: PolicyState,
    config: PolicyConfig,
    now: float,
) -> PolicyResult:
    """Evaluate one provider observation and return decisions plus next state.

    A switch timestamp is recorded when a switch is *decided*.  If execution
    fails, the runtime should retain the old timestamp so the next tick can
    retry (notably cswap's retryable credential-lock timeout).
    """

    # Self-configuring 5h requirement: only distrust a missing 5h window when the
    # provider actually reports one (Claude does; Codex reports weekly only). This
    # makes the invariant automatic — no config key or migration can leave an
    # upgraded Codex install with every account wrongly UNKNOWN.
    provider_reports_five_hour = any(
        window.kind == WindowKind.FIVE_HOUR
        for account in snapshot.accounts
        for window in account.windows
    )
    effective_config = (
        config if provider_reports_five_hour
        else replace(config, require_five_hour=False)
    )
    evaluations = tuple(
        evaluate_account(account, effective_config, now) for account in snapshot.accounts
    )
    by_id = {account.account_id: account for account in evaluations}
    active = by_id.get(snapshot.active_account_id)
    provider = snapshot.provider

    last_switch = dict(state.last_switch_at)
    last_rotation = dict(state.last_ladder_rotation_at)
    ladders = dict(state.ladder_active)
    unknown_ticks = dict(state.unknown_active_ticks)
    last_harvest = dict(state.last_harvest_at)
    harvested_resets = {
        name: dict(resets) for name, resets in state.harvested_resets.items()
    }
    was_ladder = bool(ladders.get(provider, False))
    started_in_ladder = was_ladder
    decisions = []  # type: List[Decision]

    known = [account for account in evaluations if account.known]
    snapshots_by_id = {account.account_id: account for account in snapshot.accounts}
    healthy = sorted(
        (account for account in known if account.healthy),
        key=lambda account: _reset_score_key(account, snapshots_by_id, config, now),
    )
    alternatives = [
        account
        for account in healthy
        if account.account_id != snapshot.active_account_id
    ]

    def _escape_target() -> Optional[AccountEvaluation]:
        """Best account to flee to when the active MUST leave.

        Priority: a healthy account, then a red-but-serviceable one (5h not
        spent — can still serve *something*), and only as a last resort a
        session-blocked one (every option is dead). This guarantees the
        fallthrough paths never strand the user, and never pick a useless
        target while a serviceable one exists.
        """
        if alternatives:
            return alternatives[0]
        serviceable_red = sorted(
            (a for a in known
             if a.account_id != snapshot.active_account_id and not a.fully_blocked),
            key=_raw_key,
        )
        if serviceable_red:
            return serviceable_red[0]
        other = sorted(
            (a for a in known if a.account_id != snapshot.active_account_id),
            key=_raw_key,
        )
        return other[0] if other else None

    # A reset ends ladder mode even when the active account itself is UNKNOWN.
    # Recovery is emitted once because next_state clears the ladder flag.
    if was_ladder and healthy:
        ladders[provider] = False
        decisions.append(
            NotifyDecision(
                provider=provider,
                reason="a candidate account is below threshold again",
                key="ladder-recovery",
                message="%s usage recovered; leaving the all-red ladder." % provider,
            )
        )
        was_ladder = False

    # An observed usage-limit message is authoritative even when the active
    # telemetry still looks fine — flee to the best serviceable account (not
    # only a strictly-healthy one), so a red-but-serviceable alternative is
    # still honored instead of the signal being dropped.
    observed_target = _escape_target() if snapshot.hard_limit_observed else None
    if observed_target is not None:
        if active is not None and active.known:
            unknown_ticks[provider] = 0
        else:
            unknown_ticks[provider] = unknown_ticks.get(provider, 0) + 1
        decisions.append(
            SwitchDecision(
                provider,
                "hard limit: observed usage-limit error",
                snapshot.active_account_id,
                observed_target.account_id,
                True,
            )
        )
        last_switch[provider] = now
        return _result(
            decisions, last_switch, last_rotation, ladders, unknown_ticks, last_harvest,
            harvested_resets, evaluations
        )

    if active is None:
        streak = unknown_ticks.get(provider, 0) + 1
        unknown_ticks[provider] = streak
        escape = _escape_target() if streak >= config.unknown_ticks else None
        if escape is not None:
            decisions.append(
                SwitchDecision(
                    provider,
                    "active account absent for %d consecutive cycles" % streak,
                    snapshot.active_account_id,
                    escape.account_id,
                    True,
                )
            )
            decisions.append(
                NotifyDecision(
                    provider=provider,
                    reason="active account absent; usable account available",
                    key="unknown-failover:%s" % snapshot.active_account_id,
                    message="Active %s account %s is absent from usage data; failing over to %s."
                    % (provider, snapshot.active_account_id, escape.account_id),
                )
            )
            last_switch[provider] = now
        else:
            reason = "active account is absent from candidate snapshot"
            if not known:
                reason += "; no usable switch target"
            else:
                reason += "; failover streak %d/%d" % (streak, config.unknown_ticks)
            decisions.append(NoneDecision(provider, reason))
        return _result(
            decisions, last_switch, last_rotation, ladders, unknown_ticks, last_harvest,
            harvested_resets, evaluations
        )

    if active.known:
        unknown_ticks[provider] = 0
    else:
        streak = unknown_ticks.get(provider, 0) + 1
        unknown_ticks[provider] = streak
        escape = _escape_target() if streak >= config.unknown_ticks else None
        if escape is not None:
            decisions.append(
                SwitchDecision(
                    provider,
                    "active usage unknown for %d consecutive cycles" % streak,
                    active.account_id,
                    escape.account_id,
                    True,
                )
            )
            decisions.append(
                NotifyDecision(
                    provider=provider,
                    reason="active usage unknown; usable account available",
                    key="unknown-failover:%s" % active.account_id,
                    message="Active %s account %s has unknown usage; failing over to %s."
                    % (provider, active.account_id, escape.account_id),
                )
            )
            last_switch[provider] = now
        else:
            reason = active.unknown_reason or "active usage unknown"
            if not known:
                reason += "; no usable switch target"
            else:
                reason += "; failover streak %d/%d" % (streak, config.unknown_ticks)
            decisions.append(NoneDecision(provider, reason))
        return _result(
            decisions, last_switch, last_rotation, ladders, unknown_ticks, last_harvest,
            harvested_resets, evaluations
        )

    # A window at 100% blocks the active account now — leave immediately to the
    # best usable account (serviceable red counts), never wait out the ladder
    # interval, and never move to a session-blocked target unless all are dead.
    forced_target = _escape_target() if active.hard_limit else None
    # Only take the immediate escape to a target that is a REAL improvement.
    # Fleeing one 100%-blocked account for another equally-maxed one just
    # ping-pongs every tick — defer that to the paced all-red ladder instead.
    if (
        forced_target is not None
        and not (forced_target.fully_blocked and not active.fully_blocked)
        and not (forced_target.hard_limit and active.hard_limit)
    ):
        decisions.append(
            SwitchDecision(
                provider,
                "hard limit: window reached 100%",
                active.account_id,
                forced_target.account_id,
                True,
            )
        )
        decisions.append(
            NotifyDecision(
                provider=provider,
                reason="hard limit reached",
                key="switch:%s:%s" % (active.account_id, forced_target.account_id),
                message="%s hit a hard limit on %s; switched to %s."
                % (provider, active.account_id, forced_target.account_id),
                bypass_dedupe=True,
            )
        )
        last_switch[provider] = now
        return _result(
            decisions, last_switch, last_rotation, ladders, unknown_ticks, last_harvest,
            harvested_resets, evaluations
        )

    if active.red and alternatives:
        # Prefer the reset-aware first choice (alternatives[0]); but if it only
        # marginally beats the active (below hysteresis) while a different
        # alternative offers a large improvement, take that one instead — so a
        # dramatically better target is never skipped for a marginal sort winner.
        reset_first = alternatives[0]
        best_headroom = min(alternatives, key=lambda a: (a.score, a.account_id))
        cooldown_ready = now - last_switch.get(provider, float("-inf")) >= config.cooldown_seconds
        if active.score - reset_first.score >= config.hysteresis:  # type: ignore[operator]
            target = reset_first
        else:
            target = best_headroom
        improvement = active.score - target.score  # type: ignore[operator]
        if cooldown_ready and improvement >= config.hysteresis:
            decisions.append(
                SwitchDecision(
                    provider,
                    "active score %.3f is red; target %.3f improves by %.3f"
                    % (active.score, target.score, improvement),
                    active.account_id,
                    target.account_id,
                )
            )
            decisions.append(
                NotifyDecision(
                    provider=provider,
                    reason="routine threshold switch",
                    key="switch:%s:%s" % (active.account_id, target.account_id),
                    message="%s switched %s → %s (threshold crossed)."
                    % (provider, active.account_id, target.account_id),
                    bypass_dedupe=True,
                )
            )
            last_switch[provider] = now
        elif not cooldown_ready:
            decisions.append(NoneDecision(provider, "active red; provider cooldown active"))
        else:
            decisions.append(
                NoneDecision(
                    provider,
                    "active red; best healthy account does not beat hysteresis %.3f"
                    % config.hysteresis,
                )
            )
        return _result(
            decisions, last_switch, last_rotation, ladders, unknown_ticks, last_harvest,
            harvested_resets, evaluations
        )

    # All-red means there are known accounts, but none has score < 1. UNKNOWN
    # accounts do not count as headroom and are never selected.
    all_red = bool(known) and not healthy
    if all_red:
        entering = not bool(ladders.get(provider, False))
        ladders[provider] = True
        if entering:
            decisions.append(
                NotifyDecision(
                    provider=provider,
                    reason="all known accounts are red",
                    key="ladder-entry",
                    message="All known %s accounts are above threshold; entering the all-red ladder."
                    % provider,
                )
            )

        other_known = sorted(
            (account for account in known if account.account_id != active.account_id),
            key=_raw_key,
        )
        # A still-serviceable active is never traded for a session-dead account:
        # only offer non-blocked others (unless the active itself is blocked, in
        # which case rotating among blocked accounts is the least-bad option).
        usable_others = [
            account for account in other_known
            if not account.fully_blocked or active.fully_blocked
        ]
        rotation_due = (
            active.max_raw_percent is not None
            and active.max_raw_percent >= config.ladder_percent
        ) or snapshot.hard_limit_observed or active.hard_limit
        interval_ready = (
            now - last_rotation.get(provider, float("-inf"))
            >= config.ladder_rotation_seconds
        )

        # Only rotate to a target that is strictly better than the active by the
        # same ranking — never ping-pong between two equally-maxed accounts
        # (unless an observed usage-limit forces us off the active regardless).
        better_others = [
            account for account in usable_others
            if snapshot.hard_limit_observed or _raw_key(account) < _raw_key(active)
        ]
        if rotation_due and interval_ready and better_others:
            target = better_others[0]
            if snapshot.hard_limit_observed:
                rotation_reason = "all-red ladder rotation after observed usage-limit error"
            elif active.hard_limit:
                rotation_reason = "all-red ladder rotation after a window reached 100%"
            else:
                rotation_reason = "all-red ladder rotation at %.1f%% raw usage" % (
                    active.max_raw_percent or 0.0
                )
            decisions.append(
                SwitchDecision(
                    provider,
                    rotation_reason,
                    active.account_id,
                    target.account_id,
                    True,
                )
            )
            decisions.append(
                NotifyDecision(
                    provider=provider,
                    reason="riding account reached ladder rotation point",
                    key="ladder-rotation:%s:%s"
                    % (active.account_id, target.account_id),
                    message="%s all-red ladder rotating from %s to %s."
                    % (provider, active.account_id, target.account_id),
                    bypass_dedupe=True,
                )
            )
            last_switch[provider] = now
            last_rotation[provider] = now
        elif not rotation_due and other_known:
            # Enter (or keep) the ladder on the most serviceable account (lowest
            # _raw_key ranks non-session-blocked first). min over ALL known
            # includes the active, so a serviceable active stays put instead of
            # being traded for a session-dead one.
            best = min(known, key=_raw_key)
            if best.account_id != active.account_id and (
                not best.fully_blocked or active.fully_blocked
            ):
                decisions.append(
                    SwitchDecision(
                        provider,
                        "all-red ladder riding lowest max-raw account",
                        active.account_id,
                        best.account_id,
                        True,
                    )
                )
                last_switch[provider] = now

        if not decisions:
            decisions.append(NoneDecision(provider, "all-red ladder: staying on current account"))
        return _result(
            decisions, last_switch, last_rotation, ladders, unknown_ticks, last_harvest,
            harvested_resets, evaluations
        )

    if active.healthy and not started_in_ladder and config.harvest_enabled:
        cooldown_ready = (
            now - last_harvest.get(provider, float("-inf"))
            >= config.harvest_cooldown_seconds
        )
        provider_harvests = harvested_resets.setdefault(provider, {})
        harvestable = []
        for candidate in healthy:
            window = _harvest_window(
                candidate, snapshots_by_id[candidate.account_id], config, now
            )
            if (
                candidate.account_id != active.account_id
                and window is not None
                and provider_harvests.get(candidate.account_id) != window.resets_at
            ):
                harvestable.append((candidate, window))

        if cooldown_ready and harvestable:
            target, target_window = min(
                harvestable,
                key=lambda item: (
                    item[1].resets_at or float("inf"),
                    item[0].score or float("inf"),
                    item[0].account_id,
                ),
            )
            active_window = _harvest_window(
                active, snapshots_by_id[active.account_id], config, now
            )
            if (
                active_window is not None
                and active_window.resets_at is not None
                and active_window.resets_at <= (target_window.resets_at or float("inf"))
            ):
                provider_harvests[active.account_id] = active_window.resets_at
                decisions.append(
                    NoneDecision(provider, "active account is the harvest target")
                )
            else:
                assert target_window.resets_at is not None
                hours = (target_window.resets_at - now) / 3600.0
                left = 100.0 - target_window.used_percent
                reason = "harvest: weekly resets in %dh with %d%% left" % (
                    int(hours), int(left)
                )
                decisions.append(
                    SwitchDecision(
                        provider,
                        reason,
                        active.account_id,
                        target.account_id,
                    )
                )
                decisions.append(
                    NotifyDecision(
                        provider=provider,
                        reason=reason,
                        key="harvest:%s:%s" % (target.account_id, target_window.resets_at),
                        message=(
                            "%s harvesting weekly quota on %s: reset in %dh with %d%% left."
                            % (provider, target.account_id, int(hours), int(left))
                        ),
                    )
                )
                last_harvest[provider] = now
                provider_harvests[target.account_id] = target_window.resets_at
        else:
            decisions.append(NoneDecision(provider, "active account is below all thresholds"))
    elif active.healthy:
        decisions.append(NoneDecision(provider, "active account is below all thresholds"))
    else:
        decisions.append(NoneDecision(provider, "no known-good switch target"))
    return _result(
        decisions, last_switch, last_rotation, ladders, unknown_ticks, last_harvest,
        harvested_resets, evaluations
    )


def _score_key(account: AccountEvaluation) -> Tuple[float, str]:
    assert account.score is not None
    return (account.score, account.account_id)


def _raw_key(account: AccountEvaluation) -> Tuple[int, float, float, str]:
    """Ladder ranking — which red account to ride / rotate to.

    A 5h-session-blocked account can't serve ANY request, so it sorts strictly
    last (tier 1). Among accounts that can still serve, prefer the one with the
    most immediate session headroom (lowest 5h), then the lowest overall usage,
    then a stable id. This stops the ladder from parking on a session-dead
    account while a usable one is available.
    """
    assert account.max_raw_percent is not None
    # Unknown 5h state must not be treated as spare capacity — rank it after
    # every account with measured session headroom (but not as "blocked").
    five_hour = (
        account.five_hour_percent if account.five_hour_percent is not None else float("inf")
    )
    return (
        1 if account.fully_blocked else 0,     # a universal window at 100%: serves nothing → last
        1 if account.hard_limit else 0,        # any (incl. scoped) window at 100% → after clean reds
        five_hour,
        account.max_raw_percent,
        account.account_id,
    )


def _result(
    decisions: Sequence[Decision],
    last_switch: Mapping[str, float],
    last_rotation: Mapping[str, float],
    ladders: Mapping[str, bool],
    unknown_ticks: Mapping[str, int],
    last_harvest: Mapping[str, float],
    harvested_resets: Mapping[str, Mapping[str, float]],
    evaluations: Iterable[AccountEvaluation],
) -> PolicyResult:
    return PolicyResult(
        tuple(decisions),
        PolicyState(
            dict(last_switch),
            dict(last_rotation),
            dict(ladders),
            dict(unknown_ticks),
            dict(last_harvest),
            {provider: dict(resets) for provider, resets in harvested_resets.items()},
        ),
        tuple(evaluations),
    )
