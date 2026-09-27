"""Unit tests for control-station arbitration (cab vs remote)."""

from __future__ import annotations

import threading
import unittest

from cranefleet.audit import Audit
from cranefleet.authority import (
    HANDSHAKE_CANCELLED,
    HANDSHAKE_DENIED,
    HANDSHAKE_GRANTED,
    HANDSHAKE_REQUESTED,
    Authority,
    acquire,
    deny_handover,
    emergency_stop,
    ensure_in_control,
    grant_handover,
    release,
    request_handover,
    reset,
)
from cranefleet.errors import (
    AuthorityHeldElsewhere,
    EmergencyStopActive,
    HandoverPending,
    NotHolder,
    NotRequester,
    UnknownStation,
)


class AuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.authority = Authority("crane-1")
        self.audit = Audit(100)

    def test_first_station_acquires_idle_token(self) -> None:
        entry = acquire(self.authority, "cab", self.audit)
        self.assertEqual(entry.state, HANDSHAKE_GRANTED)
        self.assertEqual(self.authority.holder_now(), "cab")
        self.assertTrue(self.authority.controls("cab"))
        self.assertFalse(self.authority.controls("remote"))

    def test_second_acquire_is_rejected(self) -> None:
        acquire(self.authority, "cab")
        with self.assertRaises(AuthorityHeldElsewhere):
            acquire(self.authority, "remote")
        self.assertEqual(self.authority.holder_now(), "cab")

    def test_reacquire_by_holder_is_idempotent(self) -> None:
        acquire(self.authority, "cab")
        acquire(self.authority, "cab")
        self.assertEqual(self.authority.handshake_sequence(), 1)

    def test_unknown_station_rejected(self) -> None:
        with self.assertRaises(UnknownStation):
            acquire(self.authority, "footpedal")
        with self.assertRaises(UnknownStation):
            ensure_in_control(self.authority, "footpedal")
        with self.assertRaises(AuthorityHeldElsewhere):
            ensure_in_control(self.authority, "cab")  # nobody holds yet
        acquire(self.authority, "cab")
        ensure_in_control(self.authority, "cab")
        with self.assertRaises(AuthorityHeldElsewhere):
            ensure_in_control(self.authority, "remote")

    def test_explicit_request_grant_hands_over(self) -> None:
        acquire(self.authority, "cab")
        request = request_handover(self.authority, "remote", self.audit)
        self.assertEqual(request.state, HANDSHAKE_REQUESTED)
        self.assertEqual(self.authority.pending_now().requester, "remote")
        with self.assertRaises(HandoverPending):
            request_handover(self.authority, "remote")
        with self.assertRaises(NotHolder):
            grant_handover(self.authority, "remote")
        grant = grant_handover(self.authority, "cab", self.audit)
        self.assertEqual(grant.state, HANDSHAKE_GRANTED)
        self.assertEqual(grant.handshake_id, request.handshake_id)
        self.assertEqual(self.authority.holder_now(), "remote")
        self.assertIsNone(self.authority.pending_now())

    def test_request_without_holder_points_to_acquire(self) -> None:
        with self.assertRaises(AuthorityHeldElsewhere):
            request_handover(self.authority, "remote")

    def test_holder_request_is_noop(self) -> None:
        acquire(self.authority, "cab")
        entry = request_handover(self.authority, "cab")
        self.assertEqual(entry.state, HANDSHAKE_GRANTED)
        self.assertIsNone(self.authority.pending_now())

    def test_deny_keeps_control_with_holder(self) -> None:
        acquire(self.authority, "cab")
        request_handover(self.authority, "remote")
        with self.assertRaises(NotHolder):
            deny_handover(self.authority, "remote")
        denied = deny_handover(self.authority, "cab", "load swinging")
        self.assertEqual(denied.state, HANDSHAKE_DENIED)
        self.assertEqual(denied.reason, "load swinging")
        self.assertEqual(self.authority.holder_now(), "cab")
        self.assertIsNone(self.authority.pending_now())
        with self.assertRaises(NotRequester):
            grant_handover(self.authority, "cab")
        # A fresh request is possible after the denial.
        request_handover(self.authority, "remote")
        self.assertIsNotNone(self.authority.pending_now())

    def test_release_revokes_authority(self) -> None:
        acquire(self.authority, "cab")
        entry = release(self.authority, "cab", self.audit)
        self.assertEqual(entry.state, HANDSHAKE_CANCELLED)
        self.assertEqual(self.authority.holder_now(), "")
        with self.assertRaises(AuthorityHeldElsewhere):
            ensure_in_control(self.authority, "cab")
        with self.assertRaises(NotHolder):
            release(self.authority, "remote")

    def test_requester_release_cancels_pending(self) -> None:
        acquire(self.authority, "cab")
        request_handover(self.authority, "remote")
        release(self.authority, "remote")
        self.assertIsNone(self.authority.pending_now())
        self.assertEqual(self.authority.holder_now(), "cab")

    def test_emergency_stop_works_without_authority(self) -> None:
        acquire(self.authority, "cab")
        request_handover(self.authority, "remote")
        emergency_stop(self.authority, "remote", self.audit)
        self.assertTrue(self.authority.is_estopped())
        self.assertEqual(self.authority.estopped_by, "remote")
        self.assertEqual(self.authority.holder_now(), "")
        self.assertIsNone(self.authority.pending_now())
        with self.assertRaises(EmergencyStopActive):
            ensure_in_control(self.authority, "cab")
        with self.assertRaises(EmergencyStopActive):
            acquire(self.authority, "cab")
        with self.assertRaises(EmergencyStopActive):
            request_handover(self.authority, "remote")
        with self.assertRaises(EmergencyStopActive):
            release(self.authority, "cab")
        # Repeated e-stop keeps the first source and does not re-log.
        count = self.audit.count_by_action("emergency-stop")
        emergency_stop(self.authority, "cab")
        self.assertEqual(self.authority.estopped_by, "remote")
        self.assertEqual(self.audit.count_by_action("emergency-stop"), count)

    def test_emergency_stop_from_neutral_state(self) -> None:
        emergency_stop(self.authority, "cab", self.audit)
        self.assertTrue(self.authority.is_estopped())
        reset(self.authority, self.audit)
        self.assertFalse(self.authority.is_estopped())
        self.assertEqual(self.authority.holder_now(), "")
        acquire(self.authority, "remote")
        self.assertTrue(self.authority.controls("remote"))

    def test_reset_without_latch_does_not_log(self) -> None:
        reset(self.authority, self.audit)
        self.assertEqual(self.audit.count_by_action("emergency-reset"), 0)

    def test_ledger_records_every_exchange_in_order(self) -> None:
        acquire(self.authority, "cab")
        request_handover(self.authority, "remote")
        grant_handover(self.authority, "cab")
        request_handover(self.authority, "cab")
        deny_handover(self.authority, "remote", "busy")
        release(self.authority, "remote")
        states = [entry.state for entry in self.authority.ledger_snapshot()]
        self.assertEqual(
            states,
            [
                HANDSHAKE_GRANTED,
                HANDSHAKE_REQUESTED,
                HANDSHAKE_GRANTED,
                HANDSHAKE_REQUESTED,
                HANDSHAKE_DENIED,
                HANDSHAKE_CANCELLED,
            ],
        )

    def test_audit_trail_distinguishes_actions(self) -> None:
        acquire(self.authority, "cab", self.audit)
        request_handover(self.authority, "remote", self.audit)
        grant_handover(self.authority, "cab", self.audit)
        emergency_stop(self.authority, "remote", self.audit)
        actions = {entry.action for entry in self.audit.snapshot()}
        self.assertIn("authority-acquire", actions)
        self.assertIn("authority-handover-request", actions)
        self.assertIn("authority-handover-grant", actions)
        self.assertIn("emergency-stop", actions)

    def test_concurrent_acquires_leave_one_holder(self) -> None:
        outcomes: list[str] = []

        def try_acquire(station: str) -> None:
            try:
                acquire(self.authority, station)
                outcomes.append(station)
            except AuthorityHeldElsewhere:
                outcomes.append("rejected")

        threads = [
            threading.Thread(target=try_acquire, args=("cab",)),
            threading.Thread(target=try_acquire, args=("remote",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(sorted(outcomes), ["cab", "rejected"])
        self.assertIn(self.authority.holder_now(), ("cab", "remote"))

    def test_concurrent_requests_open_one_pending_handshake(self) -> None:
        acquire(self.authority, "cab")
        errors: list[Exception] = []

        def ask() -> None:
            try:
                request_handover(self.authority, "remote")
            except Exception as exc:  # pragma: no cover - surfaced by assertion
                errors.append(exc)

        threads = [threading.Thread(target=ask) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        # One request lands; the rest see the pending handshake.
        self.assertTrue(all(isinstance(exc, HandoverPending) for exc in errors))
        self.assertEqual(len(errors), 5)
        self.assertIsNotNone(self.authority.pending_now())


if __name__ == "__main__":
    unittest.main()
