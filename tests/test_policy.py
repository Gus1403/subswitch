"""Dry-run unit coverage for the pure subswitch policy engine."""

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "daemon"))

from policy import (  # noqa: E402
    AccountSnapshot,
    NoneDecision,
    NotifyDecision,
    PolicyConfig,
    PolicyState,
    ProviderSnapshot,
    ResetCredit,
    RedeemDecision,
    SwitchDecision,
    Window,
    WindowKind,
    evaluate,
    evaluate_credits,
)


NOW = 1000.0


def account(
    identifier,
    five=None,
    weekly=None,
    scoped=None,
    observed_at=NOW,
    error=None,
    five_resets_at=None,
    weekly_resets_at=None,
    scoped_resets_at=None,
):
    windows = []
    if five is not None:
        windows.append(Window("fiveHour", five, WindowKind.FIVE_HOUR, five_resets_at))
    if weekly is not None:
        windows.append(Window("sevenDay", weekly, WindowKind.WEEKLY, weekly_resets_at))
    if scoped is not None:
        windows.append(Window("scoped:Fable", scoped, WindowKind.WEEKLY, scoped_resets_at))
    return AccountSnapshot(identifier, tuple(windows), observed_at, error)


def snapshot(active, *accounts, hard_limit_observed=False):
    return ProviderSnapshot("claude", active, tuple(accounts), hard_limit_observed)


def decisions_of(result, kind):
    return [decision for decision in result.decisions if isinstance(decision, kind)]


class PolicyTransitionsTest(unittest.TestCase):
    def setUp(self):
        self.config = PolicyConfig()

    def check(self, active, *accounts, state=None, now=NOW, hard=False):
        return evaluate(
            snapshot(active, *accounts, hard_limit_observed=hard),
            state or PolicyState(),
            self.config,
            now,
        )

    def test_below_threshold_has_no_switch(self):
        result = self.check("one", account("one", five=89), account("two", five=20))
        self.assertEqual([], decisions_of(result, SwitchDecision))
        self.assertIsInstance(result.decisions[0], NoneDecision)

    def test_five_hour_threshold_switches_with_hysteresis(self):
        result = self.check("one", account("one", five=95), account("two", five=70))
        switch = decisions_of(result, SwitchDecision)[0]
        self.assertEqual(("one", "two"), (switch.from_account_id, switch.to_account_id))
        self.assertFalse(switch.bypass_cooldown)

    def test_red_account_does_not_switch_without_hysteresis_improvement(self):
        # 95/95 - 91/95 = 0.042 is less than the configured 0.05 score improvement.
        result = self.check("one", account("one", five=95), account("two", five=91))
        self.assertEqual([], decisions_of(result, SwitchDecision))
        self.assertIn("hysteresis", result.decisions[0].reason)

    def test_cooldown_blocks_then_expiry_allows_switch(self):
        state = PolicyState(last_switch_at={"claude": NOW - 299})
        blocked = self.check("one", account("one", five=95), account("two", five=70), state=state)
        self.assertEqual([], decisions_of(blocked, SwitchDecision))
        allowed = self.check(
            "one", account("one", five=95), account("two", five=70), state=state, now=NOW + 1
        )
        self.assertEqual("two", decisions_of(allowed, SwitchDecision)[0].to_account_id)

    def test_weekly_threshold_crossing_switches(self):
        result = self.check("one", account("one", five=10, weekly=95), account("two", five=10, weekly=30))
        self.assertEqual("two", decisions_of(result, SwitchDecision)[0].to_account_id)

    def test_scoped_weekly_window_crossing_switches(self):
        result = self.check("one", account("one", five=10, scoped=95), account("two", five=10, scoped=30))
        self.assertEqual("two", decisions_of(result, SwitchDecision)[0].to_account_id)

    def test_window_hard_limit_bypasses_cooldown(self):
        state = PolicyState(last_switch_at={"claude": NOW})
        result = self.check("one", account("one", five=100), account("two", five=70), state=state)
        switch = decisions_of(result, SwitchDecision)[0]
        self.assertTrue(switch.bypass_cooldown)
        self.assertIn("100%", switch.reason)

    def test_observed_hard_limit_switches_even_when_active_usage_is_healthy(self):
        state = PolicyState(last_switch_at={"claude": NOW})
        result = self.check(
            "one", account("one", five=10), account("two", five=20), state=state, hard=True
        )
        switch = decisions_of(result, SwitchDecision)[0]
        self.assertEqual("two", switch.to_account_id)
        self.assertTrue(switch.bypass_cooldown)

    def test_all_red_entry_notifies_exactly_once_on_consecutive_ticks(self):
        first = self.check("one", account("one", five=96), account("two", five=97))
        self.assertEqual(1, len(decisions_of(first, NotifyDecision)))
        second = self.check(
            "one", account("one", five=96), account("two", five=97), state=first.next_state, now=NOW + 1
        )
        self.assertEqual([], decisions_of(second, NotifyDecision))

    def test_all_red_rides_lowest_max_raw_account(self):
        result = self.check("one", account("one", five=98), account("two", five=96))
        switch = decisions_of(result, SwitchDecision)[0]
        self.assertEqual("two", switch.to_account_id)
        self.assertIn("riding", switch.reason)

    def test_ladder_rotates_at_99_percent_after_rotation_interval(self):
        result = self.check("one", account("one", five=99), account("two", five=98))
        switch = decisions_of(result, SwitchDecision)[0]
        notices = decisions_of(result, NotifyDecision)
        self.assertEqual("two", switch.to_account_id)
        self.assertTrue(switch.bypass_cooldown)
        self.assertTrue(notices[-1].bypass_dedupe)

    def test_ladder_rotation_is_blocked_inside_sixty_seconds(self):
        state = PolicyState(ladder_active={"claude": True}, last_ladder_rotation_at={"claude": NOW - 59})
        result = self.check("one", account("one", five=99), account("two", five=98), state=state)
        self.assertEqual([], decisions_of(result, SwitchDecision))
        self.assertEqual([], decisions_of(result, NotifyDecision))

    def test_ladder_recovery_notifies_and_clears_flag(self):
        state = PolicyState(ladder_active={"claude": True})
        result = self.check("one", account("one", five=80), account("two", five=96), state=state)
        notices = decisions_of(result, NotifyDecision)
        self.assertEqual("ladder-recovery", notices[0].key)
        self.assertFalse(result.next_state.ladder_active["claude"])

    def test_unknown_active_fails_over_on_third_tick_and_notifies(self):
        state = PolicyState()
        for tick in (1, 2):
            result = self.check("missing", account("two", five=20), state=state, now=NOW + tick)
            self.assertEqual([], decisions_of(result, SwitchDecision))
            state = result.next_state
        result = self.check("missing", account("two", five=20), state=state, now=NOW + 3)
        self.assertEqual("two", decisions_of(result, SwitchDecision)[0].to_account_id)
        self.assertEqual(1, len(decisions_of(result, NotifyDecision)))

    def test_unknown_active_does_not_switch_without_candidate(self):
        state = PolicyState(unknown_active_ticks={"claude": 2})
        result = self.check("missing", account("broken", error="collector error"), state=state)
        self.assertEqual([], decisions_of(result, SwitchDecision))
        self.assertEqual(3, result.next_state.unknown_active_ticks["claude"])

    def test_unknown_streak_resets_when_active_becomes_known(self):
        state = PolicyState(unknown_active_ticks={"claude": 2})
        result = self.check("one", account("one", five=20), account("two", five=30), state=state)
        self.assertEqual(0, result.next_state.unknown_active_ticks["claude"])

    def test_stale_usage_is_unknown_and_never_a_target(self):
        stale = account("two", five=20, observed_at=NOW - self.config.stale_max_seconds - 1)
        result = self.check("one", account("one", five=90), stale)
        self.assertEqual([], decisions_of(result, SwitchDecision))
        evaluation = next(item for item in result.accounts if item.account_id == "two")
        self.assertFalse(evaluation.known)
        self.assertIn("stale", evaluation.unknown_reason)

    def test_collector_error_account_is_unknown_and_never_a_target(self):
        result = self.check("one", account("one", five=90), account("two", error="usageStatus=error"))
        self.assertEqual([], decisions_of(result, SwitchDecision))
        evaluation = next(item for item in result.accounts if item.account_id == "two")
        self.assertFalse(evaluation.known)
        self.assertIn("error", evaluation.unknown_reason)

    def test_harvest_switches_to_low_usage_weekly_window_resetting_in_22_hours(self):
        reset_at = NOW + 22 * 60 * 60
        result = self.check(
            "one",
            account("one", five=20, weekly=70),
            account("two", five=12, weekly=13, weekly_resets_at=reset_at),
        )

        switch = decisions_of(result, SwitchDecision)[0]
        notice = decisions_of(result, NotifyDecision)[0]
        self.assertEqual(("one", "two"), (switch.from_account_id, switch.to_account_id))
        self.assertFalse(switch.bypass_cooldown)
        self.assertTrue(switch.reason.startswith("harvest: weekly resets in 22"))
        self.assertIn("with 87", switch.reason)
        self.assertTrue(switch.reason.endswith("% left"))
        self.assertEqual("harvest:two:%s" % reset_at, notice.key)
        self.assertEqual(NOW, result.next_state.last_harvest_at["claude"])
        self.assertEqual(reset_at, result.next_state.harvested_resets["claude"]["two"])

    def test_harvest_does_not_fire_when_reset_is_beyond_harvest_window(self):
        result = self.check(
            "one",
            account("one", five=20, weekly=70),
            account(
                "two",
                five=12,
                weekly=13,
                weekly_resets_at=NOW + self.config.harvest_window_seconds + 1,
            ),
        )
        self.assertEqual([], decisions_of(result, SwitchDecision))

    def test_harvest_respects_provider_cooldown(self):
        state = PolicyState(
            last_harvest_at={"claude": NOW - self.config.harvest_cooldown_seconds + 1}
        )
        result = self.check(
            "one",
            account("one", five=20, weekly=70),
            account("two", five=12, weekly=13, weekly_resets_at=NOW + 22 * 60 * 60),
            state=state,
        )
        self.assertEqual([], decisions_of(result, SwitchDecision))

    def test_harvest_never_reuses_the_same_account_reset_after_cooldown(self):
        reset_at = NOW + 22 * 60 * 60
        first = self.check(
            "one",
            account("one", five=20, weekly=70),
            account("two", five=12, weekly=13, weekly_resets_at=reset_at),
        )
        later = NOW + self.config.harvest_cooldown_seconds + 1
        again = self.check(
            "one",
            account("one", five=20, weekly=70, observed_at=later),
            account("two", five=12, weekly=13, weekly_resets_at=reset_at, observed_at=later),
            state=first.next_state,
            now=later,
        )
        self.assertEqual([], decisions_of(again, SwitchDecision))

    def test_harvest_requires_every_five_hour_window_to_be_below_50_percent(self):
        result = self.check(
            "one",
            account("one", five=20, weekly=70),
            account("two", five=50, weekly=13, weekly_resets_at=NOW + 22 * 60 * 60),
        )
        self.assertEqual([], decisions_of(result, SwitchDecision))

    def test_active_harvest_target_stays_put_and_records_its_reset(self):
        active_reset = NOW + 10 * 60 * 60
        result = self.check(
            "one",
            account("one", five=12, weekly=13, weekly_resets_at=active_reset),
            account("two", five=12, weekly=13, weekly_resets_at=NOW + 12 * 60 * 60),
        )
        self.assertEqual([], decisions_of(result, SwitchDecision))
        self.assertIsInstance(result.decisions[-1], NoneDecision)
        self.assertEqual("active account is the harvest target", result.decisions[-1].reason)
        self.assertEqual(active_reset, result.next_state.harvested_resets["claude"]["one"])
        self.assertNotIn("claude", result.next_state.last_harvest_at)

    def test_red_switch_target_prefers_soonest_reset_over_lower_score(self):
        result = self.check(
            "one",
            account("one", five=91, weekly=20),
            account("lowest-score", five=10, weekly=10),
            account(
                "soonest-reset",
                five=20,
                weekly=20,
                weekly_resets_at=NOW + 22 * 60 * 60,
            ),
        )
        self.assertEqual("soonest-reset", decisions_of(result, SwitchDecision)[0].to_account_id)

    def test_red_switch_target_prefers_reset_far_beyond_harvest_window(self):
        # Regression (2026-07-16 live miss): a 44h-away weekly reset was
        # ranked as infinity because ranking was capped at the 24h harvest
        # window, so a slightly lower-score far-reset account won instead.
        result = self.check(
            "one",
            account("one", five=95, weekly=20),
            account("lowest-score-far-reset", five=4, weekly=2),
            account(
                "resets-in-44h",
                five=7,
                weekly=2,
                weekly_resets_at=NOW + 44 * 60 * 60,
            ),
        )
        self.assertEqual("resets-in-44h", decisions_of(result, SwitchDecision)[0].to_account_id)

    def test_red_switch_skips_soonest_reset_with_exhausted_five_hour(self):
        # Owner rule (2026-07-16): never switch TO an account whose 5h window
        # is mostly spent, even if its weekly resets soonest — pick the NEXT
        # soonest-reset rideable account instead.
        result = self.check(
            "one",
            account("one", five=96, weekly=20),
            account(
                "soonest-but-5h-dead",
                five=90,
                weekly=20,
                weekly_resets_at=NOW + 19 * 60 * 60,
            ),
            account(
                "next-soonest-rideable",
                five=5,
                weekly=1,
                weekly_resets_at=NOW + 44 * 60 * 60,
            ),
            account("far-reset-fresh", five=2, weekly=1),
        )
        self.assertEqual(
            "next-soonest-rideable", decisions_of(result, SwitchDecision)[0].to_account_id
        )

    def test_unknown_reset_is_not_harvestable_but_remains_a_healthy_threshold_target(self):
        no_harvest = self.check(
            "one",
            account("one", five=20, weekly=70),
            account("two", five=12, weekly=13),
        )
        self.assertEqual([], decisions_of(no_harvest, SwitchDecision))

        red_switch = self.check(
            "one",
            account("one", five=95, weekly=20),
            account("two", five=12, weekly=13),
        )
        self.assertEqual("two", decisions_of(red_switch, SwitchDecision)[0].to_account_id)

    def test_harvest_result_is_deterministic(self):
        inputs = snapshot(
            "one",
            account("one", five=20, weekly=70),
            account("two", five=12, weekly=13, weekly_resets_at=NOW + 22 * 60 * 60),
        )
        self.assertEqual(
            evaluate(inputs, PolicyState(), self.config, NOW),
            evaluate(inputs, PolicyState(), self.config, NOW),
        )

    def test_same_inputs_produce_identical_result(self):
        inputs = snapshot("one", account("one", weekly=96), account("two", five=30))
        state = PolicyState(last_switch_at={"claude": 1})
        self.assertEqual(
            evaluate(inputs, state, self.config, NOW),
            evaluate(inputs, state, self.config, NOW),
        )


HOUR = 3600.0
DAY = 86400.0


class LadderUsabilityTest(unittest.TestCase):
    """When all accounts are red, the ladder must ride an account that can
    actually serve — never one whose 5h session window is fully spent."""

    def setUp(self):
        self.config = PolicyConfig()

    def _acct(self, ident, five, weekly, fable):
        return AccountSnapshot(ident, (
            Window("fiveHour", five, WindowKind.FIVE_HOUR),
            Window("sevenDay", weekly, WindowKind.WEEKLY),
            Window("scoped:Fable", fable, WindowKind.WEEKLY, NOW + 6 * DAY),
        ), NOW)

    def test_ladder_skips_session_dead_account(self):
        # active (4) just exhausted 5h; slot1 is 5h-dead+Fable-free, slot2 is
        # 5h-free+Fable-dead. Must ride slot2 (can serve), not slot1 (useless).
        snap = ProviderSnapshot("claude", "4", (
            self._acct("1", 100, 32, 52),
            self._acct("2", 0, 67, 100),
            self._acct("4", 100, 25, 45),
        ))
        r = evaluate(snap, PolicyState(), self.config, NOW)
        sw = [d for d in r.decisions if isinstance(d, SwitchDecision)]
        self.assertTrue(sw)
        self.assertEqual("2", sw[0].to_account_id)

    def test_ladder_prefers_most_session_headroom(self):
        snap = ProviderSnapshot("claude", "3", (
            self._acct("1", 50, 90, 100),
            self._acct("2", 10, 90, 100),
            self._acct("3", 80, 90, 100),
        ))
        r = evaluate(snap, PolicyState(), self.config, NOW)
        sw = [d for d in r.decisions if isinstance(d, SwitchDecision)]
        self.assertEqual("2", sw[0].to_account_id)  # lowest 5h among usable

    def test_ladder_stays_serviceable_active_over_session_dead_only_alt(self):
        # active red-but-serviceable (5h 10%, weekly 99%); only alt is 5h-dead.
        # Must NOT trade a serviceable active for a useless one.
        snap = ProviderSnapshot("claude", "1", (
            self._acct("1", 10, 99, 100),
            self._acct("2", 100, 30, 100),
        ))
        r = evaluate(snap, PolicyState(), self.config, NOW)
        self.assertEqual([], [d for d in r.decisions if isinstance(d, SwitchDecision)])

    def test_observed_limit_flees_to_red_serviceable_when_no_healthy(self):
        # usage-limit observed; no healthy account, but a red-serviceable one
        # exists — must switch to it, not drop the authoritative signal.
        snap = ProviderSnapshot("claude", "1", (
            self._acct("1", 10, 60, 100),
            self._acct("2", 20, 96, 100),  # red (weekly 96) but serviceable
        ), hard_limit_observed=True)
        r = evaluate(snap, PolicyState(), self.config, NOW)
        sw = [d for d in r.decisions if isinstance(d, SwitchDecision)]
        self.assertEqual("2", sw[0].to_account_id)

    def test_codex_weekly_only_account_stays_usable(self):
        # Codex reports only a weekly window (no 5h). With require_five_hour=False
        # it must remain KNOWN and switchable — never marked unknown.
        cfg = PolicyConfig(require_five_hour=False)
        snap = ProviderSnapshot("codex", "a", (
            AccountSnapshot("a", (Window("secondary", 99, WindowKind.WEEKLY),), NOW),
            AccountSnapshot("b", (Window("secondary", 20, WindowKind.WEEKLY),), NOW),
        ))
        r = evaluate(snap, PolicyState(), cfg, NOW)
        sw = [d for d in r.decisions if isinstance(d, SwitchDecision)]
        self.assertEqual("b", sw[0].to_account_id)  # red weekly → switch to healthy weekly

    def test_codex_5h_requirement_auto_disabled_by_default_config(self):
        # Even with the DEFAULT config (require_five_hour=True), a provider that
        # reports NO 5h window (Codex) must not have its accounts marked unknown.
        snap = ProviderSnapshot("codex", "a", (
            AccountSnapshot("a", (Window("secondary", 99, WindowKind.WEEKLY),), NOW),
            AccountSnapshot("b", (Window("secondary", 20, WindowKind.WEEKLY),), NOW),
        ))
        r = evaluate(snap, PolicyState(), PolicyConfig(), NOW)  # default: require True
        self.assertTrue(all(a.known for a in r.accounts))
        self.assertTrue([d for d in r.decisions if isinstance(d, SwitchDecision)])

    def test_scope_blocked_account_is_a_valid_escape_from_fully_blocked(self):
        # A: universal weekly 100% (serves nothing) = fully_blocked.
        # B: only scoped Fable 100%, 5h+weekly free (serves non-Fable) = NOT fully_blocked.
        # Must escape A -> B (not stay stuck), since B can serve something.
        snap = ProviderSnapshot("claude", "1", (
            AccountSnapshot("1", (Window("fiveHour", 10, WindowKind.FIVE_HOUR),
                                  Window("sevenDay", 100, WindowKind.WEEKLY)), NOW),
            AccountSnapshot("2", (Window("fiveHour", 50, WindowKind.FIVE_HOUR),
                                  Window("sevenDay", 50, WindowKind.WEEKLY),
                                  Window("scoped:Fable", 100, WindowKind.WEEKLY)), NOW),
        ))
        r = evaluate(snap, PolicyState(), self.config, NOW)
        sw = [d for d in r.decisions if isinstance(d, SwitchDecision)]
        self.assertTrue(sw)
        self.assertEqual("2", sw[0].to_account_id)

    def test_weekly_only_account_is_unknown_not_a_target(self):
        # An account with no 5h window (its session state is unknowable) must not
        # be a switch target — active red 5h, alt weekly-only must NOT be chosen.
        snap = ProviderSnapshot("claude", "1", (
            self._acct("1", 100, 20, 20),
            AccountSnapshot("2", (Window("sevenDay", 10, WindowKind.WEEKLY),), NOW),
        ))
        r = evaluate(snap, PolicyState(), self.config, NOW)
        self.assertEqual([], [d for d in r.decisions if isinstance(d, SwitchDecision)])

    def test_no_pingpong_between_two_weekly_maxed_accounts(self):
        # Both accounts: 5h free, weekly 100% (hard_limit, serviceable). The
        # forced path must NOT switch every tick (would ping-pong) — it defers
        # to the paced ladder, which also should not rotate to a no-better peer.
        acc = lambda i, five: AccountSnapshot(i, (
            Window("fiveHour", five, WindowKind.FIVE_HOUR),
            Window("sevenDay", 100, WindowKind.WEEKLY),
        ), NOW)
        snap = ProviderSnapshot("claude", "1", (acc("1", 20), acc("2", 30)))
        r = evaluate(snap, PolicyState(), self.config, NOW)
        sw = [d for d in r.decisions if isinstance(d, SwitchDecision)]
        # active (5h 20) is the lower-5h serviceable one → stays; no pointless move.
        self.assertEqual([], sw)

    def test_absent_active_falls_through_to_ladder(self):
        # active absent from snapshot; only known accounts are red-serviceable.
        # After the streak, must ride the best usable red account (not give up).
        state = PolicyState(unknown_active_ticks={"claude": 2})
        snap = ProviderSnapshot("claude", "missing", (
            self._acct("2", 20, 96, 100),
            self._acct("3", 10, 97, 100),
        ))
        r = evaluate(snap, state, self.config, NOW)
        sw = [d for d in r.decisions if isinstance(d, SwitchDecision)]
        self.assertTrue(sw)
        self.assertIn(sw[0].to_account_id, {"2", "3"})


def credit(cid, expires_in_days=None, reset_type="codex_rate_limits"):
    return ResetCredit(
        cid,
        NOW + expires_in_days * DAY if expires_in_days is not None else None,
        reset_type,
    )


def codex_account(identifier, weekly, five=0.0, credits=(), weekly_resets_in_days=None):
    windows = [Window("primary", five, WindowKind.FIVE_HOUR)]
    if weekly is not None:
        windows.append(
            Window("secondary", weekly, WindowKind.WEEKLY,
                   NOW + weekly_resets_in_days * DAY if weekly_resets_in_days is not None else None)
        )
    return AccountSnapshot(identifier, tuple(windows), NOW, None, tuple(credits))


def codex_snapshot(*accounts):
    return ProviderSnapshot("codex", accounts[0].account_id if accounts else "", tuple(accounts))


class CreditPolicyTest(unittest.TestCase):
    def setUp(self):
        self.on = PolicyConfig(redeem_enabled=True, weekly_threshold=95.0)

    def redeems(self, result):
        return [d for d in result[0] if isinstance(d, RedeemDecision)]

    def test_starvation_redeems_furthest_reset_rideable_soonest_credit(self):
        # a: resets in 1 day (self-heals soon) ; b: resets in 5 days (furthest)
        snap = codex_snapshot(
            codex_account("a", weekly=99, five=10, weekly_resets_in_days=1, credits=[credit("ca", 20)]),
            codex_account("b", weekly=99, five=10, weekly_resets_in_days=5,
                          credits=[credit("cb-late", 20), credit("cb-soon", 3)]),
        )
        decisions, _ = evaluate_credits(snap, self.on, PolicyState(), NOW)
        red = [d for d in decisions if isinstance(d, RedeemDecision)]
        self.assertEqual(1, len(red))
        self.assertEqual("b", red[0].account_id)          # furthest natural reset
        self.assertEqual("cb-soon", red[0].credit_id)      # soonest-expiring on b

    def test_maxed_5h_account_still_eligible(self):
        # The Weekly+5h credit resets 5h too, so a maxed 5h is NOT a reason to
        # exclude — a (furthest reset, 5h maxed) should be chosen over b.
        snap = codex_snapshot(
            codex_account("a", weekly=99, five=100, weekly_resets_in_days=5, credits=[credit("ca", 5)]),
            codex_account("b", weekly=99, five=10, weekly_resets_in_days=2, credits=[credit("cb", 5)]),
        )
        decisions, _ = evaluate_credits(snap, PolicyConfig(redeem_enabled=True), PolicyState(), NOW)
        red = [d for d in decisions if isinstance(d, RedeemDecision)]
        self.assertEqual(1, len(red))
        self.assertIn(red[0].account_id, {"a", "b"})  # both eligible; a not excluded for 5h

    def test_ambiguous_unknown_account_blocks_starvation(self):
        # b has no weekly window and no error → might be healthy → do not redeem.
        snap = codex_snapshot(
            codex_account("a", weekly=99, five=10, weekly_resets_in_days=5, credits=[credit("ca", 5)]),
            AccountSnapshot("b", tuple(), NOW, None),
        )
        decisions, _ = evaluate_credits(snap, PolicyConfig(redeem_enabled=True), PolicyState(), NOW)
        self.assertEqual([], [d for d in decisions if isinstance(d, RedeemDecision)])

    def test_errored_account_blocks_starvation(self):
        # A transiently-errored account might be healthy — must block redeem.
        snap = codex_snapshot(
            codex_account("a", weekly=99, five=10, weekly_resets_in_days=5, credits=[credit("ca", 5)]),
            AccountSnapshot("b", tuple(), None, "timeout"),
        )
        decisions, _ = evaluate_credits(snap, PolicyConfig(redeem_enabled=True), PolicyState(), NOW)
        self.assertEqual([], [d for d in decisions if isinstance(d, RedeemDecision)])

    def test_doomed_ordering_independent_of_notify_horizon(self):
        # a: safe credit (expires day 2, natural reset day 1) — NOT doomed.
        # b: doomed credit (expires day 3, natural reset day 5) — WILL be lost.
        # Both expiries are >24h away; doomed-first must still pick b.
        snap = codex_snapshot(
            codex_account("a", weekly=99, five=10, weekly_resets_in_days=1, credits=[credit("ca-safe", 2)]),
            codex_account("b", weekly=99, five=10, weekly_resets_in_days=5, credits=[credit("cb-doomed", 3)]),
        )
        decisions, _ = evaluate_credits(snap, PolicyConfig(redeem_enabled=True), PolicyState(), NOW)
        red = [d for d in decisions if isinstance(d, RedeemDecision)]
        self.assertEqual("b", red[0].account_id)
        self.assertEqual("cb-doomed", red[0].credit_id)

    def test_credit_in_backoff_is_skipped(self):
        snap = codex_snapshot(
            codex_account("a", weekly=99, five=10, weekly_resets_in_days=5, credits=[credit("ca", 3)]),
            codex_account("b", weekly=99, five=10, weekly_resets_in_days=2),
        )
        state = PolicyState(redeem_backoff={"ca": NOW + 100})
        decisions, _ = evaluate_credits(snap, PolicyConfig(redeem_enabled=True), state, NOW)
        self.assertEqual([], [d for d in decisions if isinstance(d, RedeemDecision)])

    def test_unknown_reset_type_not_redeemed(self):
        snap = codex_snapshot(
            codex_account("a", weekly=99, five=10, weekly_resets_in_days=5,
                          credits=[credit("ca", 5, reset_type="some_future_type")]),
            codex_account("b", weekly=99, five=10, weekly_resets_in_days=2),
        )
        decisions, _ = evaluate_credits(snap, PolicyConfig(redeem_enabled=True), PolicyState(), NOW)
        self.assertEqual([], [d for d in decisions if isinstance(d, RedeemDecision)])

    def test_no_redeem_when_a_healthy_account_exists(self):
        snap = codex_snapshot(
            codex_account("a", weekly=99, credits=[credit("ca", 5)]),
            codex_account("b", weekly=40, credits=[credit("cb", 5)]),  # healthy
        )
        decisions, _ = evaluate_credits(snap, self.on, PolicyState(), NOW)
        self.assertEqual([], [d for d in decisions if isinstance(d, RedeemDecision)])

    def test_expiry_harvest_notifies_early_executes_near_expiry(self):
        # Credit expires before natural reset, weekly 60% used. Far from expiry
        # → notify only (no redeem). Within safety lead → redeem.
        far = codex_snapshot(
            codex_account("a", weekly=60, five=10, weekly_resets_in_days=3, credits=[credit("ca", 0.5)]),
        )
        decisions, _ = evaluate_credits(far, self.on, PolicyState(), NOW)  # 0.5d away > 1h lead
        self.assertEqual([], [d for d in decisions if isinstance(d, RedeemDecision)])
        self.assertTrue(any(isinstance(d, NotifyDecision) for d in decisions))

        near = codex_snapshot(
            codex_account("a", weekly=60, five=10, weekly_resets_in_days=3, credits=[credit("ca", 0.02)]),
        )
        decisions, _ = evaluate_credits(near, self.on, PolicyState(), NOW)  # ~29 min < 1h lead
        red = [d for d in decisions if isinstance(d, RedeemDecision)]
        self.assertEqual(1, len(red))
        self.assertEqual("a", red[0].account_id)

    def test_harvest_skipped_when_credit_expires_after_natural_reset(self):
        # Natural reset in 1 day gives the quota free; credit expiring in 2 days
        # is not doomed relative to it → do not spend it.
        snap = codex_snapshot(
            codex_account("a", weekly=60, five=10, weekly_resets_in_days=1, credits=[credit("ca", 2)]),
        )
        decisions, _ = evaluate_credits(snap, self.on, PolicyState(), NOW)
        self.assertEqual([], [d for d in decisions if isinstance(d, RedeemDecision)])

    def test_notify_only_when_disabled_no_state_change(self):
        snap = codex_snapshot(
            codex_account("a", weekly=99, five=10, weekly_resets_in_days=5, credits=[credit("ca", 3)]),
            codex_account("b", weekly=99, five=10, weekly_resets_in_days=2, credits=[credit("cb", 3)]),
        )
        decisions, new_state = evaluate_credits(snap, PolicyConfig(redeem_enabled=False), PolicyState(), NOW)
        self.assertEqual([], [d for d in decisions if isinstance(d, RedeemDecision)])
        self.assertTrue(any(isinstance(d, NotifyDecision) for d in decisions))
        self.assertEqual({}, dict(new_state.last_redeem_at))
        self.assertEqual({}, dict(new_state.redeemed_credits))

    def test_reserve_blocks_when_starvation_may_not_breach(self):
        snap = codex_snapshot(
            codex_account("a", weekly=99, five=10, weekly_resets_in_days=5, credits=[credit("ca", 3)]),
            codex_account("b", weekly=99, five=10, weekly_resets_in_days=2),
        )
        cfg = PolicyConfig(redeem_enabled=True, redeem_reserve=1, redeem_starvation_breach_reserve=False)
        decisions, _ = evaluate_credits(snap, cfg, PolicyState(), NOW)
        self.assertEqual([], [d for d in decisions if isinstance(d, RedeemDecision)])

    def test_starvation_breaches_reserve_by_default(self):
        snap = codex_snapshot(
            codex_account("a", weekly=99, five=10, weekly_resets_in_days=5, credits=[credit("ca", 3)]),
            codex_account("b", weekly=99, five=10, weekly_resets_in_days=2),
        )
        cfg = PolicyConfig(redeem_enabled=True, redeem_reserve=5)  # breach default True
        decisions, _ = evaluate_credits(snap, cfg, PolicyState(), NOW)
        self.assertEqual(1, len([d for d in decisions if isinstance(d, RedeemDecision)]))

    def test_cooldown_blocks_recent_redeem(self):
        snap = codex_snapshot(
            codex_account("a", weekly=99, five=10, weekly_resets_in_days=5, credits=[credit("ca", 3)]),
            codex_account("b", weekly=99, five=10, weekly_resets_in_days=2, credits=[credit("cb", 3)]),
        )
        state = PolicyState(last_redeem_at={"a": NOW - 100, "b": NOW - 100})
        decisions, _ = evaluate_credits(snap, PolicyConfig(redeem_enabled=True, redeem_cooldown_seconds=3600), state, NOW)
        self.assertEqual([], [d for d in decisions if isinstance(d, RedeemDecision)])

    def test_already_attempted_credit_not_reemitted(self):
        snap = codex_snapshot(
            codex_account("a", weekly=99, five=10, weekly_resets_in_days=5, credits=[credit("ca", 3)]),
            codex_account("b", weekly=99, five=10, weekly_resets_in_days=2),
        )
        state = PolicyState(redeemed_credits={"ca": NOW - 10})
        decisions, _ = evaluate_credits(snap, PolicyConfig(redeem_enabled=True), state, NOW)
        self.assertEqual([], [d for d in decisions if isinstance(d, RedeemDecision)])

    def test_deterministic(self):
        snap = codex_snapshot(
            codex_account("a", weekly=99, five=10, weekly_resets_in_days=5, credits=[credit("ca", 3)]),
            codex_account("b", weekly=99, five=10, weekly_resets_in_days=2, credits=[credit("cb", 3)]),
        )
        self.assertEqual(
            evaluate_credits(snap, self.on, PolicyState(), NOW),
            evaluate_credits(snap, self.on, PolicyState(), NOW),
        )


if __name__ == "__main__":
    unittest.main()
