"""Exclusive control-station arbitration between the cab and the remote.

Only one station (``cab`` or ``remote``) may command motion at a time. Taking a
free station is immediate, but moving control away from a holder is an explicit
two-party handover: the holder *offers* control and the other station *accepts*
it; either side can reject or cancel. Until the handover completes the original
station keeps full control, so an interrupted handover can never leave both
ends live.

The emergency stop is deliberately outside the arbitration: either station can
trigger it at any time, it cancels any pending handover, and while it is latched
no motion command is accepted from either end. Every state transition is
written to the audit log so the chain of custody is reconstructable.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from .audit import Audit
from .crane import SOURCE_CAB, SOURCE_REMOTE
from .errors import (
    AuthorityHeldByOther,
    AuthorityIdle,
    EmergencyStopActive,
    HandoverOfferedByOther,
    NoPendingHandover,
    StationNotHolder,
    StoreNotFound,
    UnknownStation,
)
from .store import Store

STATIONS = (SOURCE_CAB, SOURCE_REMOTE)
AUTHORITY_KEY = "authority/control"


@dataclass(frozen=True)
class Handover:
    """A control handover waiting for the other station to accept."""

    offered_by: str
    offered_to: str
    offered_at: str

    def to_dict(self) -> dict[str, str]:
        return {
            "offered_by": self.offered_by,
            "offered_to": self.offered_to,
            "offered_at": self.offered_at,
        }


class ControlAuthority:
    """Thread-safe holder, handover and emergency-stop state for one crane."""

    def __init__(self, crane_id: str) -> None:
        self._lock = threading.Lock()
        self.crane_id = crane_id
        self.holder: str | None = None
        self.estop = False
        self.estop_source: str | None = None
        self.pending: Handover | None = None
        self.generation = 0

    def holder_now(self) -> str | None:
        with self._lock:
            return self.holder

    def estop_active(self) -> bool:
        with self._lock:
            return self.estop

    def estop_source_now(self) -> str | None:
        with self._lock:
            return self.estop_source

    def pending_handover(self) -> Handover | None:
        with self._lock:
            return self.pending

    def generation_now(self) -> int:
        with self._lock:
            return self.generation

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "crane_id": self.crane_id,
                "holder": self.holder,
                "estop": self.estop,
                "estop_source": self.estop_source,
                "generation": self.generation,
                "pending": self.pending.to_dict() if self.pending else None,
            }


def _validate_station(station: str) -> str:
    if station not in STATIONS:
        raise UnknownStation(f"unknown control station: {station!r}")
    return station


def _other(station: str) -> str:
    return SOURCE_REMOTE if station == SOURCE_CAB else SOURCE_CAB


def take(authority: ControlAuthority, station: str, recorder: Audit | None = None) -> str | None:
    """Grant control to ``station`` immediately.

    Allowed while nobody holds control, when the same station already holds it,
    or when the current holder has offered control away (a take by the offered
    station is the accept side of that handshake). Control held by a third
    station without an offer cannot be stolen.
    """

    _validate_station(station)
    with authority._lock:  # noqa: SLF001 - module functions own the state
        pending = authority.pending
        if authority.holder is not None:
            if authority.holder == station:
                return authority.holder
            if pending is None or pending.offered_to != station:
                raise AuthorityHeldByOther(f"control held by {authority.holder}")
        authority.pending = None
        previous = authority.holder
        authority.holder = station
        authority.generation += 1
        generation = authority.generation
    if recorder is not None:
        detail = "idle" if previous is None else f"from={previous}"
        recorder.record(authority.crane_id, "control-take", f"station={station} {detail} gen={generation}")
    return previous


def offer(authority: ControlAuthority, station: str, recorder: Audit | None = None) -> Handover:
    """The current holder offers control to the other station."""

    _validate_station(station)
    with authority._lock:  # noqa: SLF001
        if authority.holder is None:
            raise AuthorityIdle("no station currently holds control")
        if authority.holder != station:
            raise StationNotHolder(f"control held by {authority.holder}")
        target = _other(station)
        handover = Handover(
            offered_by=station,
            offered_to=target,
            offered_at=datetime.now(timezone.utc).isoformat(),
        )
        authority.pending = handover
        pending = handover
    if recorder is not None:
        recorder.record(
            authority.crane_id,
            "control-offer",
            f"station={station} target={target}",
        )
    return pending


def accept_handover(
    authority: ControlAuthority, station: str, recorder: Audit | None = None
) -> str:
    """The offered-to station accepts; control moves and the holder loses it."""

    _validate_station(station)
    with authority._lock:  # noqa: SLF001
        handover = authority.pending
        if handover is None:
            raise NoPendingHandover("no handover waiting to be accepted")
        if handover.offered_to != station:
            raise HandoverOfferedByOther(f"handover was offered to {handover.offered_to}")
        previous = authority.holder
        authority.holder = station
        authority.pending = None
        authority.generation += 1
        generation = authority.generation
    if recorder is not None:
        recorder.record(
            authority.crane_id,
            "control-handover",
            f"from={previous} to={station} gen={generation}",
        )
    return previous or ""


def reject_handover(
    authority: ControlAuthority, station: str, recorder: Audit | None = None
) -> str:
    """The offered-to station declines; the offering station keeps control."""

    _validate_station(station)
    with authority._lock:  # noqa: SLF001
        handover = authority.pending
        if handover is None:
            raise NoPendingHandover("no handover waiting to be rejected")
        if handover.offered_to != station:
            raise HandoverOfferedByOther(f"handover was offered to {handover.offered_to}")
        offered_by = handover.offered_by
        authority.pending = None
    if recorder is not None:
        recorder.record(
            authority.crane_id,
            "control-handover-reject",
            f"by={station} offered_by={offered_by}",
        )
    return offered_by


def cancel_handover(
    authority: ControlAuthority, station: str, recorder: Audit | None = None
) -> None:
    """Withdraw a handover. Only the offering holder or the offered-to station
    may cancel it; either way the holder keeps control."""

    _validate_station(station)
    with authority._lock:  # noqa: SLF001
        handover = authority.pending
        if handover is None:
            raise NoPendingHandover("no handover waiting to be cancelled")
        if station not in (handover.offered_by, handover.offered_to):
            raise HandoverOfferedByOther("station is not part of the handover")
        authority.pending = None
    if recorder is not None:
        recorder.record(
            authority.crane_id,
            "control-handover-cancel",
            f"by={station} offered_by={handover.offered_by}",
        )


def release(authority: ControlAuthority, station: str, recorder: Audit | None = None) -> None:
    """The holder gives control back; the station becomes idle (no holder).

    A pending handover is withdrawn first, so release also serves as the
    holder's way of backing out of a half-finished handover.
    """

    _validate_station(station)
    with authority._lock:  # noqa: SLF001
        if authority.holder is not None and authority.holder != station:
            raise StationNotHolder(f"control held by {authority.holder}")
        previous = authority.holder
        authority.holder = None
        authority.pending = None
        authority.generation += 1
        generation = authority.generation
    if recorder is not None:
        recorder.record(
            authority.crane_id,
            "control-release",
            f"station={station if previous else 'idle'} gen={generation}",
        )


def trigger_estop(
    authority: ControlAuthority, station: str, recorder: Audit | None = None
) -> bool:
    """Latch the emergency stop from either station.

    Returns ``True`` when this call latched a previously clear stop. Any pending
    handover is cancelled so a stale offer cannot be accepted after the stop.
    """

    _validate_station(station)
    with authority._lock:  # noqa: SLF001
        already = authority.estop
        authority.estop = True
        authority.estop_source = station
        had_pending = authority.pending is not None
        authority.pending = None
        authority.generation += 1
        generation = authority.generation
    if recorder is not None:
        recorder.record(
            authority.crane_id,
            "emergency-stop",
            f"station={station} already={str(already).lower()} pending_cancelled={str(had_pending).lower()} gen={generation}",
        )
    return not already


def clear_estop(
    authority: ControlAuthority, station: str, recorder: Audit | None = None
) -> None:
    """Release the emergency-stop latch. Control stays with its previous holder."""

    _validate_station(station)
    with authority._lock:  # noqa: SLF001
        if not authority.estop:
            return
        authority.estop = False
        authority.estop_source = None
        authority.generation += 1
        generation = authority.generation
    if recorder is not None:
        recorder.record(
            authority.crane_id,
            "emergency-stop-reset",
            f"station={station} gen={generation}",
        )


def admit(authority: ControlAuthority, station: str, action: str) -> None:
    """Gate a motion command: emergency stop first, then the active holder.

    Emergency-stop commands are never gated -- they must be honoured regardless
    of who holds control.
    """

    _validate_station(station)
    with authority._lock:  # noqa: SLF001
        estop = authority.estop
        holder = authority.holder
    if estop:
        raise EmergencyStopActive("emergency stop is active; reset required")
    if holder is None:
        raise StationNotHolder("no station currently holds control")
    if holder != station:
        raise StationNotHolder(f"control held by {holder}")


def persist(authority: ControlAuthority, store: Store) -> None:
    """Persist holder and estop state. A pending handover is intentionally
    transient and is not written, so a restart always resumes a clean chain of
    custody. The in-memory state is rolled back if the write fails.
    """

    with authority._lock:  # noqa: SLF001
        snapshot = {
            "crane_id": authority.crane_id,
            "holder": authority.holder,
            "estop": authority.estop,
            "estop_source": authority.estop_source,
            "generation": authority.generation,
        }
        payload = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
    store.save(_authority_key(authority.crane_id), payload)


def restore(authority: ControlAuthority, store: Store) -> None:
    """Reload persisted holder/estop state on boot."""

    try:
        payload = store.load(_authority_key(authority.crane_id))
    except StoreNotFound:
        return
    snapshot = json.loads(payload.decode("utf-8"))
    with authority._lock:  # noqa: SLF001
        holder = snapshot.get("holder")
        authority.holder = holder if holder in STATIONS else None
        authority.estop = bool(snapshot.get("estop", False))
        source = snapshot.get("estop_source")
        authority.estop_source = source if source in STATIONS else None
        authority.generation = int(snapshot.get("generation", 0))
        authority.pending = None


def _authority_key(crane_id: str) -> str:
    return f"{AUTHORITY_KEY}/{crane_id}"
