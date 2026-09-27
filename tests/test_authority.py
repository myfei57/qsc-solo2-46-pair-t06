"""Unit and integration tests for exclusive control-station arbitration.

Covers the four requirements:
* only one station is in command at a time,
* changing the commander is an explicit, two-party handover,
* the emergency stop overrides whoever holds control,
* every transition is recorded in the audit log and survives a restart.
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from cranefleet import authority as auth
from cranefleet.audit import Audit
from cranefleet.console import Console, Request
from cranefleet.crane import ACTION_ESTOP
from cranefleet.errors import (
    AuthorityHeldByOther,
    AuthorityIdle,
    EmergencyStopActive,
    HandoverOfferedByOther,
    NoPendingHandover,
    StationNotHolder,
    UnknownStation,
)
from cranefleet.store import MemoryStore
from cranefleet.wiring import build_app

WEB_ROOT = Path(__file__).resolve().parents[1] / "web"


def dispatch(console: Console, method: str, path: str, body: dict | None = None):  # noqa: ANN201
    request = Request(method=method, path=path, query={}, body=body or {}, body_valid=True)
    return console.dispatch(request)


class AuthorityUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.auth = auth.ControlAuthority("crane-1")
        self.audit = Audit(200)
        self.store = MemoryStore()

    def test_initial_state_has_no_holder(self) -> None:
        self.assertIsNone(self.auth.holder_now())
        self.assertFalse(self.auth.estop_active())
        self.assertIsNone(self.auth.pending_handover())

    def test_take_when_idle_grants_immediately(self) -> None:
        auth.take(self.auth, "cab", self.audit)
        self.assertEqual(self.auth.holder_now(), "cab")
        # Idempotent for the holder.
        auth.take(self.auth, "cab", self.audit)
        self.assertEqual(self.auth.generation_now(), 1)

    def test_take_cannot_steal_control(self) -> None:
        auth.take(self.auth, "cab", self.audit)
        with self.assertRaises(AuthorityHeldByOther):
            auth.take(self.auth, "remote", self.audit)
        self.assertEqual(self.auth.holder_now(), "cab")

    def test_unknown_station_is_rejected(self) -> None:
        with self.assertRaises(UnknownStation):
            auth.take(self.auth, "footpedal", self.audit)
        with self.assertRaises(UnknownStation):
            auth.trigger_estop(self.auth, "footpedal", self.audit)

    def test_explicit_offer_then_accept_handover(self) -> None:
        auth.take(self.auth, "cab", self.audit)
        handover = auth.offer(self.auth, "cab", self.audit)
        self.assertEqual(handover.offered_by, "cab")
        self.assertEqual(handover.offered_to, "remote")
        # Until acceptance the original holder stays in command.
        self.assertEqual(self.auth.holder_now(), "cab")
        self.assertIsNotNone(self.auth.pending_handover())
        previous = auth.accept_handover(self.auth, "remote", self.audit)
        self.assertEqual(previous, "cab")
        self.assertEqual(self.auth.holder_now(), "remote")
        self.assertIsNone(self.auth.pending_handover())
        actions = [entry.action for entry in self.audit.snapshot()]
        self.assertEqual(
            actions,
            ["control-take", "control-offer", "control-handover"],
        )

    def test_offer_requires_the_holder(self) -> None:
        auth.take(self.auth, "cab", self.audit)
        with self.assertRaises(StationNotHolder):
            auth.offer(self.auth, "remote", self.audit)

    def test_offer_when_idle_is_rejected(self) -> None:
        with self.assertRaises(AuthorityIdle):
            auth.offer(self.auth, "cab", self.audit)

    def test_accept_is_restricted_to_offered_station(self) -> None:
        auth.take(self.auth, "cab", self.audit)
        auth.offer(self.auth, "cab", self.audit)
        with self.assertRaises(HandoverOfferedByOther):
            auth.accept_handover(self.auth, "cab", self.audit)
        # A bad accept leaves the holder in place.
        self.assertEqual(self.auth.holder_now(), "cab")

    def test_reject_keeps_the_original_holder(self) -> None:
        auth.take(self.auth, "cab", self.audit)
        auth.offer(self.auth, "cab", self.audit)
        offered_by = auth.reject_handover(self.auth, "remote", self.audit)
        self.assertEqual(offered_by, "cab")
        self.assertEqual(self.auth.holder_now(), "cab")
        self.assertIsNone(self.auth.pending_handover())
        self.assertIsNotNone(self.audit.latest("control-handover-reject"))

    def test_accept_without_offer_is_rejected(self) -> None:
        with self.assertRaises(NoPendingHandover):
            auth.accept_handover(self.auth, "remote", self.audit)

    def test_cancel_by_either_side_returns_to_holder(self) -> None:
        auth.take(self.auth, "cab", self.audit)
        auth.offer(self.auth, "cab", self.audit)
        auth.cancel_handover(self.auth, "cab", self.audit)
        self.assertEqual(self.auth.holder_now(), "cab")
        self.assertIsNone(self.auth.pending_handover())

        auth.offer(self.auth, "cab", self.audit)
        auth.cancel_handover(self.auth, "remote", self.audit)
        self.assertIsNone(self.auth.pending_handover())
        with self.assertRaises(NoPendingHandover):
            auth.cancel_handover(self.auth, "remote")

    def test_release_returns_station_to_idle(self) -> None:
        auth.take(self.auth, "cab", self.audit)
        auth.release(self.auth, "cab", self.audit)
        self.assertIsNone(self.auth.holder_now())
        # Now the remote can take control without a handover.
        auth.take(self.auth, "remote", self.audit)
        self.assertEqual(self.auth.holder_now(), "remote")

    def test_non_holder_cannot_release(self) -> None:
        auth.take(self.auth, "cab", self.audit)
        with self.assertRaises(StationNotHolder):
            auth.release(self.auth, "remote", self.audit)

    def test_admit_gates_motion(self) -> None:
        with self.assertRaises(StationNotHolder):
            auth.admit(self.auth, "cab", "swing")
        auth.take(self.auth, "cab", self.audit)
        auth.admit(self.auth, "cab", "swing")
        with self.assertRaises(StationNotHolder):
            auth.admit(self.auth, "remote", "swing")

    def test_emergency_stop_from_either_station_latches(self) -> None:
        auth.take(self.auth, "cab", self.audit)
        self.assertTrue(auth.trigger_estop(self.auth, "remote", self.audit))
        self.assertTrue(self.auth.estop_active())
        self.assertEqual(self.auth.estop_source_now(), "remote")
        # The holder did not change: the original station keeps command.
        self.assertEqual(self.auth.holder_now(), "cab")
        # Re-triggering is idempotent and still allowed from either side.
        self.assertFalse(auth.trigger_estop(self.auth, "cab", self.audit))
        self.assertEqual(
            self.audit.latest("emergency-stop").detail.split()[0],
            "station=cab",
        )
        with self.assertRaises(EmergencyStopActive):
            auth.admit(self.auth, "cab", "swing")
        with self.assertRaises(EmergencyStopActive):
            auth.admit(self.auth, "remote", "trolley")

    def test_estop_cancels_pending_handover(self) -> None:
        auth.take(self.auth, "cab", self.audit)
        auth.offer(self.auth, "cab", self.audit)
        auth.trigger_estop(self.auth, "remote", self.audit)
        self.assertIsNone(self.auth.pending_handover())
        # After reset the cab must explicitly re-offer; no stale accept.
        auth.clear_estop(self.auth, "cab", self.audit)
        with self.assertRaises(NoPendingHandover):
            auth.accept_handover(self.auth, "remote", self.audit)

    def test_clear_estop_is_idempotent_and_logged(self) -> None:
        auth.trigger_estop(self.auth, "cab", self.audit)
        auth.clear_estop(self.auth, "remote", self.audit)
        self.assertFalse(self.auth.estop_active())
        self.assertIsNone(self.auth.estop_source_now())
        self.assertIsNotNone(self.audit.latest("emergency-stop-reset"))

    def test_persist_and_restore_keeps_holder_and_estop(self) -> None:
        auth.take(self.auth, "remote", self.audit)
        auth.persist(self.auth, self.store)
        restored = auth.ControlAuthority("crane-1")
        auth.restore(restored, self.store)
        self.assertEqual(restored.holder_now(), "remote")
        self.assertFalse(restored.estop_active())

        auth.trigger_estop(self.auth, "cab", self.audit)
        auth.persist(self.auth, self.store)
        restored = auth.ControlAuthority("crane-1")
        auth.restore(restored, self.store)
        self.assertTrue(restored.estop_active())
        self.assertEqual(restored.estop_source_now(), "cab")

    def test_pending_handover_is_not_persisted(self) -> None:
        auth.take(self.auth, "cab", self.audit)
        auth.offer(self.auth, "cab", self.audit)
        # persist only writes holder/estop, and a fresh restore must not carry
        # the half-finished handover across a restart.
        auth.persist(self.auth, self.store)
        restored = auth.ControlAuthority("crane-1")
        auth.restore(restored, self.store)
        self.assertIsNone(restored.pending_handover())
        self.assertEqual(restored.holder_now(), "cab")

    def test_restore_without_snapshot_is_noop(self) -> None:
        auth.restore(self.auth, MemoryStore())
        self.assertIsNone(self.auth.holder_now())


class AuthorityApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = build_app("memory", web_root=str(WEB_ROOT))
        self.console = self.app.console

    def tearDown(self) -> None:
        self.app.shutdown()

    def _audit_actions(self) -> list[str]:
        return [entry.action for entry in self.console.audit.snapshot()]

    def test_state_exposes_control_block(self) -> None:
        status, payload = dispatch(self.console, "GET", "/api/state")
        self.assertEqual(status, 200)
        control = payload["data"]["control"]
        self.assertIsNone(control["holder"])
        self.assertFalse(control["estop"])
        self.assertIsNone(control["pending_handover"])

    def test_motion_without_holder_is_refused(self) -> None:
        status, payload = dispatch(
            self.console, "POST", "/api/crane/command", {"source": "cab", "action": "lower"}
        )
        self.assertEqual(status, 409)
        self.assertIn("holds control", payload["error"])

    def test_take_then_motion_flows_and_other_end_is_locked_out(self) -> None:
        status, payload = dispatch(self.console, "POST", "/api/control/take", {"station": "cab"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["holder"], "cab")

        status, payload = dispatch(
            self.console, "POST", "/api/crane/command", {"source": "cab", "action": "lower"}
        )
        self.assertEqual(status, 200)
        status, payload = dispatch(
            self.console, "POST", "/api/crane/command", {"source": "remote", "action": "trolley"}
        )
        self.assertEqual(status, 409)
        self.assertIn("cab", payload["error"])

    def test_remote_take_while_cab_holds_is_conflict(self) -> None:
        dispatch(self.console, "POST", "/api/control/take", {"station": "cab"})
        status, payload = dispatch(self.console, "POST", "/api/control/take", {"station": "remote"})
        self.assertEqual(status, 409)
        self.assertIn("held by cab", payload["error"])

    def test_handover_end_to_end_through_http(self) -> None:
        dispatch(self.console, "POST", "/api/control/take", {"station": "cab"})
        status, payload = dispatch(self.console, "POST", "/api/control/offer", {"station": "cab"})
        self.assertEqual(status, 200)
        pending = payload["data"]["pending_handover"]
        self.assertEqual(pending["offered_by"], "cab")
        self.assertEqual(pending["offered_to"], "remote")

        # Holder remains cab while the offer is open.
        status, payload = dispatch(self.console, "GET", "/api/control/state")
        self.assertEqual(payload["data"]["holder"], "cab")

        status, payload = dispatch(self.console, "POST", "/api/control/accept", {"station": "remote"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["holder"], "remote")
        self.assertIsNone(payload["data"]["pending_handover"])

        # Now commands only flow from the remote.
        status, _ = dispatch(
            self.console, "POST", "/api/crane/command", {"source": "cab", "action": "lower"}
        )
        self.assertEqual(status, 409)
        status, _ = dispatch(
            self.console, "POST", "/api/crane/command", {"source": "remote", "action": "lower"}
        )
        self.assertEqual(status, 200)

    def test_wrong_station_cannot_accept(self) -> None:
        dispatch(self.console, "POST", "/api/control/take", {"station": "cab"})
        dispatch(self.console, "POST", "/api/control/offer", {"station": "cab"})
        status, payload = dispatch(self.console, "POST", "/api/control/accept", {"station": "cab"})
        self.assertEqual(status, 409)
        self.assertEqual(self.console.default_authority().holder_now(), "cab")

    def test_reject_and_cancel_paths(self) -> None:
        dispatch(self.console, "POST", "/api/control/take", {"station": "cab"})
        dispatch(self.console, "POST", "/api/control/offer", {"station": "cab"})
        status, payload = dispatch(self.console, "POST", "/api/control/reject", {"station": "remote"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["holder"], "cab")

        dispatch(self.console, "POST", "/api/control/offer", {"station": "cab"})
        status, payload = dispatch(self.console, "POST", "/api/control/cancel", {"station": "cab"})
        self.assertEqual(status, 200)
        self.assertIsNone(payload["data"]["pending_handover"])

        status, payload = dispatch(self.console, "POST", "/api/control/accept", {"station": "remote"})
        self.assertEqual(status, 409)

    def test_release_allows_take_without_handshake(self) -> None:
        dispatch(self.console, "POST", "/api/control/take", {"station": "cab"})
        status, _ = dispatch(self.console, "POST", "/api/control/release", {"station": "cab"})
        self.assertEqual(status, 200)
        status, payload = dispatch(self.console, "POST", "/api/control/take", {"station": "remote"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["holder"], "remote")

    def test_bad_station_and_body(self) -> None:
        status, payload = dispatch(self.console, "POST", "/api/control/take", {"station": "voice"})
        self.assertEqual(status, 400)
        self.assertIn("station", payload["error"])
        request = Request("POST", "/api/control/take", {}, {}, body_valid=False)
        status, _ = self.console.dispatch(request)
        self.assertEqual(status, 400)

    def test_estop_from_non_holder_overrides_everything(self) -> None:
        dispatch(self.console, "POST", "/api/slew/start")
        dispatch(self.console, "POST", "/api/control/take", {"station": "cab"})
        # Remote triggers estop without holding control.
        status, payload = dispatch(self.console, "POST", "/api/control/estop", {"station": "remote"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["data"]["estop"])
        self.assertEqual(payload["data"]["estop_source"], "remote")
        self.assertTrue(self.console.slew.is_stopped())

        # Even the holder cannot move until the stop is reset.
        status, payload = dispatch(
            self.console, "POST", "/api/crane/command", {"source": "cab", "action": "lower"}
        )
        self.assertEqual(status, 409)
        self.assertIn("emergency stop", payload["error"])

        status, payload = dispatch(self.console, "POST", "/api/control/reset", {"station": "cab"})
        self.assertEqual(status, 200)
        self.assertFalse(payload["data"]["estop"])
        # Holder kept command throughout the emergency.
        self.assertEqual(payload["data"]["holder"], "cab")

    def test_estop_command_from_non_holder_also_latches(self) -> None:
        dispatch(self.console, "POST", "/api/control/take", {"station": "cab"})
        status, payload = dispatch(
            self.console, "POST", "/api/crane/command", {"source": "remote", "action": ACTION_ESTOP}
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["data"]["authority"]["estop"])
        self.assertTrue(self.console.default_authority().estop_active())

    def test_crane_reset_clears_the_estop_latch(self) -> None:
        dispatch(self.console, "POST", "/api/control/take", {"station": "cab"})
        dispatch(self.console, "POST", "/api/control/estop", {"station": "remote"})
        status, _ = dispatch(self.console, "POST", "/api/crane/reset", {"source": "cab"})
        self.assertEqual(status, 200)
        self.assertFalse(self.console.default_authority().estop_active())

    def test_full_chain_of_custody_is_audited(self) -> None:
        dispatch(self.console, "POST", "/api/control/take", {"station": "cab"})
        dispatch(self.console, "POST", "/api/control/offer", {"station": "cab"})
        dispatch(self.console, "POST", "/api/control/accept", {"station": "remote"})
        dispatch(self.console, "POST", "/api/control/estop", {"station": "cab"})
        dispatch(self.console, "POST", "/api/control/reset", {"station": "remote"})
        dispatch(self.console, "POST", "/api/control/release", {"station": "remote"})
        status, payload = dispatch(self.console, "GET", "/api/audit", {})
        # GET through dispatch with empty query: query filtering via query params
        actions = [entry["action"] for entry in payload["data"]]
        for expected in (
            "control-take",
            "control-offer",
            "control-handover",
            "emergency-stop",
            "emergency-stop-reset",
            "control-release",
        ):
            self.assertIn(expected, actions, f"missing audited action {expected}")
        # The estop entry names the triggering station, assigning responsibility.
        request = Request("GET", "/api/audit", {"action": ["emergency-stop"]}, {})
        _, payload = self.console.dispatch(request)
        self.assertTrue(any("station=cab" in entry["detail"] for entry in payload["data"]))

    def test_authority_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = build_app(directory, web_root=str(WEB_ROOT))
            dispatch(first.console, "POST", "/api/control/take", {"station": "remote"})
            first.shutdown()

            second = build_app(directory, web_root=str(WEB_ROOT))
            self.assertEqual(second.console.default_authority().holder_now(), "remote")
            status, payload = dispatch(
                second.console,
                "POST",
                "/api/crane/command",
                {"source": "cab", "action": "lower"},
            )
            self.assertEqual(status, 409)
            second.shutdown()


class AuthorityConcurrencyTests(unittest.TestCase):
    def test_concurrent_takes_leave_a_single_holder(self) -> None:
        authority = auth.ControlAuthority("crane-1")
        audit = Audit(500)
        winners: list[str] = []
        lock = threading.Lock()

        def attempt(station: str) -> None:
            try:
                auth.take(authority, station, audit)
                with lock:
                    winners.append(station)
            except AuthorityHeldByOther:
                pass

        threads = [
            threading.Thread(target=attempt, args=("cab" if index % 2 == 0 else "remote",))
            for index in range(20)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        # One station becomes holder; threads that retried the same winner are
        # idempotent successes, but both stations must never win.
        self.assertTrue(winners)
        self.assertEqual(set(winners), {authority.holder_now()})


if __name__ == "__main__":
    unittest.main()
