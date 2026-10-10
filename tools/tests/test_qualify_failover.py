"""Failover qualification: report logic, the safety latch, and process cleanup.

The full eight-scenario run is `python -m tools.qualify_failover`. These tests
cover the decisions that run must get right, against the real simulated Jetson
where a claim is about motors, and without one where a claim is about the report.

Every safety check here has a sabotage twin. The twin turns the latch, the
lease, or the view guard off and proves the check goes red. A check that stays
green after that is not a check.
"""

from contextlib import suppress
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from jetson.control.state import ControlState
from tools.qualify_failover import (NOT_MEASURABLE, REQUIRED_FIELDS, SCENARIO_EXPECT, Harness,
                                    assert_guards_intact, choose_upstream, guard_constants, judge,
                                    live_probe, markdown_report, mark_unmeasured, overall_passed,
                                    read_view_guard_ms, refused_gap, scenario_row, summarize)


class ReportLogic(unittest.TestCase):
    def test_guards_are_read_from_the_code(self):
        constants = assert_guards_intact()
        self.assertEqual(constants["permit_lease_ms"], 250)
        self.assertEqual(ControlState.LEASE, 0.250)
        self.assertEqual(constants["view_guard_ms"], 500)
        self.assertEqual(guard_constants()["permit_lease_s"], ControlState.LEASE)
        # The 500 is not a copy of itself. It is parsed out of the two files
        # that enforce it. A moved number in either file fails the read.
        self.assertEqual(read_view_guard_ms(
            state_text="not 0 <= video_age <= 500:",
            quest_text="video_fresh: age < 500,"), 500)
        with self.assertRaises(RuntimeError):
            read_view_guard_ms(state_text="not 0 <= video_age <= 800:",
                               quest_text="video_fresh: age < 500,")
        with self.assertRaises(RuntimeError):
            read_view_guard_ms(state_text="no bound here", quest_text="video_fresh: age < 500,")

    def test_a_moved_lease_or_view_guard_is_refused(self):
        with self.assertRaises(RuntimeError):
            assert_guards_intact({"permit_lease_s": 0.5, "view_guard_ms": 500})
        with self.assertRaises(RuntimeError):
            assert_guards_intact({"permit_lease_s": 0.250, "view_guard_ms": 800})

    def test_choose_upstream_is_connection_time_only(self):
        paths = {"A": {"origin": "http://127.0.0.1:1", "available": False},
                 "B": {"origin": "http://127.0.0.1:2", "available": True}}
        name, origin, tries = choose_upstream(paths, prefer="A")
        self.assertEqual((name, origin), ("B", "http://127.0.0.1:2"))
        self.assertEqual([item["name"] for item in tries], ["A", "B"])
        self.assertEqual(choose_upstream(paths, prefer="B")[:2], ("B", "http://127.0.0.1:2"))
        both_dead = {"A": {"origin": None, "available": False}, "B": {"origin": None, "available": False}}
        self.assertEqual(choose_upstream(both_dead)[:2], (None, None))
        # Nothing answered, so the outage is still going. A number here would be
        # the harness giving up and calling that the gap.
        self.assertIsNone(refused_gap(choose_upstream(both_dead)[2]))
        # A stalled path is still the path the open socket is on. The picker does
        # not move an established stream; the caller decides that.
        stalled = {"A": {"origin": "http://127.0.0.1:1", "available": True},
                   "B": {"origin": "http://127.0.0.1:2", "available": True}}
        self.assertEqual(choose_upstream(stalled, prefer="A")[0], "A")

    def test_choose_upstream_tries_a_down_path_instead_of_skipping_it(self):
        paths = {"A": {"origin": "http://127.0.0.1:1", "available": False},
                 "B": {"origin": "http://127.0.0.1:2", "available": True}}
        seen = []

        def probe(name, origin):
            seen.append((name, origin))
            time.sleep(0.05)
            return name == "B"

        name, origin, tries = choose_upstream(paths, prefer="A", probe=probe)
        self.assertEqual(seen, [("A", "http://127.0.0.1:1"), ("B", "http://127.0.0.1:2")])
        self.assertEqual((name, origin), ("B", "http://127.0.0.1:2"))
        self.assertFalse(tries[0]["ok"])
        self.assertGreaterEqual(tries[0]["elapsed_s"], 0.04)
        self.assertEqual(refused_gap(tries), tries[0]["elapsed_s"])
        # The report default keeps probing after the first answer. A phone
        # bridge that binds to this must pass probe_all=False, or every
        # connect also waits on the fallback probe.
        asked = []

        def recording(name, origin):
            asked.append(name)
            return name == "B"

        stopped, _origin, stopped_tries = choose_upstream(
            paths, prefer="B", probe=recording, probe_all=False)
        self.assertEqual(stopped, "B")
        self.assertEqual(asked, ["B"])
        self.assertEqual([item["name"] for item in stopped_tries], ["B"])

    def test_service_passes_only_when_the_other_path_answers(self):
        passed, _ = judge("service", service_up=True, motors_stopped=True,
                          release_held=True, replayed=False, recovered=False)
        self.assertTrue(passed)
        passed, reason = judge("service", service_up=False, motors_stopped=True,
                               release_held=True, replayed=False, recovered=False)
        self.assertFalse(passed)
        self.assertIn("connect time", reason)

    def test_a_replayed_command_fails_every_scenario(self):
        for expect in SCENARIO_EXPECT.values():
            passed, reason = judge(expect, service_up=True, motors_stopped=True,
                                   release_held=True, replayed=True, recovered=True)
            self.assertFalse(passed, expect)
            self.assertIn("replayed", reason)

    def test_midstream_loss_must_stop_and_must_not_claim_the_stream_moved(self):
        passed, _ = judge("stop_then_recover", service_up=False, motors_stopped=True,
                          release_held=True, replayed=False, recovered=True)
        self.assertTrue(passed)
        passed, reason = judge("stop_then_recover", service_up=False, motors_stopped=False,
                               release_held=False, replayed=False, recovered=True)
        self.assertFalse(passed)
        self.assertIn("stop", reason)
        passed, reason = judge("stop", service_up=False, motors_stopped=True,
                               release_held=True, replayed=False, recovered=True)
        self.assertFalse(passed)
        self.assertIn("came back", reason)

    def test_summary_splits_kept_service_from_a_correct_stop(self):
        rows = [
            {"scenario": "primary_unavailable_before_connect", "passed": True, "service": "kept"},
            {"scenario": "stalled_primary", "passed": True, "service": "stopped"},
            {"scenario": "primary_loss_http", "passed": True, "service": "restored_after_gap"},
            {"scenario": "recovery", "passed": False, "service": "stopped"},
        ]
        summary = summarize(rows)
        self.assertEqual(summary["single_path_keeps_service"], ["primary_unavailable_before_connect"])
        self.assertIn("stalled_primary", summary["correctly_stops"][0])
        self.assertEqual(summary["failed"], ["recovery"])
        self.assertEqual(summary["skipped"], [])
        self.assertIn("cannot move", summary["connection_time_vs_midstream"])
        skipped = [{"scenario": "primary_loss_h264", "passed": None, "service": "not_run"}]
        self.assertEqual(summarize(skipped)["skipped"], ["primary_loss_h264"])
        self.assertEqual(summarize(skipped)["failed"], [])
        # A skipped H.264 row is not a pass, and it does not fail the runs that did happen.
        self.assertFalse(overall_passed(skipped))
        self.assertTrue(overall_passed(rows[:3] + skipped))
        self.assertFalse(overall_passed(rows))

    def test_report_schema_has_every_required_field(self):
        row = scenario_row(scenario="both_paths_lost", passed=True, reason="stopped")
        missing = [field for field in REQUIRED_FIELDS if field not in row]
        self.assertEqual(missing, [])
        self.assertEqual(row["scope"], "simulated")
        self.assertIn("p50_ms", row["control_rtt"])
        self.assertIn("p95_ms", row["control_rtt"])
        self.assertIn("held_command_replayed", row["explicit_release"])
        self.assertFalse(row["explicit_release"]["held_command_replayed"])
        report = {"scope": "simulated", "guards": guard_constants(), "scenarios": [row],
                  "summary": summarize([row]), "live": None}
        text = markdown_report(report)
        self.assertIn("simulated", text)
        self.assertIn("250", text)
        self.assertIn("500", text)
        self.assertIn("both_paths_lost", text)
        self.assertIn("Live mode was not claimed", text)
        ongoing = scenario_row(scenario="both_paths_lost", interruption_s=None, passed=True)
        ongoing_text = markdown_report({"scope": "simulated", "guards": guard_constants(),
                                        "scenarios": [mark_unmeasured(ongoing)],
                                        "summary": summarize([ongoing]), "live": None})
        self.assertIn("ongoing", ongoing_text)
        self.assertIn(NOT_MEASURABLE, ongoing_text)
        skipped_row = scenario_row(scenario="primary_loss_h264", passed=None, service="not_run")
        skipped_text = markdown_report({"scope": "simulated", "guards": guard_constants(),
                                        "scenarios": [skipped_row],
                                        "summary": summarize([skipped_row]), "live": None})
        self.assertIn("skipped", skipped_text)
        self.assertNotIn("FAIL", skipped_text)
        report["live"] = {"origin": "http://127.0.0.1:9", "health": {"camera": True}}
        live_text = markdown_report(report)
        self.assertNotIn("Live mode was not claimed", live_text)
        self.assertIn("http://127.0.0.1:9", live_text)

    def test_http_and_h264_scenarios_are_not_the_control_cut(self):
        source = Path(__file__).resolve().parents[1].joinpath("qualify_failover.py").read_text()
        self.assertIn("async def http_loss", source)
        self.assertIn("async def h264_loss", source)
        self.assertIn("async def control_loss", source)
        http = source.split("async def http_loss")[1].split("\nasync def ")[0]
        h264 = source.split("async def h264_loss")[1].split("\nasync def ")[0]
        self.assertIn("/camera.jpg", http)
        self.assertIn("/ws/video", h264)
        self.assertIn("open_h264", h264)
        # The video socket must still be open at the cut, not closed inside a with-block first.
        self.assertLess(h264.index("open_h264"), h264.index("cut_path"))
        self.assertLess(h264.index("cut_path"), h264.index("video.close()"))


class Latch(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.harness = Harness(h264=False)
        await self.harness.__aenter__()

    async def asyncTearDown(self):
        await self.harness.close()

    async def _drive(self):
        self.harness.paths["B"].cut()
        name = await self.harness.connect_relay(prefer="A")
        self.assertEqual(name, "A")
        control = await self.harness.open_control()
        held = await self.harness.hold_motion(control, seconds=0.3)
        self.assertTrue(held, "simulated motion never applied")
        return control

    async def test_replay_attack_is_answered_on_its_own_seq(self):
        control = await self._drive()
        try:
            attack = await self.harness.inject_replay_attack(control)
            self.assertTrue(attack["rejected"], attack)
            self.assertEqual(attack["echoed_seq"], attack["replayed_seq"])
            self.assertIn(attack["after_reason"], ("stale_sequence", "expired_permit", "release_required"))
            self.assertEqual(attack["after_pwm"], [0, 0])
            self.assertFalse(attack["held_command_replayed"])
            # The reply came back inside the lease. No default: a missing
            # elapsed_s used to compare 0.12 and always pass.
            self.assertIn("elapsed_s", attack)
            self.assertLess(attack["elapsed_s"], 0.25)
        finally:
            with suppress(Exception):
                await control.ws.close()

    async def test_sabotaged_latch_lets_the_replay_attack_land(self):
        """A server that accepted the replay must make this check go red.

        The Jetson is another process, so the sabotage is the reply the harness
        would see if accept() no longer stopped on a stale seq: same seq, reason
        active, nonzero PWM. Patching ControlState in this process would not
        reach that server and would leave the check green.
        """
        control = await self._drive()
        sent = next(item for item in reversed(control.sent) if item["kind"] == "motion")
        try:
            async def accepted(_seq, timeout=0.12):
                return {"seq": sent["seq"], "reason": "active", "left_pwm": 40, "right_pwm": 40,
                        "requires_release": False}
            with patch.object(control, "reply_for", accepted):
                attack = await self.harness.inject_replay_attack(control)
            self.assertFalse(attack["rejected"], attack)
            self.assertTrue(attack["held_command_replayed"])
            self.assertEqual(attack["after_reason"], "active")
            self.assertEqual(attack["after_pwm"], [40, 40])
        finally:
            with suppress(Exception):
                await control.ws.close()

    async def test_old_command_on_a_new_session_is_expired_permit(self):
        control = await self._drive()
        try:
            await self.harness.paths["A"].reset_clients()
            await self.harness.paths["A"].start(self.harness.group)
            await self.harness.connect_relay(prefer="A")
            fresh = await self.harness.inject_old_command_attack(control)
            self.assertTrue(fresh["connected"], fresh)
            self.assertEqual(fresh["reason"], "expired_permit", fresh)
            # accept() does not store a rejected seq, so the echo stays at the
            # session-open -1. The attack seq is what we sent, recorded separately.
            self.assertEqual(fresh["answered_seq"], control.sent[-1]["seq"])
            self.assertTrue(fresh["pwm_stayed_zero"])
            self.assertTrue(fresh["requires_release"])
            self.assertFalse(fresh["held_command_replayed"])
            # release_required is the session-open latch. Seeing it means the
            # attack was never processed. That must not count as a pass.
            self.assertNotEqual(fresh["reason"], "release_required")
        finally:
            with suppress(Exception):
                await control.ws.close()

    async def test_sabotaged_new_session_that_never_answers_goes_red(self):
        control = await self._drive()
        try:
            await self.harness.paths["A"].reset_clients()
            await self.harness.paths["A"].start(self.harness.group)
            await self.harness.connect_relay(prefer="A")

            class Silent:
                def __init__(self):
                    self.session_id = "new"
                    self.seq = -1
                    self.sent = []
                    self.ws = self
                    self.closed = False
                async def send_json(self, _message):
                    return None
                async def reply_for(self, _seq, timeout=0.12):
                    return None
                async def close(self):
                    self.closed = True

            silent = Silent()
            async def open_silent():
                return silent
            with patch.object(self.harness, "open_control", open_silent):
                fresh = await self.harness.inject_old_command_attack(control)
            self.assertTrue(fresh["connected"])
            self.assertIsNone(fresh["reason"])
            self.assertTrue(fresh["held_command_replayed"])
            self.assertFalse(fresh.get("pwm_stayed_zero"))
        finally:
            with suppress(Exception):
                await control.ws.close()

    async def test_press_without_release_is_rejected_on_its_seq(self):
        await self.harness.connect_relay(prefer="A")
        control = await self.harness.open_control()
        try:
            probe = await self.harness.press_without_release_is_rejected(control)
            self.assertTrue(probe["rejected"], probe)
            self.assertEqual(probe["reason"], "release_required")
            self.assertEqual(probe["sent_seq"], 0)
            self.assertEqual(probe["after_pwm"], [0, 0])
        finally:
            await control.ws.close()

    async def test_sabotaged_lease_accepts_the_press_and_the_check_goes_red(self):
        """A press the server wrongly accepted must not pass just because 300 ms elapsed."""
        await self.harness.connect_relay(prefer="A")
        control = await self.harness.open_control()
        await control.pump_one(timeout=0.5)
        try:
            async def accepted(seq, timeout=0.12):
                return {"seq": seq, "reason": "active", "left_pwm": 40, "right_pwm": 40,
                        "requires_release": False}
            with patch.object(control, "reply_for", accepted):
                probe = await self.harness.press_without_release_is_rejected(control)
            self.assertFalse(probe["rejected"], probe)
            self.assertEqual(probe["reason"], "active")
            self.assertNotEqual(probe["after_pwm"], [0, 0])
        finally:
            await control.ws.close()

    async def test_stale_frame_is_video_stale(self):
        await self.harness.connect_relay(prefer="A")
        control = await self.harness.open_control()
        try:
            probe = await self.harness.view_guard_rejects_stale_frame(control)
            self.assertTrue(probe["rejected"], probe)
            self.assertIn(probe["reason"], ("video_stale", "deadman_released"))
            self.assertEqual(probe["guard_ms"], 500)
            self.assertEqual(probe["sent_age_ms"], 501)
            self.assertEqual(probe["after_pwm"], [0, 0])
        finally:
            await control.ws.close()

    async def test_a_widened_view_guard_would_accept_501_and_fail_this_check(self):
        """501 only proves the guard if the server's bound is still 500.

        Sabotage: the reply says the server accepted it (reason active). The
        check must go red. Reading our own constant and comparing it to 500
        would have stayed green.
        """
        await self.harness.connect_relay(prefer="A")
        control = await self.harness.open_control()
        try:
            async def accepted(seq, timeout=0.12):
                return {"seq": seq, "reason": "active", "left_pwm": 40, "right_pwm": 40,
                        "requires_release": False}
            with patch.object(control, "reply_for", accepted):
                probe = await self.harness.view_guard_rejects_stale_frame(control)
            self.assertFalse(probe["rejected"], probe)
            self.assertNotEqual(probe.get("reason"), "video_stale")
        finally:
            await control.ws.close()

    async def test_refuses_to_drive_once_a_stop_was_required(self):
        await self.harness.connect_relay(prefer="A")
        control = await self.harness.open_control()
        try:
            control.stopped = True
            with self.assertRaises(RuntimeError):
                await control.drive_once()
            status = await self.harness.direct_status()
            self.assertEqual(status["left_pwm"], 0)
            self.assertEqual(status["right_pwm"], 0)
        finally:
            await control.ws.close()

    async def test_unstalled_heartbeats_keep_moving(self):
        """The stall scenario's control. Without a stall, keep-alive holds PWM."""
        await self.harness.connect_relay(prefer="A")
        control = await self.harness.open_control()
        try:
            started = time.monotonic()
            held = await self.harness.hold_motion(control, seconds=0.45, keep_alive=True)
            elapsed = time.monotonic() - started
            self.assertTrue(held)
            self.assertGreater(elapsed, 0.4)
            status = await self.harness.direct_status()
            self.assertTrue(status["left_pwm"] or status["right_pwm"], status)
        finally:
            await control.ws.close()


def _awaitable(value):
    async def produce():
        return value
    return produce()


class Cleanup(unittest.IsolatedAsyncioTestCase):
    async def test_a_failed_scenario_still_reaps_every_process(self):
        harness = Harness(h264=False)
        await harness.__aenter__()
        pids = [owned.pid for owned in harness.group.owned]
        self.assertTrue(pids, "the simulated Jetson was not started")
        for owned in harness.group.owned:
            self.assertIsNone(owned.poll())
        try:
            raise RuntimeError("scenario blew up")
        except RuntimeError:
            pass
        finally:
            await harness.close()
        import os
        for pid in pids:
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        self.assertEqual(harness.group.owned, [])
        self.assertEqual(harness.group.forwarders, [])

    def test_atexit_and_sigterm_reap_the_group(self):
        import signal
        from tools.qualify_failover import ProcessGroup, _atexit_reap, _on_signal, install_signal_handlers
        # Importing the module must leave Ctrl-C alone. main() installs the
        # handler, and the atexit hook is registered at import. atexit hides its
        # callback list, so the proof is calling the hook and watching it reap.
        self.assertIsNot(signal.getsignal(signal.SIGINT), _on_signal)
        previous = signal.getsignal(signal.SIGTERM)
        install_signal_handlers()
        try:
            self.assertIs(signal.getsignal(signal.SIGTERM), _on_signal)
            self.assertIs(signal.getsignal(signal.SIGINT), _on_signal)
        finally:
            signal.signal(signal.SIGTERM, previous)
            signal.signal(signal.SIGINT, signal.default_int_handler)
        group = ProcessGroup()
        owned = _FakeOwned()
        group.owned.append(owned)
        _atexit_reap()
        self.assertTrue(group.closed)
        self.assertTrue(owned.closed)
        self.assertEqual(group.owned, [])
        self.assertNotIn(group, ProcessGroup._active)

    def test_live_probe_never_builds_a_control_socket(self):
        source = Path(__file__).resolve().parents[1].joinpath("qualify_failover.py").read_text()
        probe = source.split("async def live_probe")[1].split("\ndef write_report")[0]
        gets = [line for line in probe.splitlines() if "session.get(" in line]
        self.assertEqual(sorted(line.strip() for line in gets), [
            'async with session.get(str(origin) + "/control/status") as response:',
            'async with session.get(str(origin) + "/health") as response:',
        ])
        self.assertNotIn("ws_connect", probe)

    def test_the_qualification_run_does_not_resend_a_held_command(self):
        source = Path(__file__).resolve().parents[1].joinpath("qualify_failover.py").read_text()
        runner = source.split("async def run_scenarios")[1].split("\ndef write_report")[0]
        # The two probes exist for the unit tests. The scenario runner must not call them.
        self.assertIn("async def inject_replay_attack", source)
        self.assertIn("async def inject_old_command_attack", source)
        self.assertNotIn("inject_replay_attack(", runner)
        self.assertNotIn("inject_old_command_attack(", runner)
        self.assertIn("press_without_release_is_rejected", runner)
        guide = Path(__file__).resolve().parents[2].joinpath("docs", "failover-qualification.md").read_text()
        self.assertIn("never resends a held command", guide)
        self.assertIn("inject_replay_attack", guide)
        self.assertIn("simulated server", guide)


class _FakeOwned:
    closed = False

    def close(self):
        self.closed = True


class LiveGuard(unittest.IsolatedAsyncioTestCase):
    async def test_live_probe_reads_health_and_status_only(self):
        from aiohttp import web
        seen = []

        async def health(request):
            seen.append(request.path)
            return web.json_response({"camera": True})

        async def status(request):
            seen.append(request.path)
            return web.json_response({"simulated": False, "left_pwm": 40, "right_pwm": 40,
                                      "reason": "active", "requires_release": False, "rover": True,
                                      "permit": "must-not-leak", "session_id": "must-not-leak"})

        async def control(request):
            seen.append("CONTROL OPENED")
            raise web.HTTPForbidden()

        app = web.Application()
        app.router.add_get("/health", health)
        app.router.add_get("/control/status", status)
        app.router.add_get("/ws/control", control)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            report = await live_probe(f"http://127.0.0.1:{port}")
        finally:
            await runner.cleanup()
        self.assertNotIn("CONTROL OPENED", seen)
        self.assertEqual(seen, ["/health", "/control/status"])
        self.assertNotIn("error", report)
        self.assertFalse(report["control_socket_opened"])
        self.assertFalse(report["motion_requested"])
        self.assertNotIn("permit", report["control_status"])
        self.assertNotIn("session_id", report["control_status"])
        self.assertIsNone(report["control_status"]["left_pwm"])

    async def test_a_simulated_live_status_also_drops_the_permit(self):
        from aiohttp import web

        async def health(_request):
            return web.json_response({"camera": True})

        async def status(_request):
            return web.json_response({"simulated": True, "left_pwm": 0, "right_pwm": 0,
                                      "permit": "still-a-secret", "session_id": "still-a-secret"})

        app = web.Application()
        app.router.add_get("/health", health)
        app.router.add_get("/control/status", status)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            report = await live_probe(f"http://127.0.0.1:{port}")
        finally:
            await runner.cleanup()
        self.assertNotIn("permit", report["control_status"])
        self.assertNotIn("session_id", report["control_status"])
        self.assertEqual(report["control_status"]["left_pwm"], 0)

    async def test_a_tls_failure_stays_in_the_live_block(self):
        # Nothing is listening, and the origin is https, so the default cert
        # check fails. That used to abort the whole qualification run.
        report = await live_probe("https://127.0.0.1:1")
        self.assertFalse(report["reachable"])
        self.assertIn("error", report)
        self.assertFalse(report["control_socket_opened"])
        self.assertFalse(report["motion_requested"])
        self.assertNotIn("/ws/control", report["error"])


if __name__ == "__main__":
    unittest.main()
