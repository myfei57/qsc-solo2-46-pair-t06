"""Control-station arbitration: one active station per crane.

The cab and the radio remote share one machine, but at any instant only one of
them may command it. Authority is a token held by at most one station. Moving
the token is deliberate:

* ``acquire`` takes the token when nobody holds it.
* ``request_handover`` lets the idle station ask the holder; the request waits
  until the holder calls ``grant_handover`` or ``deny_handover``.
* ``release`` puts the token back on the table, leaving no active station.

The emergency stop is the one exception: it never needs authority. Firing it
latches the machine, removes whichever station held the token and cancels any
pending handover. Motion stays refused until ``reset`` clears the latch and a
station acquires authority again.

Every outcome (grant, release, request, denial, rejection and e-stop) is
recorded in a per-crane ledger with a sequence number, so a later investigation
can reconstruct who held control and who asked for it.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass

from .audit import Audit
from .errors import (
    AuthorityHeldElsewhere,
    EmergencyStopActive,
    HandoverPending,
    NotHolder,
    NotRequester,
    UnknownStation,
)

SOURCE_CAB = "cab"
SOURCE_REMOTE = "remote"
STATIONS = (SOURCE_CAB, SOURCE_REMOTE)

# Handover ledger states.
HANDSHAKE_REQUESTED = "requested"
HANDSHAKE_GRANTED = "granted"
HANDSHAKE_DENIED = "denied"
HANDSHAKE_CANCELLED = "cancelled"


def other(station: str) -> str:
    """Return the station opposite ``station``."""

    if station == SOURCE_CAB:
        return SOURCE_REMOTE
    if station == SOURCE_REMOTE:
        return SOURCE_CAB
    raise UnknownStation(f"unknown station: {station!r}")


def validate_station(station: str) -> str:
    if station not in STATIONS:
        raise UnknownStation(f"unknown station: {station!r}")
    return station


@dataclass(frozen=True)
class Handshake:
    """One explicit handover exchange between the two stations."""

    handshake_id: str
    crane_id: str
    requester: str
    holder: str
    state: str
    reason: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.handshake_id,
            "crane_id": self.crane_id,
            "requester": self.requester,
            "holder": self.holder,
            "state": self.state,
            "reason": self.reason,
        }


class Authority:
    """The single control token for one crane, with its handover ledger."""

    def __init__(self, crane_id: str) -> None:
        self._lock = threading.Lock()
        self.crane_id = crane_id
        self.holder = ""
        self.pending: Handshake | None = None
        self.estopped = False
        self.estopped_by = ""
        self.sequence = 0
        self.ledger: list[Handshake] = []

    # ------------------------------------------------------------- snapshots

    def holder_now(self) -> str:
        with self._lock:
            return self.holder

    def is_estopped(self) -> bool:
        with self._lock:
            return self.estopped

    def pending_now(self) -> Handshake | None:
        with self._lock:
            return self.pending

    def handshake_sequence(self) -> int:
        with self._lock:
            return self.sequence

    def ledger_snapshot(self) -> list[Handshake]:
        with self._lock:
            return list(self.ledger)

    def controls(self, station: str) -> bool:
        """True when ``station`` currently holds the token (no e-stop)."""

        with self._lock:
            return not self.estopped and self.holder == station

    def state_snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "crane_id": self.crane_id,
                "holder": self.holder,
                "active": bool(self.holder) and not self.estopped,
                "estopped": self.estopped,
                "estopped_by": self.estopped_by,
                "sequence": self.sequence,
                "pending": self.pending.to_dict() if self.pending is not None else None,
                "ledger": [entry.to_dict() for entry in self.ledger],
            }

    # ----------------------------------------------------------- internals

    def _append(self, entry: Handshake) -> None:
        self.sequence += 1
        self.ledger.append(entry)


def acquire(authority: Authority, station: str, recorder: Audit | None = None) -> Handshake:
    """Take the token when no station holds it and no e-stop is latched."""

    station = validate_station(station)
    with authority._lock:  # noqa: SLF001 - module functions share authority state
        if authority.estopped:
            raise EmergencyStopActive("emergency stop is active; reset before acquiring control")
        if authority.holder == station:
            # Idempotent re-acquire: nothing changes, nothing is logged.
            return _latest(authority, station, HANDSHAKE_GRANTED)
        if authority.holder:
            raise AuthorityHeldElsewhere(f"control held by {authority.holder}")
        if authority.pending is not None:
            raise HandoverPending("a handover request is already pending")
        authority.holder = station
        entry = Handshake(
            handshake_id=uuid.uuid4().hex,
            crane_id=authority.crane_id,
            requester=station,
            holder="",
            state=HANDSHAKE_GRANTED,
            reason="acquire-idle",
        )
        authority._append(entry)
    if recorder is not None:
        recorder.record(
            authority.crane_id,
            "authority-acquire",
            f"station={station}",
        )
    return entry


def request_handover(
    authority: Authority, station: str, recorder: Audit | None = None
) -> Handshake:
    """Ask the current holder to hand control over.

    A holder asking while it already controls is a no-op error-free refresh:
    nothing is requested. The idle station is the only meaningful requester.
    """

    station = validate_station(station)
    with authority._lock:
        if authority.estopped:
            raise EmergencyStopActive("emergency stop is active; handover refused")
        if authority.holder == station:
            return _latest(authority, station, HANDSHAKE_GRANTED)
        if not authority.holder:
            # Nobody holds it: take it directly instead of opening a handshake.
            raise AuthorityHeldElsewhere("no station holds control; acquire instead of handover")
        if authority.pending is not None:
            raise HandoverPending(f"pending handover requested by {authority.pending.requester}")
        entry = Handshake(
            handshake_id=uuid.uuid4().hex,
            crane_id=authority.crane_id,
            requester=station,
            holder=authority.holder,
            state=HANDSHAKE_REQUESTED,
        )
        authority._append(entry)
        authority.pending = entry
    if recorder is not None:
        recorder.record(
            authority.crane_id,
            "authority-handover-request",
            f"from={entry.holder} to={station}",
        )
    return entry


def grant_handover(
    authority: Authority, station: str, recorder: Audit | None = None
) -> Handshake:
    """The current holder approves the pending request and gives up control."""

    station = validate_station(station)
    with authority._lock:
        pending = authority.pending
        if authority.estopped:
            raise EmergencyStopActive("emergency stop is active; handover refused")
        if pending is None:
            raise NotRequester("no pending handover request")
        if station != pending.holder:
            raise NotHolder(f"only {pending.holder} may grant this handover")
        granted = Handshake(
            handshake_id=pending.handshake_id,
            crane_id=authority.crane_id,
            requester=pending.requester,
            holder=pending.holder,
            state=HANDSHAKE_GRANTED,
            reason="explicit-grant",
        )
        authority._append(granted)
        authority.pending = None
        authority.holder = pending.requester
    if recorder is not None:
        recorder.record(
            authority.crane_id,
            "authority-handover-grant",
            f"from={station} to={granted.requester}",
        )
    return granted


def deny_handover(
    authority: Authority, station: str, reason: str = "", recorder: Audit | None = None
) -> Handshake:
    """The current holder refuses the pending request and keeps control."""

    station = validate_station(station)
    with authority._lock:
        pending = authority.pending
        if pending is None:
            raise NotRequester("no pending handover request")
        if station != pending.holder:
            raise NotHolder(f"only {pending.holder} may deny this handover")
        denied = Handshake(
            handshake_id=pending.handshake_id,
            crane_id=authority.crane_id,
            requester=pending.requester,
            holder=pending.holder,
            state=HANDSHAKE_DENIED,
            reason=reason or "refused",
        )
        authority._append(denied)
        authority.pending = None
        holder = authority.holder
    if recorder is not None:
        recorder.record(
            authority.crane_id,
            "authority-handover-deny",
            f"from={holder} to={denied.requester} reason={denied.reason}",
        )
    return denied


def release(
    authority: Authority, station: str, recorder: Audit | None = None
) -> Handshake:
    """The holder returns the token; afterwards no station can command."""

    station = validate_station(station)
    with authority._lock:
        if authority.estopped:
            # E-stop already removed the token; report it without rewriting it.
            raise EmergencyStopActive("emergency stop is active; control already released")
        if authority.holder != station:
            if authority.pending is not None and station == authority.pending.requester:
                cancelled = _cancel_pending(authority, station, "requester-released")
                return cancelled
            raise NotHolder("only the holding station may release control")
        previous = station
        reason = "explicit-release"
        if authority.pending is not None:
            reason = "released-with-pending-request"
        authority.pending = None
        authority.holder = ""
        entry = Handshake(
            handshake_id=uuid.uuid4().hex,
            crane_id=authority.crane_id,
            requester=station,
            holder=previous,
            state=HANDSHAKE_CANCELLED,
            reason=reason,
        )
        authority._append(entry)
    if recorder is not None:
        recorder.record(
            authority.crane_id,
            "authority-release",
            f"station={previous} reason={reason}",
        )
    return entry


def emergency_stop(
    authority: Authority, station: str, recorder: Audit | None = None
) -> None:
    """Latch the machine and strip control from whichever station held it.

    Works from either station regardless of who holds the token. Repeated
    presses while already latched are idempotent and keep the first source.
    """

    station = validate_station(station)
    with authority._lock:
        previous_holder = authority.holder
        was_pending = authority.pending
        if authority.estopped:
            return
        authority.estopped = True
        authority.estopped_by = station
        authority.holder = ""
        authority.pending = None
        if was_pending is not None:
            authority._append(
                Handshake(
                    handshake_id=was_pending.handshake_id,
                    crane_id=authority.crane_id,
                    requester=was_pending.requester,
                    holder=was_pending.holder,
                    state=HANDSHAKE_CANCELLED,
                    reason="emergency-stop",
                )
            )
    if recorder is not None:
        recorder.record(
            authority.crane_id,
            "emergency-stop",
            f"station={station} previous_holder={previous_holder or 'none'}",
        )


def reset(authority: Authority, recorder: Audit | None = None) -> None:
    """Clear the e-stop latch. No station holds control afterwards."""

    with authority._lock:
        was_latched = authority.estopped
        authority.estopped = False
        authority.estopped_by = ""
        authority.holder = ""
        authority.pending = None
    if recorder is not None and was_latched:
        recorder.record(
            authority.crane_id,
            "emergency-reset",
            "latch cleared; control must be re-acquired",
        )


def ensure_in_control(authority: Authority, station: str) -> None:
    """Gate used before motion commands."""

    station = validate_station(station)
    with authority._lock:
        if authority.estopped:
            raise EmergencyStopActive("emergency stop is active")
        if authority.holder != station:
            if not authority.holder:
                raise AuthorityHeldElsewhere("no station holds control; acquire first")
            raise AuthorityHeldElsewhere(f"control held by {authority.holder}")


def _latest(authority: Authority, station: str, state: str) -> Handshake:
    for entry in reversed(authority.ledger):
        if entry.state == state and (entry.requester == station or entry.holder == station):
            return entry
    return Handshake(
        handshake_id=uuid.uuid4().hex,
        crane_id=authority.crane_id,
        requester=station,
        holder="",
        state=state,
        reason="already-holder",
    )


def _cancel_pending(authority: Authority, station: str, reason: str) -> Handshake:
    pending = authority.pending
    assert pending is not None
    cancelled = Handshake(
        handshake_id=pending.handshake_id,
        crane_id=authority.crane_id,
        requester=pending.requester,
        holder=pending.holder,
        state=HANDSHAKE_CANCELLED,
        reason=reason,
    )
    authority._append(cancelled)
    authority.pending = None
    return cancelled
