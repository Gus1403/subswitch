#!/usr/bin/env python3
"""Dry-run unit coverage for subswitchd's collection and action helpers.

The daemon snapshots CONFIG_ROOT during import, so every case imports it only
after pointing SUBSWITCH_CONFIG_HOME at a disposable directory.  subprocess is
mocked at its boundary in every test: these tests never invoke host binaries.
"""

import base64
import importlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import ANY, patch


REPO_ROOT = Path(__file__).resolve().parents[1]
DAEMON_DIR = REPO_ROOT / "daemon"
if str(DAEMON_DIR) not in sys.path:
    sys.path.insert(0, str(DAEMON_DIR))


def completed(arguments, stdout="", stderr="", returncode=0):
    """Create the subset of CompletedProcess consumed by run_command."""
    return subprocess.CompletedProcess(arguments, returncode, stdout, stderr)


class SubswitchdTestCase(unittest.TestCase):
    """Reload the daemon beneath a per-test config root."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.config_root = Path(self.tempdir.name) / "config"
        self.env_patch = patch.dict(
            os.environ, {"SUBSWITCH_CONFIG_HOME": str(self.config_root)}
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        sys.modules.pop("subswitchd", None)
        self.daemon = importlib.import_module("subswitchd")
        self.log = self.daemon.Log(self.config_root / "test.log")

    def codex_config(self, homes):
        return {"homes": [str(home) for home in homes]}


class ClaudeCollectorTests(SubswitchdTestCase):
    def test_claude_maps_windows_scopes_age_and_non_ok_status(self):
        payload = {
            "activeAccountNumber": 2,
            "accounts": [
                {
                    "number": 2,
                    "usageStatus": "ok",
                    "usageAgeSeconds": 17,
                    "usage": {
                        "fiveHour": {
                            "pct": 81,
                            "resetsAt": "2026-07-22T12:59:59.738777+00:00",
                        },
                        "sevenDay": {"pct": 94, "resetsAt": "2026-07-23T12:00:00Z"},
                        "scoped": [
                            {
                                "name": "Fable",
                                "pct": 96,
                                "resetsAt": "not-an-iso-timestamp",
                            }
                        ],
                    },
                },
                {
                    "number": 3,
                    "usageStatus": "error",
                    "usageAgeSeconds": 1,
                    "usage": {"fiveHour": {"pct": 1}},
                },
            ],
        }
        with patch.object(
            self.daemon.subprocess,
            "run",
            return_value=completed(["cswap"], json.dumps(payload)),
        ) as run:
            snapshot = self.daemon.collect_claude({}, 1000.0, self.log)

        self.assertEqual(["cswap", "list", "--json"], run.call_args.args[0])
        self.assertEqual("2", snapshot.active_account_id)
        good, bad = snapshot.accounts
        self.assertEqual(983.0, good.observed_at)
        self.assertEqual(
            [("fiveHour", 81.0, "five_hour"), ("sevenDay", 94.0, "weekly"),
             ("scoped:Fable", 96.0, "weekly")],
            [(window.name, window.used_percent, window.kind.value) for window in good.windows],
        )
        self.assertEqual(
            [
                datetime(2026, 7, 22, 12, 59, 59, 738777, tzinfo=timezone.utc).timestamp(),
                datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc).timestamp(),
                None,
            ],
            [window.resets_at for window in good.windows],
        )
        self.assertEqual("usageStatus=error", bad.error)
        self.assertEqual((), bad.windows)


class InvisibleClaudeAccountTests(SubswitchdTestCase):
    def _snap(self, error, observed_at=None):
        windows = () if error else (
            self.daemon.Window("fiveHour", 10.0, self.daemon.WindowKind.FIVE_HOUR),)
        acct = self.daemon.AccountSnapshot("1", windows, observed_at, error)
        return self.daemon.ProviderSnapshot("claude", "2", (acct,))

    def test_notifies_after_threshold_and_clears_on_recovery(self):
        state = self.daemon.default_state()
        state["claudeIdentities"]["1"] = {"org": "o", "email": "a@x.y"}
        cfg = {"invisibleThresholdSeconds": 300}
        with patch.object(self.daemon, "notify") as n:
            # first sighting: record, no alert
            self.daemon.scan_invisible_claude_accounts(cfg, state, self._snap("usageStatus=unavailable"), 1000.0, self.log)
            n.assert_not_called()
            # still unavailable but under threshold
            self.daemon.scan_invisible_claude_accounts(cfg, state, self._snap("usageStatus=unavailable"), 1200.0, self.log)
            n.assert_not_called()
            # past threshold → alert once
            self.daemon.scan_invisible_claude_accounts(cfg, state, self._snap("usageStatus=unavailable"), 1400.0, self.log)
            self.assertEqual(1, n.call_count)
            self.assertIn("a@x.y", n.call_args.args[0])
            # dedupe: still unavailable, no second alert
            self.daemon.scan_invisible_claude_accounts(cfg, state, self._snap("usageStatus=unavailable"), 1700.0, self.log)
            self.assertEqual(1, n.call_count)
        # recovery clears the tracker
        self.daemon.scan_invisible_claude_accounts(cfg, state, self._snap(None, observed_at=1800.0), 1800.0, self.log)
        self.assertNotIn("1", state["claudeInvisible"])

    def test_healthy_account_never_alerts(self):
        state = self.daemon.default_state()
        with patch.object(self.daemon, "notify") as n:
            self.daemon.scan_invisible_claude_accounts({}, state, self._snap(None), 5000.0, self.log)
        n.assert_not_called()


class ClaudeDirectFallbackTests(SubswitchdTestCase):
    LIMITS = {
        "limits": [
            {"kind": "session", "percent": 100, "resets_at": "2026-07-22T19:00:00Z", "scope": None},
            {"kind": "weekly_all", "percent": 16, "resets_at": "2026-07-29T13:00:00Z", "scope": None},
            {"kind": "weekly_scoped", "percent": 26, "resets_at": "2026-07-29T13:00:00Z",
             "scope": {"model": {"display_name": "Fable"}}},
        ]
    }

    def _list_payload(self):
        # slot 1 unavailable, slot 2 ok
        return json.dumps({"activeAccountNumber": 2, "accounts": [
            {"number": 1, "email": "a@x.y", "usageStatus": "unavailable", "usage": None},
            {"number": 2, "email": "b@x.y", "usageStatus": "ok", "usageAgeSeconds": 5,
             "usage": {"fiveHour": {"pct": 10}, "sevenDay": {"pct": 20}}},
        ]})

    def test_fallback_recovers_unavailable_account(self):
        class Resp:
            def __init__(s, b): s._b = b
            def read(s): return s._b
            def __enter__(s): return s
            def __exit__(s, *a): return False
        def fake_run(args, **kw):
            if args[:3] == ["cswap", "list", "--json"]:
                return 0, self._list_payload(), ""
            if args[0] == "/usr/bin/security":
                return 0, json.dumps({"claudeAiOauth": {
                    "accessToken": "tok", "expiresAt": 9999999999999}}), ""
            return 0, "", ""
        with patch.object(self.daemon, "run_command", side_effect=fake_run), \
             patch.object(self.daemon.urllib.request, "urlopen",
                          return_value=Resp(json.dumps(self.LIMITS).encode())):
            snap = self.daemon.collect_claude({}, 1000.0, self.log)
        one = [a for a in snap.accounts if a.account_id == "1"][0]
        self.assertIsNone(one.error)  # recovered
        names = {(w.name, w.used_percent) for w in one.windows}
        self.assertIn(("scoped:Fable", 26.0), names)
        self.assertIn(("fiveHour", 100.0), names)

    def test_stale_ok_account_is_refreshed_via_fallback(self):
        class Resp:
            def __init__(s, b): s._b = b
            def read(s): return s._b
            def __enter__(s): return s
            def __exit__(s, *a): return False
        # cswap reports OK but with 33-min-old data → policy would drop it as
        # stale/unknown; the fallback must refresh it fresh.
        payload = json.dumps({"activeAccountNumber": 2, "accounts": [
            {"number": 4, "email": "jp@x.y", "usageStatus": "ok", "usageAgeSeconds": 2000,
             "usage": {"fiveHour": {"pct": 63}, "sevenDay": {"pct": 25},
                       "scoped": [{"name": "Fable", "pct": 44}]}},
        ]})
        def fake_run(args, **kw):
            if args[:3] == ["cswap", "list", "--json"]:
                return 0, payload, ""
            if args[0] == "/usr/bin/security":
                return 0, json.dumps({"claudeAiOauth": {
                    "accessToken": "tok", "expiresAt": 9999999999999}}), ""
            return 0, "", ""
        with patch.object(self.daemon, "run_command", side_effect=fake_run), \
             patch.object(self.daemon.urllib.request, "urlopen",
                          return_value=Resp(json.dumps(self.LIMITS).encode())):
            snap = self.daemon.collect_claude({}, 5000.0, self.log)
        four = snap.accounts[0]
        self.assertIsNone(four.error)
        self.assertEqual(5000.0, four.observed_at)  # refreshed to now, not stale

    def test_fresh_ok_account_is_not_refetched(self):
        payload = json.dumps({"activeAccountNumber": 2, "accounts": [
            {"number": 2, "email": "b@x.y", "usageStatus": "ok", "usageAgeSeconds": 30,
             "usage": {"fiveHour": {"pct": 10}, "sevenDay": {"pct": 20}}},
        ]})
        def fake_run(args, **kw):
            if args[:3] == ["cswap", "list", "--json"]:
                return 0, payload, ""
            return 0, "", ""
        with patch.object(self.daemon, "run_command", side_effect=fake_run), \
             patch.object(self.daemon.urllib.request, "urlopen") as urlopen:
            self.daemon.collect_claude({}, 5000.0, self.log)
        urlopen.assert_not_called()  # fresh → no direct fetch

    def test_fallback_disabled_leaves_account_unavailable(self):
        def fake_run(args, **kw):
            if args[:3] == ["cswap", "list", "--json"]:
                return 0, self._list_payload(), ""
            return 0, "", ""
        with patch.object(self.daemon, "run_command", side_effect=fake_run), \
             patch.object(self.daemon.urllib.request, "urlopen") as urlopen:
            snap = self.daemon.collect_claude({"directFallback": False}, 1000.0, self.log)
        urlopen.assert_not_called()
        one = [a for a in snap.accounts if a.account_id == "1"][0]
        self.assertEqual("usageStatus=unavailable", one.error)

    def test_expired_backup_token_skips_fetch(self):
        self.assertIsNone(self.daemon._claude_backup_access_token("1", "", 1000.0))  # no email
        with patch.object(self.daemon, "run_command",
                          return_value=(0, json.dumps({"claudeAiOauth": {
                              "accessToken": "t", "expiresAt": 500 * 1000}}), "")):
            # expiresAt 500s epoch << now → expired → None
            self.assertIsNone(self.daemon._claude_backup_access_token("1", "a@x.y", 1000.0))


class CodexCollectorTests(SubswitchdTestCase):
    def _home(self, name):
        home = Path(self.tempdir.name) / name
        home.mkdir()
        (home / "auth.json").write_text("{}", encoding="utf-8")
        return home

    def test_codex_maps_primary_secondary_and_clears_backoff_after_success(self):
        home = self._home("codex-a")
        state = self.daemon.default_state()
        state["codexBackoff"][str(home.resolve())] = {
            "failures": 2,
            "nextAttemptAt": 0,
            "lastError": "old",
        }
        payload = [
            {
                "usage": {
                    "primary": {"usedPercent": 88, "resetsAt": "2026-07-19T16:52:01Z"},
                    "secondary": {
                        "usedPercent": 91,
                        "resetsAt": "2026-07-22T16:52:01+00:00",
                    },
                }
            }
        ]
        with patch.object(
            self.daemon.subprocess,
            "run",
            return_value=completed(["codexbar"], json.dumps(payload)),
        ) as run:
            snapshot = self.daemon.collect_codex(self.codex_config([home]), state, 50.0, self.log)

        account = snapshot.accounts[0]
        self.assertEqual(str(home.resolve()), snapshot.active_account_id)
        self.assertEqual(50.0, account.observed_at)
        self.assertEqual(
            [("primary", 88.0, "five_hour"), ("secondary", 91.0, "weekly")],
            [(window.name, window.used_percent, window.kind.value) for window in account.windows],
        )
        self.assertEqual(
            [
                datetime(2026, 7, 19, 16, 52, 1, tzinfo=timezone.utc).timestamp(),
                datetime(2026, 7, 22, 16, 52, 1, tzinfo=timezone.utc).timestamp(),
            ],
            [window.resets_at for window in account.windows],
        )
        self.assertNotIn(str(home.resolve()), state["codexBackoff"])
        self.assertEqual(str(home.resolve()), run.call_args.kwargs["env"]["CODEX_HOME"])

    def test_codex_error_payload_is_unknown_and_backoff_doubles_to_cap(self):
        home = self._home("codex-a")
        state = self.daemon.default_state()
        home_id = str(home.resolve())

        for failures, now, expected_delay in (
            (1, 100.0, 60),
            (2, 200.0, 120),
            (3, 300.0, 240),
            (4, 400.0, 480),
            (5, 500.0, 900),
            (6, 600.0, 900),
        ):
            # Make an existing retry due on each loop so collection happens.
            if failures > 1:
                state["codexBackoff"][home_id]["nextAttemptAt"] = 0
            response = [{"error": "unauthenticated"}]
            with patch.object(
                self.daemon.subprocess,
                "run",
                return_value=completed(["codexbar"], json.dumps(response)),
            ):
                snapshot = self.daemon.collect_codex(self.codex_config([home]), state, now, self.log)
            self.assertEqual("unauthenticated", snapshot.accounts[0].error)
            self.assertEqual((), snapshot.accounts[0].windows)
            backoff = state["codexBackoff"][home_id]
            self.assertEqual(failures, backoff["failures"])
            self.assertEqual(now + expected_delay, backoff["nextAttemptAt"])


class PaneWatcherTests(SubswitchdTestCase):
    def test_fingerprint_is_stable_when_history_grows(self):
        text = self.daemon.USAGE_LIMIT_TEXT
        first = "\n".join(["old"] * 34 + [text])
        # The same absolute history line has moved five rows earlier in the
        # most-recent 40-line capture after history has grown by five.
        second = "\n".join(["old"] * 29 + [text])
        self.assertEqual(
            self.daemon.pane_message_offset(first, 100),
            self.daemon.pane_message_offset(second, 105),
        )

    def test_scan_dedupes_seen_message_and_hits_a_new_line(self):
        text = self.daemon.USAGE_LIMIT_TEXT
        state = self.daemon.default_state()
        captures = [
            "\n".join(["old"] * 34 + [text]),
            "\n".join(["old"] * 29 + [text]),
            "\n".join(["old"] * 28 + [text, "new", text]),
        ]
        histories = iter(["%1\tcodex\t900\t100\n", "%1\tcodex\t900\t105\n", "%1\tcodex\t900\t107\n"])

        def fake_run(arguments, **kwargs):
            del kwargs
            if arguments[1] == "list-panes":
                return completed(arguments, next(histories))
            self.assertEqual("capture-pane", arguments[1])
            return completed(arguments, captures.pop(0))

        with patch.object(self.daemon.subprocess, "run", side_effect=fake_run), patch.object(
            self.daemon, "pane_codex_session", return_value="11111111-2222-3333-4444-555555555555"
        ), patch.object(self.daemon, "_pane_codex_home", return_value="/Users/example/.codex"):
            one = self.daemon.scan_codex_panes(state, 1.0, self.log)
            two = self.daemon.scan_codex_panes(state, 2.0, self.log)
            three = self.daemon.scan_codex_panes(state, 3.0, self.log)

        self.assertEqual(1, len(one))
        self.assertEqual([], two)
        self.assertEqual(1, len(three))
        self.assertNotEqual(one[0]["fingerprint"], three[0]["fingerprint"])

    def test_resume_panes_dry_run_does_not_mark_or_call_subprocess(self):
        state = self.daemon.default_state()
        hit = {"pane": "%7", "fingerprint": "fingerprint"}
        config = {"watcher": {"autoResume": True, "debounceSeconds": 600}}
        with patch.object(self.daemon.subprocess, "run") as run:
            self.daemon.resume_panes([hit], "/tmp/codex-two", config, state, 100.0, False, False, self.log)
        run.assert_not_called()
        self.assertNotIn("lastResumeAt", state["panes"]["%7"])

    def test_resume_refused_without_pinned_session(self):
        state = self.daemon.default_state()
        hit = {"pane": "%7", "fingerprint": "fingerprint", "sessionId": ""}
        config = {"watcher": {"autoResume": True, "debounceSeconds": 600}}
        with patch.object(self.daemon.subprocess, "run") as run, patch.object(
            self.daemon, "notify"
        ) as notified:
            self.daemon.resume_panes([hit], "/tmp/codex-two", config, state, 1000.0, True, True, self.log)
        run.assert_not_called()
        notified.assert_called_once()
        self.assertNotIn("lastResumeAt", state["panes"].get("%7", {}))

    def test_match_rollout_by_birth_unique_window_only(self):
        match = self.daemon.match_rollout_by_birth
        self.assertEqual("a", match([("a", 1005.0), ("b", 5000.0)], 1000.0))
        self.assertIsNone(match([("a", 1005.0), ("b", 1010.0)], 1000.0))
        self.assertIsNone(match([("a", 5000.0)], 1000.0))
        self.assertEqual("a", match([("a", 980.0)], 1000.0))
        self.assertIsNone(match([("a", 960.0)], 1000.0))

    def test_resume_panes_respawn_sequence_in_order_and_debounces(self):
        state = self.daemon.default_state()
        session = "11111111-2222-3333-4444-555555555555"
        hit = {"pane": "%7", "fingerprint": "fingerprint", "sessionId": session}
        config = {"watcher": {"autoResume": True, "debounceSeconds": 600}}
        shim = str(self.daemon.Path.home() / ".local" / "bin" / "codex")
        expected = [
            ["tmux", "set-option", "-p", "-t", "%7", "remain-on-exit", "on"],
            ["tmux", "send-keys", "-t", "%7", "C-c"],
            ["tmux", "send-keys", "-t", "%7", "C-c"],
            ["tmux", "respawn-pane", "-k", "-t", "%7", "%s resume %s" % (shim, session)],
            ["tmux", "set-option", "-p", "-t", "%7", "remain-on-exit", "off"],
        ]
        with patch.object(
            self.daemon.subprocess, "run", side_effect=lambda args, **kwargs: completed(args)
        ) as run, patch.object(self.daemon.time, "sleep") as sleep:
            self.daemon.resume_panes([hit], "/tmp/codex-two", config, state, 1000.0, True, True, self.log)
            self.daemon.resume_panes([hit], "/tmp/codex-two", config, state, 1100.0, True, True, self.log)

        self.assertEqual(expected, [call.args[0] for call in run.call_args_list])
        self.assertEqual(
            [((0.5,), {}), ((1.0,), {}), ((2.0,), {}), ((1.0,), {})],
            [(call.args, call.kwargs) for call in sleep.call_args_list],
        )
        self.assertEqual(1000.0, state["panes"]["%7"]["lastResumeAt"])

    def test_scan_cmux_dedupes_usage_message_and_pins_from_process_pid(self):
        state = self.daemon.default_state()
        state["panes"]["%7"] = {"messageFingerprint": "tmux-state"}
        surface_id = "55555555-5555-5555-5555-555555555555"
        binary = self.daemon.cmux_binary()
        payload = {
            "windows": [
                {
                    "workspaces": [
                        {
                            "panes": [
                                {
                                    "surfaces": [
                                        {
                                            "id": surface_id,
                                            "processes": [
                                                {
                                                    "pid": 800,
                                                    "name": "zsh",
                                                    "children": [
                                                        {
                                                            "pid": 812,
                                                            "name": "codex-aarch64-apple-darwin",
                                                            "children": [],
                                                        }
                                                    ],
                                                }
                                            ],
                                        },
                                        {
                                            "id": "not-codex",
                                            "processes": [{"pid": 900, "name": "python"}],
                                        },
                                    ]
                                }
                            ]
                        }
                    ]
                }
            ]
        }

        def fake_run(arguments, **kwargs):
            del kwargs
            if "top" in arguments:
                return 0, json.dumps(payload), ""
            self.assertEqual(
                [
                    binary,
                    "read-screen",
                    "--surface",
                    surface_id,
                    "--scrollback",
                    "--lines",
                    "200",
                ],
                arguments,
            )
            return 0, "before\n%s\nafter\n" % self.daemon.USAGE_LIMIT_TEXT, ""

        with patch.object(self.daemon, "run_command", side_effect=fake_run), patch.object(
            self.daemon, "pane_codex_session", return_value="11111111-2222-3333-4444-555555555555"
        ) as pinned, patch.object(self.daemon, "_pane_codex_home", return_value="/Users/example/.codex-2"):
            one = self.daemon.scan_cmux_codex_surfaces(state, 1000.0, self.log)
            two = self.daemon.scan_cmux_codex_surfaces(state, 1001.0, self.log)

        self.assertEqual(1, len(one))
        self.assertEqual("cmux", one[0]["transport"])
        self.assertEqual(surface_id, one[0]["surface"])
        self.assertEqual([], two)
        self.assertIn("%7", state["panes"])
        self.assertEqual([(("812", 1000.0), {})], [(call.args, call.kwargs) for call in pinned.call_args_list])

    def test_scan_cmux_detects_without_pid_but_refuses_to_invent_session(self):
        state = self.daemon.default_state()
        payload = {
            "windows": [{"workspaces": [{"panes": [{"surfaces": [{
                "ref": "surface:4",
                "processes": [{"name": "codex", "children": []}],
            }]}]}]}]
        }

        def fake_run(arguments, **kwargs):
            del kwargs
            if "top" in arguments:
                return 0, json.dumps(payload), ""
            return 0, self.daemon.USAGE_LIMIT_TEXT, ""

        with patch.object(self.daemon, "run_command", side_effect=fake_run), patch.object(
            self.daemon, "pane_codex_session"
        ) as pinned:
            hits = self.daemon.scan_cmux_codex_surfaces(state, 2000.0, self.log)

        self.assertEqual("", hits[0]["sessionId"])
        pinned.assert_not_called()
        self.assertIn("could not pin", self.log.path.read_text(encoding="utf-8"))

    def test_resume_cmux_uses_interrupts_and_pinned_run_command(self):
        state = self.daemon.default_state()
        session = "11111111-2222-3333-4444-555555555555"
        surface = "55555555-5555-5555-5555-555555555555"
        binary = self.daemon.cmux_binary()
        hit = {
            "pane": "cmux:%s" % surface,
            "surface": surface,
            "transport": "cmux",
            "fingerprint": "fingerprint",
            "sessionId": session,
        }
        config = {"watcher": {"autoResume": True, "debounceSeconds": 600}}
        shim = str(self.daemon.Path.home() / ".local" / "bin" / "codex")
        expected = [
            [binary, "send-key", "--surface", surface, "ctrl+c"],
            [binary, "send-key", "--surface", surface, "ctrl+c"],
            [
                binary,
                "respawn-pane",
                "--surface",
                surface,
                "--command",
                "%s resume %s" % (shim, session),
            ],
        ]
        with patch.object(
            self.daemon, "run_command", side_effect=lambda args, **kwargs: (0, "", "")
        ) as run, patch.object(self.daemon.time, "sleep") as sleep:
            self.daemon.resume_panes(
                [hit], "/tmp/codex-two", config, state, 3000.0, True, True, self.log
            )

        self.assertEqual(expected, [call.args[0] for call in run.call_args_list])
        self.assertEqual([1.0, 2.0, 1.0], [call.args[0] for call in sleep.call_args_list])
        self.assertEqual(3000.0, state["panes"]["cmux:%s" % surface]["lastResumeAt"])


class WalledProcessWatcherTests(SubswitchdTestCase):
    def _result(self, home, hard_limit=True):
        evaluation = self.daemon.AccountEvaluation(
            home,
            True,
            1.0 if hard_limit else 0.5,
            100.0 if hard_limit else 50.0,
            hard_limit,
        )
        return self.daemon.PolicyResult(tuple(), self.daemon.PolicyState(), (evaluation,))

    def test_scan_notifies_once_for_old_walled_tui_and_excludes_exec(self):
        home = str((Path(self.tempdir.name) / "codex-one").resolve())
        state = self.daemon.default_state()
        snapshot = self.daemon.ProviderSnapshot("codex", home, tuple())

        def fake_run(arguments, **kwargs):
            del kwargs
            if arguments[:2] == ["pgrep", "-f"]:
                return 0, "101\n202\n", ""
            if arguments[:4] == ["ps", "eww", "-o", "command="]:
                pid = arguments[-1]
                suffix = "exec --help" if pid == "202" else "resume --last"
                return 0, "/opt/homebrew/bin/codex %s CODEX_HOME=%s\n" % (suffix, home), ""
            self.assertEqual(["ps", "-o", "etime=", "-p", "101"], arguments)
            return 0, "05:01\n", ""

        with patch.object(self.daemon, "run_command", side_effect=fake_run), patch.object(
            self.daemon, "notify"
        ) as notified:
            self.daemon.scan_walled_codex_processes(
                {}, state, snapshot, self._result(home), 1000.0, self.log
            )
            self.daemon.scan_walled_codex_processes(
                {}, state, snapshot, self._result(home), 1060.0, self.log
            )

        notified.assert_called_once_with(
            "codex session pid 101 (home %s) is on a walled account — "
            "redeem a reset credit or restart it on a fresh account" % home,
            "walled-process:101",
            state,
            1000.0,
            self.log,
        )
        self.assertEqual({"101": 1000.0}, state["walledPids"])
        self.assertEqual(
            1,
            self.log.path.read_text(encoding="utf-8").count("WALLED CODEX PROCESS"),
        )

    def test_scan_uses_default_home_observed_limit_and_prunes_dead_pids(self):
        fake_home = Path(self.tempdir.name) / "home"
        codex_home = str((fake_home / ".codex").resolve())
        state = self.daemon.default_state()
        state["walledPids"] = {"999": 123.0}
        snapshot = self.daemon.ProviderSnapshot(
            "codex", codex_home, tuple(), hard_limit_observed=True
        )

        def fake_run(arguments, **kwargs):
            del kwargs
            if arguments[:2] == ["pgrep", "-f"]:
                return 0, "303\n", ""
            if arguments[:4] == ["ps", "eww", "-o", "command="]:
                return 0, "/opt/homebrew/bin/codex resume --last\n", ""
            return 0, "1-00:00:00\n", ""

        with patch.dict(os.environ, {"HOME": str(fake_home)}), patch.object(
            self.daemon, "run_command", side_effect=fake_run
        ), patch.object(self.daemon, "notify") as notified:
            self.daemon.scan_walled_codex_processes(
                {}, state, snapshot, self._result(codex_home, hard_limit=False), 2000.0, self.log
            )

        self.assertEqual({"303": 2000.0}, state["walledPids"])
        notified.assert_called_once()

    def test_scan_disabled_prunes_but_does_not_inspect_or_notify(self):
        state = self.daemon.default_state()
        state["walledPids"] = {"404": 123.0}
        snapshot = self.daemon.ProviderSnapshot("codex", "", tuple())
        with patch.object(
            self.daemon, "run_command", return_value=(1, "", "")
        ) as run, patch.object(self.daemon, "notify") as notified:
            self.daemon.scan_walled_codex_processes(
                {"walledProcessNotify": False},
                state,
                snapshot,
                self._result("/tmp/codex", hard_limit=False),
                3000.0,
                self.log,
            )

        self.assertEqual({}, state["walledPids"])
        self.assertEqual(1, run.call_count)
        notified.assert_not_called()


class ExecutorTests(SubswitchdTestCase):
    def _switch(self, provider, target):
        return self.daemon.SwitchDecision(provider, "test", "old", target)

    def test_execute_switch_dry_run_does_not_invoke_subprocess(self):
        with patch.object(self.daemon.subprocess, "run") as run:
            switched = self.daemon.execute_switch(self._switch("claude", "2"), False, self.log)
        self.assertFalse(switched)
        run.assert_not_called()

    def test_execute_switch_claude_uses_cswap_json_switch(self):
        with patch.object(
            self.daemon.subprocess, "run", return_value=completed(["cswap"], "{}")
        ) as run:
            switched = self.daemon.execute_switch(self._switch("claude", "4"), True, self.log)
        self.assertTrue(switched)
        self.assertEqual(["cswap", "switch", "4", "--json"], run.call_args.args[0])

    def test_execute_switch_codex_writes_one_line_pointer_atomically(self):
        target = str(Path(self.tempdir.name) / "codex-two")
        with patch.object(self.daemon.subprocess, "run") as run:
            switched = self.daemon.execute_switch(self._switch("codex", target), True, self.log)
        self.assertTrue(switched)
        run.assert_not_called()
        pointer = self.config_root / "codex-current"
        self.assertEqual(target + "\n", pointer.read_text(encoding="utf-8"))
        self.assertEqual([target], pointer.read_text(encoding="utf-8").splitlines())

    def test_notify_dedupes_but_rotation_bypasses_the_window(self):
        state = self.daemon.default_state()
        with patch.object(
            self.daemon.subprocess, "run", side_effect=lambda args, **kwargs: completed(args)
        ) as run:
            self.daemon.notify("first", "ladder", state, 2000.0, self.log)
            self.daemon.notify("second", "ladder", state, 2100.0, self.log)
            self.daemon.notify("rotation", "ladder", state, 2100.0, self.log, rotation=True)
        self.assertEqual(2, run.call_count)
        self.assertEqual(2100.0, state["notifications"]["ladder"])


class ComputerUseConfigGuardTests(SubswitchdTestCase):
    def _config_toml(self, content):
        home = Path(self.tempdir.name) / "home"
        config_path = home / ".codex" / "config.toml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(content, encoding="utf-8")
        return home, config_path

    def _run_guard(self, content):
        home, path = self._config_toml(content)
        state = self.daemon.default_state()
        with patch.dict(os.environ, {"HOME": str(home)}), patch.object(
            self.daemon, "notify"
        ) as notify:
            changed = self.daemon.ensure_computer_use_enabled(self.log, state, 1000.0)
        return changed, path.read_text(encoding="utf-8"), notify

    def test_guard_flips_only_false_in_the_exact_computer_use_section(self):
        content = (
            "[mcp_servers.other]\n"
            "enabled = false\n"
            "other = \"unchanged\"\n"
            "\n"
            "[mcp_servers.computer-use]\n"
            "command = \"computer-use\"\n"
            "enabled = false\n"
            "\n"
            "[mcp_servers.later]\n"
            "enabled = false\n"
        )
        changed, result, notify = self._run_guard(content)

        self.assertTrue(changed)
        self.assertEqual(
            content.replace(
                "command = \"computer-use\"\nenabled = false",
                "command = \"computer-use\"\nenabled = true",
            ),
            result,
        )
        notify.assert_called_once_with(
            "computer-use was disabled (app update?) — re-enabled automatically",
            "config-guard",
            ANY,
            1000.0,
            self.log,
        )

    def test_guard_leaves_true_untouched(self):
        content = "[mcp_servers.computer-use]\nenabled = true\n"
        changed, result, notify = self._run_guard(content)

        self.assertFalse(changed)
        self.assertEqual(content, result)
        notify.assert_not_called()

    def test_guard_preserves_crlf_and_all_other_bytes(self):
        content = (
            b"# a comment with CRLF\r\n"
            b"[mcp_servers.computer-use]\r\n"
            b"enabled = false\r\n"
            b"value = \"unchanged\"\r\n"
        )
        home = Path(self.tempdir.name) / "home"
        path = home / ".codex" / "config.toml"
        path.parent.mkdir(parents=True)
        path.write_bytes(content)
        state = self.daemon.default_state()
        with patch.dict(os.environ, {"HOME": str(home)}), patch.object(self.daemon, "notify"):
            self.assertTrue(self.daemon.ensure_computer_use_enabled(self.log, state, 1000.0))

        self.assertEqual(content.replace(b"enabled = false", b"enabled = true"), path.read_bytes())

    def test_guard_absent_section_is_a_noop_and_logs_once(self):
        content = "[mcp_servers.other]\nenabled = false\n"
        home, path = self._config_toml(content)
        state = self.daemon.default_state()
        with patch.dict(os.environ, {"HOME": str(home)}), patch.object(
            self.daemon, "notify"
        ) as notify:
            self.assertFalse(self.daemon.ensure_computer_use_enabled(self.log, state, 1000.0))
            self.assertFalse(self.daemon.ensure_computer_use_enabled(self.log, state, 1001.0))

        self.assertEqual(content, path.read_text(encoding="utf-8"))
        self.assertEqual(1, self.log.path.read_text(encoding="utf-8").count("section absent"))
        notify.assert_not_called()

    def test_config_guard_knob_off_skips_the_guard(self):
        content = "[mcp_servers.computer-use]\nenabled = false\n"
        home, path = self._config_toml(content)
        config = {
            "enforce": False,
            "notify": False,
            "claude": {"enabled": False},
            "codex": {"enabled": True, "configGuard": False, "homes": []},
        }
        snapshot = self.daemon.ProviderSnapshot("codex", "", tuple())
        with patch.dict(os.environ, {"HOME": str(home)}), patch.object(
            self.daemon, "ensure_computer_use_enabled"
        ) as guard, patch.object(self.daemon, "scan_codex_panes", return_value=[]), patch.object(
            self.daemon, "collect_codex", return_value=snapshot
        ), patch.object(
            self.daemon, "scan_walled_codex_processes"
        ), patch.object(
            self.daemon, "scan_cmux_codex_surfaces", return_value=[]
        ) as cmux_scan, patch.object(self.daemon.time, "time", return_value=1000.0):
            self.daemon.tick(config, self.daemon.default_state(), False, self.log)

        guard.assert_not_called()
        cmux_scan.assert_called_once()
        self.assertEqual(content, path.read_text(encoding="utf-8"))

    def test_cmux_watcher_knob_off_skips_surface_scan(self):
        config = {
            "enforce": False,
            "notify": False,
            "claude": {"enabled": False},
            "codex": {
                "enabled": True,
                "configGuard": False,
                "homes": [],
                "watcher": {"cmux": False},
            },
        }
        snapshot = self.daemon.ProviderSnapshot("codex", "", tuple())
        with patch.object(
            self.daemon, "scan_codex_panes", return_value=[]
        ), patch.object(
            self.daemon, "scan_cmux_codex_surfaces"
        ) as cmux_scan, patch.object(
            self.daemon, "collect_codex", return_value=snapshot
        ), patch.object(
            self.daemon, "scan_walled_codex_processes"
        ), patch.object(self.daemon.time, "time", return_value=1000.0):
            self.daemon.tick(config, self.daemon.default_state(), False, self.log)

        cmux_scan.assert_not_called()


class RuntimeStateTests(SubswitchdTestCase):
    def test_redeem_suppresses_switch_same_tick(self):
        # All accounts starved + a credit; redeem enabled. evaluate() would
        # switch on the stale (pre-redeem) usage — that switch must be suppressed
        # this tick and deferred to the next fresh collection.
        d = self.daemon
        state = d.default_state()
        acct = lambda name, credits=(): d.AccountSnapshot(
            name,
            (d.Window("primary", 10, d.WindowKind.FIVE_HOUR),
             d.Window("secondary", 99, d.WindowKind.WEEKLY, 1000.0 + 5 * 86400)),
            1000.0, None, credits,
        )
        snap = d.ProviderSnapshot("codex", "a", (
            acct("a", (d.ResetCredit("ca", 1000.0 + 20 * 86400, "codex_rate_limits"),)),
            acct("b"),
        ))
        config = {
            "enforce": True, "notify": False,
            "claude": {"enabled": False},
            "codex": {"enabled": True, "configGuard": False, "computerUseCanary": False,
                      "watcher": {"cmux": False}, "redeem": {"enabled": True}},
        }
        with patch.object(d, "collect_codex", return_value=snap), \
             patch.object(d, "scan_codex_panes", return_value=[]), \
             patch.object(d, "guard_codex_identities", return_value={}), \
             patch.object(d, "scan_dead_codex_auth"), \
             patch.object(d, "scan_walled_codex_processes"), \
             patch.object(d, "execute_redeem", return_value="spent") as redeem, \
             patch.object(d, "execute_switch", return_value=True) as switch, \
             patch.object(d.time, "time", return_value=1000.0):
            d.tick(config, state, False, self.log)
        redeem.assert_called_once()
        switch.assert_not_called()  # suppressed after redeem this tick
        # The spend was recorded so it is not re-emitted next tick.
        self.assertIn("ca", state["providers"]["codex"]["redeemed_credits"])

    def test_dry_run_redeem_records_no_spend(self):
        d = self.daemon
        state = d.default_state()
        home = str((Path(self.tempdir.name) / ".codex").resolve())
        snap = d.ProviderSnapshot("codex", home, (
            d.AccountSnapshot(
                home,
                (d.Window("primary", 10, d.WindowKind.FIVE_HOUR),
                 d.Window("secondary", 99, d.WindowKind.WEEKLY, 1000.0 + 5 * 86400)),
                1000.0, None, (d.ResetCredit("ca", 1000.0 + 20 * 86400, "codex_rate_limits"),),
            ),
            d.AccountSnapshot(home + "-2",
                (d.Window("primary", 10, d.WindowKind.FIVE_HOUR),
                 d.Window("secondary", 99, d.WindowKind.WEEKLY, 1000.0 + 2 * 86400)), 1000.0, None),
        ))
        config = {
            "enforce": False, "notify": False,
            "claude": {"enabled": False},
            "codex": {"enabled": True, "configGuard": False, "computerUseCanary": False,
                      "watcher": {"cmux": False}, "redeem": {"enabled": True}},
        }
        with patch.object(d, "collect_codex", return_value=snap), \
             patch.object(d, "scan_codex_panes", return_value=[]), \
             patch.object(d, "guard_codex_identities", return_value={}), \
             patch.object(d, "scan_dead_codex_auth"), \
             patch.object(d, "scan_walled_codex_processes"), \
             patch.object(d.urllib.request, "urlopen") as urlopen, \
             patch.object(d.time, "time", return_value=1000.0):
            d.tick(config, state, False, self.log)
        urlopen.assert_not_called()  # dry-run: no POST
        self.assertEqual({}, state["providers"]["codex"]["redeemed_credits"])

    def test_failed_harvest_switch_restores_harvest_state(self):
        state = self.daemon.default_state()
        state["providers"]["claude"]["last_harvest_at"] = {"claude": 123.0}
        state["providers"]["claude"]["harvested_resets"] = {"claude": {"old": 456.0}}
        observation = self.daemon.ProviderSnapshot(
            "claude",
            "one",
            (
                self.daemon.AccountSnapshot(
                    "one",
                    (
                        self.daemon.Window("fiveHour", 20, self.daemon.WindowKind.FIVE_HOUR),
                        self.daemon.Window("sevenDay", 70, self.daemon.WindowKind.WEEKLY),
                    ),
                    1000.0,
                ),
                self.daemon.AccountSnapshot(
                    "two",
                    (
                        self.daemon.Window("fiveHour", 12, self.daemon.WindowKind.FIVE_HOUR),
                        self.daemon.Window(
                            "sevenDay",
                            13,
                            self.daemon.WindowKind.WEEKLY,
                            1000.0 + 22 * 60 * 60,
                        ),
                    ),
                    1000.0,
                ),
            ),
        )
        config = {
            "enforce": True,
            "notify": False,
            "claude": {"enabled": True},
            "codex": {"enabled": False},
        }
        with patch.object(self.daemon, "collect_claude", return_value=observation), patch.object(
            self.daemon, "execute_switch", return_value=False
        ), patch.object(self.daemon.time, "time", return_value=1000.0):
            self.daemon.tick(config, state, False, self.log)

        self.assertEqual({"claude": 123.0}, state["providers"]["claude"]["last_harvest_at"])
        self.assertEqual(
            {"claude": {"old": 456.0}},
            state["providers"]["claude"]["harvested_resets"],
        )


class CodexIdentityGuardTests(SubswitchdTestCase):
    def _home_with_auth(self, name, account_id, email=None, jwt_account_id=None, complete=True):
        home = Path(self.tempdir.name) / name
        home.mkdir()
        claims = {}
        if email is not None:
            claims["email"] = email
        if jwt_account_id is not None:
            claims["https://api.openai.com/auth"] = {"chatgpt_account_id": jwt_account_id}
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode("utf-8"))
        tokens = {"id_token": "x.%s.y" % payload.decode("ascii").rstrip("=")}
        if account_id is not None:
            tokens["account_id"] = account_id
        if complete:
            tokens["refresh_token"] = "refresh-%s" % (account_id or "x")
            tokens["access_token"] = "access-%s" % (account_id or "x")
        (home / "auth.json").write_text(json.dumps({"tokens": tokens}), encoding="utf-8")
        return home.resolve()

    def test_identity_parses_account_id_email_and_jwt_fallback(self):
        home = self._home_with_auth("a", "acct-1", email="user-a@example.com")
        self.assertEqual(
            {"accountId": "acct-1", "email": "user-a@example.com"},
            self.daemon.codex_home_identity(str(home)),
        )
        fallback = self._home_with_auth("b", None, email="x@y.z", jwt_account_id="acct-jwt")
        self.assertEqual(
            {"accountId": "acct-jwt", "email": "x@y.z"},
            self.daemon.codex_home_identity(str(fallback)),
        )
        empty = Path(self.tempdir.name) / "c"
        empty.mkdir()
        self.assertIsNone(self.daemon.codex_home_identity(str(empty)))

    def test_duplicate_home_excluded_keeps_active_and_notifies(self):
        one = self._home_with_auth("one", "acct-same", email="dup@x.y")
        two = self._home_with_auth("two", "acct-same", email="dup@x.y")
        three = self._home_with_auth("three", "acct-other", email="other@x.y")
        self.config_root.mkdir(parents=True, exist_ok=True)
        (self.config_root / "codex-current").write_text(str(two) + "\n", encoding="utf-8")
        state = self.daemon.default_state()
        config = self.codex_config([one, two, three])
        with patch.object(self.daemon, "notify") as notified:
            excluded = self.daemon.guard_codex_identities(config, state, 1000.0, self.log)

        self.assertEqual([str(one)], list(excluded))
        self.assertIn("duplicate of %s" % str(two), excluded[str(one)])
        self.assertEqual(
            "codex-duplicate:%s:acct-same" % str(one), notified.call_args.args[1]
        )
        self.assertEqual(
            {"accountId": "acct-same", "email": "dup@x.y"},
            state["codexIdentities"][str(one)],
        )

    def _set_auth(self, home, account_id, email, complete=True):
        claims = {"email": email}
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode("utf-8"))
        tokens = {
            "account_id": account_id,
            "id_token": "x.%s.y" % payload.decode("ascii").rstrip("="),
        }
        if complete:
            tokens["refresh_token"] = "refresh-%s" % account_id
            tokens["access_token"] = "access-%s" % account_id
        (Path(home) / "auth.json").write_text(json.dumps({"tokens": tokens}), encoding="utf-8")

    def _account_on_disk(self, home):
        return (self.daemon.codex_home_identity(str(home)) or {}).get("accountId")

    def test_resolve_codex_homes_dedupes_aliases(self):
        one = self._home_with_auth("one", "acct-a", email="a@x.y")
        alias = Path(self.tempdir.name) / "one-alias"
        os.symlink(one, alias)
        homes = self.daemon.resolve_codex_homes({"homes": [str(one), str(alias), str(one)]})
        self.assertEqual([str(one)], homes)  # symlink + repeat collapse to one

    def test_backup_is_written_for_every_sole_holder_home(self):
        one = self._home_with_auth("one", "acct-a", email="a@x.y")
        two = self._home_with_auth("two", "acct-b", email="b@x.y")
        config = self.codex_config([one, two])
        self.daemon.backup_codex_auth_pass(config, self.log)
        for account_id in ("acct-a", "acct-b"):
            backup = self.daemon.codex_auth_backup_path(account_id)
            self.assertTrue(backup.is_file(), "expected backup for %s" % account_id)
            self.assertEqual(
                account_id,
                self.daemon._codex_identity_from_auth(
                    json.loads(backup.read_text(encoding="utf-8"))
                )["accountId"],
            )

    def test_credential_less_payload_is_never_backed_up(self):
        one = self._home_with_auth("one", "acct-a", email="a@x.y", complete=False)
        config = self.codex_config([one])
        self.daemon.backup_codex_auth_pass(config, self.log)
        self.assertFalse(self.daemon.codex_auth_backup_path("acct-a").exists())

    def test_backup_keeps_one_previous_generation(self):
        one = self._home_with_auth("one", "acct-a", email="a@x.y")
        config = self.codex_config([one])
        self.daemon.backup_codex_auth_pass(config, self.log)
        # A different (e.g. rolled-back) credential for the SAME account arrives.
        self._set_auth(one, "acct-a", "a@x.y")  # same id, different token bytes
        self.daemon.backup_codex_auth_pass(config, self.log)
        prev = self.daemon.codex_auth_backup_path("acct-a").with_suffix(".prev.json")
        self.assertTrue(prev.is_file(), "previous generation must be preserved")

    def test_errored_home_is_not_backed_up_over_a_good_backup(self):
        one = self._home_with_auth("one", "acct-a", email="a@x.y")
        config = self.codex_config([one])
        self.daemon.backup_codex_auth_pass(config, self.log)
        good = self.daemon.codex_auth_backup_path("acct-a").read_text(encoding="utf-8")
        # Same account re-writes a new token, but collection reported an auth error.
        self._set_auth(one, "acct-a", "a@x.y")
        self.daemon.backup_codex_auth_pass(config, self.log, skip_homes=frozenset([str(one)]))
        self.assertEqual(
            good, self.daemon.codex_auth_backup_path("acct-a").read_text(encoding="utf-8")
        )

    def test_duplicate_holder_is_never_backed_up(self):
        one = self._home_with_auth("one", "acct-same", email="dup@x.y")
        two = self._home_with_auth("two", "acct-same", email="dup@x.y")
        config = self.codex_config([one, two])
        self.daemon.backup_codex_auth_pass(config, self.log)
        self.assertFalse(self.daemon.codex_auth_backup_path("acct-same").exists())

    def test_daemon_never_rewrites_auth_json_on_clobber(self):
        # Safety invariant: the daemon DETECTS + EXCLUDES a clobber-duplicate but
        # NEVER writes auth.json itself (auto-restore is racy — recovery is the
        # manual bin/codex-restore.sh path). The clobbered file stays byte-exact.
        one = self._home_with_auth("one", "acct-a", email="a@x.y")
        two = self._home_with_auth("two", "acct-b", email="b@x.y")
        state = self.daemon.default_state()
        config = self.codex_config([one, two])
        self.daemon.guard_codex_identities(config, state, 1000.0, self.log)  # pin
        # A login clobbers `two` with `one`'s account — the token-revoking dup.
        self._set_auth(two, "acct-a", "a@x.y")
        before = (Path(two) / "auth.json").read_text(encoding="utf-8")
        (self.config_root).mkdir(parents=True, exist_ok=True)
        (self.config_root / "codex-current").write_text(str(one) + "\n", encoding="utf-8")
        with patch.object(self.daemon, "notify") as notified:
            excluded = self.daemon.guard_codex_identities(config, state, 1060.0, self.log)
        # File untouched; duplicate excluded from rotation; user alerted.
        self.assertEqual(before, (Path(two) / "auth.json").read_text(encoding="utf-8"))
        self.assertIn(str(two), excluded)
        keys = [call.args[1] for call in notified.call_args_list]
        self.assertTrue(any(k.startswith("codex-duplicate:") for k in keys))

    def test_clobber_duplicate_is_excluded_from_rotation(self):
        one = self._home_with_auth("one", "acct-a", email="a@x.y")
        two = self._home_with_auth("two", "acct-a", email="a@x.y")  # duplicate on disk
        state = self.daemon.default_state()
        (self.config_root).mkdir(parents=True, exist_ok=True)
        (self.config_root / "codex-current").write_text(str(one) + "\n", encoding="utf-8")
        config = self.codex_config([one, two])
        excluded = self.daemon.guard_codex_identities(config, state, 1000.0, self.log)
        self.assertEqual("acct-a", self._account_on_disk(two))  # unchanged
        self.assertIn(str(two), excluded)

    def test_identity_change_notifies_and_registry_updates(self):
        home = self._home_with_auth("home", "acct-new", email="new@x.y")
        state = self.daemon.default_state()
        state["codexIdentities"][str(home)] = {"accountId": "acct-old", "email": "old@x.y"}
        config = self.codex_config([home])
        with patch.object(self.daemon, "notify") as notified:
            excluded = self.daemon.guard_codex_identities(config, state, 1000.0, self.log)

        self.assertEqual({}, excluded)
        self.assertIn("old@x.y -> new@x.y", notified.call_args.args[0])
        self.assertEqual(
            {"accountId": "acct-new", "email": "new@x.y"},
            state["codexIdentities"][str(home)],
        )
        with patch.object(self.daemon, "notify") as second:
            self.daemon.guard_codex_identities(config, state, 1060.0, self.log)
        second.assert_not_called()

    def test_collect_codex_skips_excluded_home_without_spawning(self):
        home = self._home_with_auth("solo", "acct-1")
        state = self.daemon.default_state()
        state["codexBackoff"][str(home)] = {"failures": 3, "nextAttemptAt": 9999.0}
        with patch.object(self.daemon, "run_command") as run:
            snapshot = self.daemon.collect_codex(
                self.codex_config([home]),
                state,
                1000.0,
                self.log,
                {str(home): "duplicate of elsewhere (dup@x.y)"},
            )
        run.assert_not_called()
        (account,) = snapshot.accounts
        self.assertEqual((), account.windows)
        self.assertEqual(
            "duplicate account: duplicate of elsewhere (dup@x.y)", account.error
        )
        self.assertNotIn(str(home), state["codexBackoff"])


class DeadAuthWatcherTests(SubswitchdTestCase):
    def _snapshot(self, home, error=None, windows=("w",)):
        acct = self.daemon.AccountSnapshot(
            home,
            tuple(self.daemon.Window("secondary", 5.0, self.daemon.WindowKind.WEEKLY)
                  for _ in windows) if not error else tuple(),
            None if error else 1000.0,
            error,
        )
        return self.daemon.ProviderSnapshot("codex", home, (acct,))

    def test_notifies_once_on_revoked_then_reclears_on_recovery(self):
        home = "/Users/example/.codex-2"
        state = self.daemon.default_state()
        cfg = {"homes": [home]}
        snap = self._snapshot(home, error="401 Unauthorized; token_invalidated")
        with patch.object(self.daemon, "notify") as notified:
            self.daemon.scan_dead_codex_auth(cfg, state, snap, 1000.0, self.log)
            self.daemon.scan_dead_codex_auth(cfg, state, snap, 1005.0, self.log)  # dedupe
        self.assertEqual(1, notified.call_count)
        self.assertIn("codex-relogin.sh", notified.call_args.args[0])
        self.assertIn(home, state["deadAuth"])
        # Home recovers → marker cleared so a future death re-notifies.
        with patch.object(self.daemon, "notify") as n2:
            self.daemon.scan_dead_codex_auth(cfg, state, self._snapshot(home), 1100.0, self.log)
        self.assertNotIn(home, state["deadAuth"])
        n2.assert_not_called()

    def test_transient_errors_do_not_fire(self):
        home = "/Users/example/.codex"
        state = self.daemon.default_state()
        cfg = {"homes": [home]}
        for transient in ("Network error: SSL error", "Codex RPC timed out", "rc=1 connection reset"):
            snap = self._snapshot(home, error=transient)
            with patch.object(self.daemon, "notify") as notified:
                self.daemon.scan_dead_codex_auth(cfg, state, snap, 1000.0, self.log)
            notified.assert_not_called()
        self.assertEqual({}, state["deadAuth"])

    def test_disabled_knob_skips(self):
        home = "/Users/example/.codex"
        state = self.daemon.default_state()
        cfg = {"homes": [home], "deadAuthNotify": False}
        snap = self._snapshot(home, error="refresh token was revoked")
        with patch.object(self.daemon, "notify") as notified:
            self.daemon.scan_dead_codex_auth(cfg, state, snap, 1000.0, self.log)
        notified.assert_not_called()


class ComputerUseCanaryTests(SubswitchdTestCase):
    _BUNDLE_TAIL = ("Codex Computer Use.app/Contents/SharedSupport/"
                    "SkyComputerUseClient.app/Contents/MacOS")

    def _home_with_bundle(self, approvals_ok=True, bundle_ok=True, layout="legacy"):
        home = Path(self.tempdir.name) / ".codex"
        if bundle_ok:
            prefix = ("computer-use" if layout == "flat"
                      else "plugins/cache/openai-bundled/computer-use/1.0.1")
            b = home / prefix / self._BUNDLE_TAIL
            b.mkdir(parents=True)
            (b / "SkyComputerUseClient").write_text("x")
        else:
            home.mkdir(parents=True)
        approvals = Path(self.tempdir.name) / "approvals.json"
        approvals.write_text(json.dumps(
            {"approvedBundleIdentifiers": ["com.google.Chrome"] if approvals_ok else []}))
        return home, approvals

    def test_all_present_is_quiet(self):
        home, approvals = self._home_with_bundle()
        with patch.object(self.daemon, "COMPUTER_USE_APPROVALS", approvals), \
             patch.object(self.daemon, "notify") as notified:
            ok = self.daemon.check_computer_use_ready({"homes": [str(home)]}, self.daemon.default_state(), 1000.0, self.log)
        self.assertTrue(ok)
        notified.assert_not_called()

    def test_missing_bundle_and_approval_both_notify(self):
        home, approvals = self._home_with_bundle(approvals_ok=False, bundle_ok=False)
        with patch.object(self.daemon, "COMPUTER_USE_APPROVALS", approvals), \
             patch.object(self.daemon, "notify") as notified:
            ok = self.daemon.check_computer_use_ready({"homes": [str(home)]}, self.daemon.default_state(), 1000.0, self.log)
        self.assertFalse(ok)
        keys = {c.args[1] for c in notified.call_args_list}
        self.assertTrue(any(k.startswith("cu-canary:bundle") for k in keys))
        self.assertIn("cu-canary:approval", keys)

    def test_unreadable_approval_store_fails_open(self):
        # Under launchd the Group Containers store is unreadable — must NOT alarm.
        home, _ = self._home_with_bundle()
        missing = Path(self.tempdir.name) / "does-not-exist.json"
        with patch.object(self.daemon, "COMPUTER_USE_APPROVALS", missing), \
             patch.object(self.daemon, "notify") as notified:
            ok = self.daemon.check_computer_use_ready({"homes": [str(home)]}, self.daemon.default_state(), 1000.0, self.log)
        self.assertTrue(ok)
        notified.assert_not_called()

    def test_current_flat_bundle_layout_is_quiet(self):
        # The Codex CLI now installs to <home>/computer-use/. The canary only
        # knew the versioned plugin-cache path, so it WARNed on all three homes
        # every tick while computer-use was healthy — and this suite passed
        # because its fixture only ever built the legacy layout.
        home, approvals = self._home_with_bundle(layout="flat")
        with patch.object(self.daemon, "COMPUTER_USE_APPROVALS", approvals), \
             patch.object(self.daemon, "notify") as notified:
            ok = self.daemon.check_computer_use_ready({"homes": [str(home)]}, self.daemon.default_state(), 1000.0, self.log)
        self.assertTrue(ok)
        notified.assert_not_called()

    def test_legacy_path_without_the_app_still_alarms(self):
        # The real machine keeps the legacy directory around holding only
        # assets/bin/scripts/skills. A stale shell must NOT count as a bundle.
        home, approvals = self._home_with_bundle(bundle_ok=False)
        for leftover in ("assets", "bin", "scripts", "skills"):
            (home / "plugins/cache/openai-bundled/computer-use/1.0.1" / leftover).mkdir(parents=True)
        with patch.object(self.daemon, "COMPUTER_USE_APPROVALS", approvals), \
             patch.object(self.daemon, "notify") as notified:
            ok = self.daemon.check_computer_use_ready({"homes": [str(home)]}, self.daemon.default_state(), 1000.0, self.log)
        self.assertFalse(ok)
        self.assertTrue(any(c.args[1].startswith("cu-canary:bundle") for c in notified.call_args_list))

    def test_disabled_knob_skips(self):
        home, approvals = self._home_with_bundle(bundle_ok=False)
        with patch.object(self.daemon, "COMPUTER_USE_APPROVALS", approvals), \
             patch.object(self.daemon, "notify") as notified:
            ok = self.daemon.check_computer_use_ready({"homes": [str(home)], "computerUseCanary": False}, self.daemon.default_state(), 1000.0, self.log)
        self.assertTrue(ok)
        notified.assert_not_called()


class ResetCreditCollectorTests(SubswitchdTestCase):
    def test_parses_available_credits_only_with_expiry(self):
        usage = {"codexResetCredits": {"credits": [
            {"id": "c1", "status": "available", "expires_at": "2026-07-31T19:46:59Z",
             "reset_type": "codex_rate_limits", "title": "Full reset"},
            {"id": "c2", "status": "redeemed", "expires_at": "2026-08-12T17:26:25Z"},
            {"status": "available", "expires_at": "2026-08-01T00:00:00Z"},  # no id → skip
        ]}}
        credits = self.daemon._parse_reset_credits(usage)
        self.assertEqual(1, len(credits))
        self.assertEqual("c1", credits[0].credit_id)
        self.assertEqual("codex_rate_limits", credits[0].reset_type)
        self.assertEqual(
            datetime(2026, 7, 31, 19, 46, 59, tzinfo=timezone.utc).timestamp(),
            credits[0].expires_at,
        )

    def test_missing_or_malformed_block_is_empty(self):
        self.assertEqual((), self.daemon._parse_reset_credits({}))
        self.assertEqual((), self.daemon._parse_reset_credits(None))
        self.assertEqual((), self.daemon._parse_reset_credits({"codexResetCredits": "x"}))


class ClaudeIdentityGuardTests(SubswitchdTestCase):
    def _payload(self, accounts):
        return json.dumps({"accounts": accounts})

    def test_slot_identity_change_notifies(self):
        state = self.daemon.default_state()
        state["claudeIdentities"]["5"] = {"org": "org-old", "email": "old@x.y"}
        payload = self._payload([{"number": 5, "organizationUuid": "org-new", "email": "new@x.y"}])
        with patch.object(self.daemon, "run_command", return_value=(0, payload, "")), \
             patch.object(self.daemon, "notify") as notified:
            self.daemon.guard_claude_identities(state, 1000.0, self.log)
        self.assertIn("changed account", notified.call_args.args[0])
        self.assertEqual({"org": "org-new", "email": "new@x.y"}, state["claudeIdentities"]["5"])

    def test_duplicate_org_across_slots_notifies(self):
        state = self.daemon.default_state()
        payload = self._payload([
            {"number": 1, "organizationUuid": "same", "email": "a@x.y"},
            {"number": 2, "organizationUuid": "same", "email": "b@x.y"},
        ])
        with patch.object(self.daemon, "run_command", return_value=(0, payload, "")), \
             patch.object(self.daemon, "notify") as notified:
            self.daemon.guard_claude_identities(state, 1000.0, self.log)
        keys = [c.args[1] for c in notified.call_args_list]
        self.assertIn("claude-duplicate:same", keys)

    def test_five_distinct_slots_are_quiet(self):
        state = self.daemon.default_state()
        payload = self._payload([
            {"number": n, "organizationUuid": "org-%d" % n, "email": "u%d@x.y" % n}
            for n in range(1, 6)
        ])
        with patch.object(self.daemon, "run_command", return_value=(0, payload, "")), \
             patch.object(self.daemon, "notify") as notified:
            self.daemon.guard_claude_identities(state, 1000.0, self.log)
        notified.assert_not_called()
        self.assertEqual(5, len(state["claudeIdentities"]))


class RedeemExecutorTests(SubswitchdTestCase):
    def _home(self, access="tok", account_id="acct-1"):
        home = Path(self.tempdir.name) / ".codex"
        home.mkdir(parents=True, exist_ok=True)
        (home / "auth.json").write_text(json.dumps(
            {"tokens": {"access_token": access, "account_id": account_id}}))
        return str(home)

    def _decision(self, home):
        return self.daemon.RedeemDecision("codex", "why", home, "cred-1", "uuid-1")

    class _Resp:
        def __init__(self, payload):
            self._b = json.dumps(payload).encode()
        def read(self):
            return self._b
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def _run(self, payload=None, error=None):
        home = self._home()
        state = self.daemon.default_state()
        def fake_urlopen(request, timeout=None):
            # verify contract
            self.assertEqual(self.daemon.CONSUME_URL, request.full_url)
            self.assertEqual("Bearer tok", request.headers["Authorization"])
            self.assertEqual("acct-1", request.headers["Chatgpt-account-id"])
            body = json.loads(request.data.decode())
            self.assertEqual({"redeem_request_id": "uuid-1", "credit_id": "cred-1"}, body)
            if error:
                raise error
            return self._Resp(payload)
        with patch.object(self.daemon.urllib.request, "urlopen", side_effect=fake_urlopen), \
             patch.object(self.daemon, "notify"):
            return self.daemon.execute_redeem(self._decision(home), True, state, 1000.0, self.log)

    def test_reset_is_spent(self):
        self.assertEqual("spent", self._run({"code": "reset", "windows_reset": 2}))

    def test_already_redeemed_is_spent(self):
        self.assertEqual("spent", self._run({"code": "already_redeemed"}))

    def test_nothing_to_reset_not_spent(self):
        self.assertEqual("failed_not_spent", self._run({"code": "nothing_to_reset"}))

    def test_401_retries(self):
        err = self.daemon.urllib.error.HTTPError(self.daemon.CONSUME_URL, 401, "no", {}, None)
        self.assertEqual("ambiguous", self._run(error=err))

    def test_500_retries(self):
        err = self.daemon.urllib.error.HTTPError(self.daemon.CONSUME_URL, 503, "no", {}, None)
        self.assertEqual("ambiguous", self._run(error=err))

    def test_timeout_retries(self):
        self.assertEqual("ambiguous", self._run(error=TimeoutError("timed out")))

    def test_hard_4xx_not_spent(self):
        err = self.daemon.urllib.error.HTTPError(self.daemon.CONSUME_URL, 400, "bad", {}, None)
        self.assertEqual("failed_not_spent", self._run(error=err))

    def test_dry_run_does_not_post(self):
        home = self._home()
        with patch.object(self.daemon.urllib.request, "urlopen") as urlopen:
            outcome = self.daemon.execute_redeem(self._decision(home), False, self.daemon.default_state(), 1000.0, self.log)
        self.assertEqual("dryrun", outcome)
        urlopen.assert_not_called()


class RunCommandTests(SubswitchdTestCase):
    def test_tolerates_non_utf8_output_without_raising(self):
        # `ps eww` reproduces other processes' bytes verbatim; a single non-UTF-8
        # byte (0xbb here) must not raise UnicodeDecodeError and kill the tick.
        code, out, err = self.daemon.run_command(["/usr/bin/printf", "\\273"])
        self.assertEqual(0, code)
        self.assertIn("�", out)  # byte was replaced, not fatal


if __name__ == "__main__":
    unittest.main()
