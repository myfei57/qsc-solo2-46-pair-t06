"""The control console: wiring, routing and request handling.

Every HTTP route funnels into one method here so the interlocks stay in a
single place. Handlers return ``(status_code, envelope)`` where the envelope
is the ``{"ok": ...}`` document served to the console frontend.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from . import cluster as fleet
from . import config as site_config
from . import crane as crane_module
from . import hoist as hoist_module
from . import limit as limit_module
from . import slew as slew_module
from . import trolley as trolley_module
from . import views
from . import wind as wind_module
from . import zone as zone_module
from . import authority as authority_module
from .audit import Audit
from .authority import Authority
from .cluster import Cluster
from .config import ConfigError, SiteConfig
from .crane import Crane
from .errors import (
    AuthorityHeldElsewhere,
    CraneFleetError,
    EmergencyStopActive,
    FeedbackNotConfirmed,
    HandoverPending,
    HoistBusy,
    HookNotLowered,
    NotHolder,
    NotRequester,
    SlewBlocked,
    SlewBusy,
    SlewIdle,
    SlewStopNotAcknowledged,
    StoreError,
    UnknownStation,
    ZoneNotDurable,
)
from .hoist import Drum, Hoist
from .limit import Limit
from .slew import Ramp, Slew, SpeedFeedback
from .store import Store
from .trolley import Speedometer, Trolley
from .wind import Gust, Wind
from .zone import ZoneState

Handler = Callable[["Request"], tuple[int, dict[str, Any]]]

LOGGER = logging.getLogger("cranefleet.console")


@dataclass(frozen=True)
class Request:
    """A parsed HTTP request handed to a console handler."""

    method: str
    path: str
    query: dict[str, list[str]] = field(default_factory=dict)
    body: dict[str, Any] = field(default_factory=dict)
    body_valid: bool = True

    def query_first(self, name: str, default: str | None = None) -> str | None:
        values = self.query.get(name)
        if not values:
            return default
        return values[0]

    def query_all(self, name: str) -> list[str]:
        return list(self.query.get(name, []))


def ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def fail(message: str) -> dict[str, Any]:
    return {"ok": False, "error": message}


class Console:
    """Owns every control component and serves the console API."""

    def __init__(
        self,
        store: Store,
        cluster: Cluster,
        zone: ZoneState,
        slew: Slew,
        trolley: Trolley,
        hoist: Hoist,
        wind: Wind,
        limit: Limit,
        audit: Audit,
        cranes: dict[str, Crane],
        authorities: dict[str, Authority],
        feedback: SpeedFeedback,
        config: SiteConfig,
        web_root: str = "web",
    ) -> None:
        self.store = store
        self.cluster = cluster
        self.zone = zone
        self.slew = slew
        self.trolley = trolley
        self.hoist = hoist
        self.wind = wind
        self.limit = limit
        self.audit = audit
        self.cranes = cranes
        self.authorities = authorities
        self.feedback = feedback
        self.config = config
        self.web_root = web_root
        self.ramp = Ramp()
        self.drum = Drum(0.8)
        self.speedometer = Speedometer()
        self.gust = Gust()
        self.load = crane_module.Load(2.4)
        self._routes: dict[tuple[str, str], Handler] = {
            ("GET", "/api/state"): self.handle_state,
            ("GET", "/api/zone/state"): self.handle_zone_state,
            ("POST", "/api/zone/occupy"): self.handle_zone_occupy,
            ("POST", "/api/zone/vacate"): self.handle_zone_vacate,
            ("POST", "/api/zone/clear"): self.handle_zone_clear,
            ("GET", "/api/cluster/survey"): self.handle_cluster_survey,
            ("POST", "/api/cluster/survey"): self.handle_cluster_survey_post,
            ("POST", "/api/slew/start"): self.handle_slew_start,
            ("POST", "/api/slew/stop"): self.handle_slew_stop,
            ("POST", "/api/slew/target"): self.handle_slew_target,
            ("POST", "/api/slew/fault"): self.handle_slew_fault,
            ("POST", "/api/wind/alarm"): self.handle_wind_alarm,
            ("POST", "/api/wind/clear"): self.handle_wind_clear,
            ("POST", "/api/crane/command"): self.handle_crane_command,
            ("POST", "/api/crane/estop"): self.handle_crane_estop,
            ("GET", "/api/crane/state"): self.handle_crane_state,
            ("GET", "/api/crane/load"): self.handle_crane_load,
            ("POST", "/api/crane/reset"): self.handle_crane_reset,
            ("GET", "/api/authority/state"): self.handle_authority_state,
            ("POST", "/api/authority/acquire"): self.handle_authority_acquire,
            ("POST", "/api/authority/request"): self.handle_authority_request,
            ("POST", "/api/authority/grant"): self.handle_authority_grant,
            ("POST", "/api/authority/deny"): self.handle_authority_deny,
            ("POST", "/api/authority/release"): self.handle_authority_release,
            ("GET", "/api/authority/ledger"): self.handle_authority_ledger,
            ("GET", "/api/trolley/state"): self.handle_trolley_state,
            ("POST", "/api/trolley/resume"): self.handle_trolley_resume,
            ("GET", "/api/hoist/state"): self.handle_hoist_state,
            ("POST", "/api/hoist/payout"): self.handle_hoist_payout,
            ("POST", "/api/hoist/feedback"): self.handle_hoist_feedback,
            ("POST", "/api/hoist/release"): self.handle_hoist_release,
            ("POST", "/api/hoist/stop"): self.handle_hoist_stop,
            ("GET", "/api/limit/state"): self.handle_limit_state,
            ("POST", "/api/limit/check"): self.handle_limit_check,
            ("GET", "/api/config"): self.handle_config,
            ("POST", "/api/config"): self.handle_config_save,
            ("GET", "/api/audit"): self.handle_audit,
        }

    @property
    def routes(self) -> dict[tuple[str, str], Handler]:
        return self._routes

    def default_crane(self) -> Crane | None:
        return self.cranes.get("crane-1")

    def crane(self, crane_id: str) -> Crane | None:
        if not crane_id:
            return self.default_crane()
        return self.cranes.get(crane_id)

    def authority(self, crane_id: str) -> Authority | None:
        if not crane_id:
            return self.authorities.get("crane-1")
        return self.authorities.get(crane_id)

    def dispatch(self, request: Request) -> tuple[int, dict[str, Any]]:
        handler = self._routes.get((request.method, request.path))
        if handler is None:
            return 404, fail("not found")
        return handler(request)

    # ------------------------------------------------------------------ state

    def handle_state(self, request: Request) -> tuple[int, dict[str, Any]]:
        return 200, ok(views.state_view(self))

    def handle_zone_state(self, request: Request) -> tuple[int, dict[str, Any]]:
        return 200, ok(views.zone_view(self))

    def handle_zone_occupy(self, request: Request) -> tuple[int, dict[str, Any]]:
        if not request.body_valid:
            return 400, fail("bad request")
        crane_id = str(request.body.get("crane_id") or "")
        if not crane_id:
            return 400, fail("crane_id required")
        zone_module.occupy(self.zone, crane_id)
        try:
            zone_module.persist(self.zone, self.store)
        except StoreError as exc:
            return 500, fail(str(exc))
        conflicts, mismatches = fleet.check_conflicts(
            self.cluster, self.zone, self.config.safe_separation
        )
        if conflicts:
            zone_module.register_conflict(self.zone, conflicts[0].crane_ids)
            zone_module.lock_zone(self.zone)
            zone_module.persist(self.zone, self.store)
            self.audit.record(crane_id, "zone-conflict", "pair detected")
        if mismatches:
            self.audit.record(crane_id, "occupancy-reconcile", "mismatch")
        return 200, ok(
            {
                "occupants": zone_module.occupants_snapshot(self.zone),
                "conflicts": [conflict.to_dict() for conflict in conflicts],
                "mismatches": mismatches,
            }
        )

    def handle_zone_vacate(self, request: Request) -> tuple[int, dict[str, Any]]:
        if not request.body_valid:
            return 400, fail("bad request")
        crane_id = str(request.body.get("crane_id") or "")
        zone_module.vacate(self.zone, crane_id)
        try:
            zone_module.persist(self.zone, self.store)
        except StoreError as exc:
            LOGGER.debug("best-effort zone persist skipped after vacate: %s", exc)
        return 200, ok(zone_module.occupants_snapshot(self.zone))

    def handle_zone_clear(self, request: Request) -> tuple[int, dict[str, Any]]:
        generation = zone_module.clear_conflict(self.zone, self.cluster)
        for crane_id in self.cranes:
            if fleet.is_released(self.cluster, crane_id):
                slew_module.release_latch_hold(self.slew)
        try:
            zone_module.persist(self.zone, self.store)
        except StoreError as exc:
            LOGGER.debug("best-effort zone persist skipped after clear: %s", exc)
        self.audit.record("system", "zone-clear", f"{self.zone.id} generation={generation}")
        return 200, ok(views.zone_view(self))

    # ---------------------------------------------------------------- cluster

    def handle_cluster_survey(self, request: Request) -> tuple[int, dict[str, Any]]:
        return 200, ok(views.cluster_view(self))

    def handle_cluster_survey_post(self, request: Request) -> tuple[int, dict[str, Any]]:
        if not request.body_valid:
            return 400, fail("bad request")
        points = _parse_points(request.body.get("cranes"))
        if points is None:
            return 400, fail("bad request")
        fleet.survey(self.cluster, points)
        try:
            fleet.refresh_baseline(self.cluster, self.store)
        except StoreError as exc:
            return 500, fail(str(exc))
        self.audit.record("system", "cluster-survey", "baseline refreshed")
        return 200, ok(views.cluster_view(self))

    # ------------------------------------------------------------------- slew

    def handle_slew_start(self, request: Request) -> tuple[int, dict[str, Any]]:
        if zone_module.is_latched(self.zone):
            return 409, fail("zone latch held")
        try:
            slew_module.start(self.slew, self.zone, self.store, self.audit)
        except ZoneNotDurable:
            return 409, fail("zone state not durable")
        except SlewBlocked:
            return 409, fail("slew blocked by interlock")
        except SlewBusy:
            return 409, fail("slew already moving")
        except CraneFleetError as exc:
            return 409, fail(str(exc))
        return 200, ok(views.slew_view(self))

    def handle_slew_stop(self, request: Request) -> tuple[int, dict[str, Any]]:
        slew_module.stop(self.slew, self.audit)
        return 200, ok(views.slew_view(self))

    def handle_slew_target(self, request: Request) -> tuple[int, dict[str, Any]]:
        if not request.body_valid:
            return 400, fail("bad request")
        self.ramp.set_target(_number(request.body.get("angle")))
        self.ramp.step(0.1)
        return 200, ok(views.slew_view(self))

    def handle_slew_fault(self, request: Request) -> tuple[int, dict[str, Any]]:
        if not request.body_valid:
            return 400, fail("bad request")
        faulted = bool(request.body.get("faulted", True))
        slew_module.set_fault(self.slew, faulted)
        self.audit.record(self.slew.id, "slew-fault", f"faulted={str(faulted).lower()}")
        return 200, ok(views.slew_view(self))

    # ------------------------------------------------------------------- wind

    def handle_wind_alarm(self, request: Request) -> tuple[int, dict[str, Any]]:
        if not request.body_valid:
            return 400, fail("bad request")
        level = _number(request.body.get("level"))
        wind_module.set_level(self.wind, level)
        self.gust.observe(level)
        wind_module.latch_limits(self.wind, self.limit)
        try:
            wind_module.alarm(self.wind, self.slew, self.hoist, self.audit)
        except SlewStopNotAcknowledged:
            return 409, fail("slew stop was not acknowledged")
        return 200, ok(views.wind_view(self))

    def handle_wind_clear(self, request: Request) -> tuple[int, dict[str, Any]]:
        wind_module.clear(self.wind, self.limit)
        slew_module.release_wind_hold(self.slew)
        return 200, ok(views.wind_view(self))

    # ------------------------------------------------------------------ crane

    def handle_crane_command(self, request: Request) -> tuple[int, dict[str, Any]]:
        if not request.body_valid:
            return 400, fail("bad request")
        crane = self.crane(str(request.body.get("crane_id") or ""))
        if crane is None:
            return 404, fail("crane not found")
        authority = self.authorities[crane.id]
        command = crane_module.Command.from_dict(request.body)
        try:
            authority_module.ensure_in_control(authority, command.source)
        except EmergencyStopActive:
            return 409, fail("emergency stop active")
        except AuthorityHeldElsewhere as exc:
            with crane._lock:  # noqa: SLF001 - console shares crane state
                crane.blocked_moves.append(command.action)
            self.audit.record(
                crane.id,
                "command-rejected",
                f"source={command.source} action={command.action} holder={authority.holder_now() or 'none'}",
            )
            return 403, fail(str(exc))
        crane_module.merge(crane, command)
        self._book_keeping(command)
        try:
            crane_module.execute(crane, self.slew, self.trolley, self.hoist, self.audit)
        except SlewIdle:
            return 409, fail("slew is idle")
        except HookNotLowered:
            return 409, fail("hook not lowered")
        except HoistBusy:
            return 409, fail("hoist busy")
        if command.action == crane_module.ACTION_LOWER:
            try:
                hoist_module.persist_hoist(self.hoist, self.store)
            except StoreError as exc:
                LOGGER.debug("best-effort hoist persist skipped after lower: %s", exc)
        return 200, ok(crane.last_command().to_dict())

    def handle_crane_estop(self, request: Request) -> tuple[int, dict[str, Any]]:
        """Emergency stop from either station; it never needs authority."""

        if not request.body_valid:
            return 400, fail("bad request")
        crane = self.crane(str(request.body.get("crane_id") or ""))
        if crane is None:
            return 404, fail("crane not found")
        authority = self.authorities[crane.id]
        source = str(request.body.get("source") or "")
        try:
            authority_module.validate_station(source)
        except UnknownStation:
            return 400, fail("source must be cab or remote")
        authority_module.emergency_stop(authority, source, self.audit)
        command = crane_module.Command(
            source=source, action=crane_module.ACTION_EMERGENCY_STOP
        )
        crane_module.merge(crane, command)
        self._freeze_motion()
        self._book_keeping(command)
        return 200, ok(views.crane_view(self))

    def _freeze_motion(self) -> None:
        """Stop every axis and hold the hoist brake (used by e-stop/handover)."""

        slew_module.stop(self.slew, self.audit)
        trolley_module.stop(self.trolley, self.audit)
        hoist_module.stop(self.hoist, self.audit)
        hoist_module.engage_brake(self.hoist)
        self.ramp.set_target(0.0)

    def _book_keeping(self, command: crane_module.Command) -> None:
        if command.action == crane_module.ACTION_SWING:
            self.ramp.set_target(command.angle)
            self.load.update(0.3, 0.1)
        elif command.action == crane_module.ACTION_LOWER:
            self.drum.pay_out(1.5)
            self.load.set_vertical(8.0)
        elif command.action == crane_module.ACTION_TROLLEY:
            self.speedometer.sample(self.trolley.position_now(), 0.5)
        elif command.action in (
            crane_module.ACTION_STOP,
            crane_module.ACTION_EMERGENCY_STOP,
        ):
            self.ramp.set_target(0.0)
            self.load.update(-0.2, 0.1)

    def handle_crane_state(self, request: Request) -> tuple[int, dict[str, Any]]:
        return 200, ok(views.crane_view(self))

    def handle_crane_load(self, request: Request) -> tuple[int, dict[str, Any]]:
        return 200, ok(views.load_view(self))

    def handle_crane_reset(self, request: Request) -> tuple[int, dict[str, Any]]:
        crane = self.default_crane()
        if crane is None:
            return 404, fail("crane not found")
        crane.clear_stop()
        crane.clear_estop()
        authority_module.reset(self.authorities[crane.id], self.audit)
        limit_module.reset_arbitration(self.limit)
        self.audit.record(crane.id, "command-reset", "stops and e-stop cleared")
        return 200, ok(crane.last_command().to_dict())

    # --------------------------------------------------------------- authority

    def _station_context(
        self, request: Request
    ) -> tuple[Crane, Authority, str] | tuple[int, dict[str, Any]]:
        if not request.body_valid:
            return 400, fail("bad request")
        crane = self.crane(str(request.body.get("crane_id") or ""))
        if crane is None:
            return 404, fail("crane not found")
        station = str(request.body.get("station") or "")
        if station not in authority_module.STATIONS:
            return 400, fail("station must be cab or remote")
        return crane, self.authorities[crane.id], station

    @staticmethod
    def _is_error(
        resolved: tuple[Crane, Authority, str] | tuple[int, dict[str, Any]],
    ) -> bool:
        return isinstance(resolved[0], int)

    def handle_authority_state(self, request: Request) -> tuple[int, dict[str, Any]]:
        return 200, ok(views.authority_view(self))

    def handle_authority_acquire(self, request: Request) -> tuple[int, dict[str, Any]]:
        resolved = self._station_context(request)
        if isinstance(resolved, tuple) and len(resolved) == 2:
            return resolved  # type: ignore[return-value]
        crane, authority, station = resolved
        try:
            authority_module.acquire(authority, station, self.audit)
        except EmergencyStopActive:
            return 409, fail("emergency stop active; reset first")
        except AuthorityHeldElsewhere as exc:
            return 409, fail(str(exc))
        except HandoverPending:
            return 409, fail("handover request pending")
        return 200, ok(views.authority_for(self, crane.id))

    def handle_authority_request(self, request: Request) -> tuple[int, dict[str, Any]]:
        resolved = self._station_context(request)
        if isinstance(resolved, tuple) and len(resolved) == 2:
            return resolved  # type: ignore[return-value]
        crane, authority, station = resolved
        try:
            entry = authority_module.request_handover(authority, station, self.audit)
        except EmergencyStopActive:
            return 409, fail("emergency stop active")
        except AuthorityHeldElsewhere as exc:
            # Nobody holds it: acquire directly for the requester.
            authority_module.acquire(authority, station, self.audit)
            return 200, ok(views.authority_for(self, crane.id))
        except HandoverPending as exc:
            return 409, fail(str(exc))
        return 200, ok(
            {"handshake": entry.to_dict(), "authority": views.authority_for(self, crane.id)}
        )

    def handle_authority_grant(self, request: Request) -> tuple[int, dict[str, Any]]:
        resolved = self._station_context(request)
        if isinstance(resolved, tuple) and len(resolved) == 2:
            return resolved  # type: ignore[return-value]
        crane, authority, station = resolved
        try:
            authority_module.grant_handover(authority, station, self.audit)
        except EmergencyStopActive:
            return 409, fail("emergency stop active")
        except (NotHolder, NotRequester) as exc:
            return 409, fail(str(exc))
        # Handing over is a zero-motion boundary: nothing keeps moving under
        # the outgoing station once the new station owns the machine.
        self._freeze_motion()
        return 200, ok(views.authority_for(self, crane.id))

    def handle_authority_deny(self, request: Request) -> tuple[int, dict[str, Any]]:
        resolved = self._station_context(request)
        if isinstance(resolved, tuple) and len(resolved) == 2:
            return resolved  # type: ignore[return-value]
        crane, authority, station = resolved
        reason = str(request.body.get("reason") or "")
        try:
            authority_module.deny_handover(authority, station, reason, self.audit)
        except (NotHolder, NotRequester) as exc:
            return 409, fail(str(exc))
        return 200, ok(views.authority_for(self, crane.id))

    def handle_authority_release(self, request: Request) -> tuple[int, dict[str, Any]]:
        resolved = self._station_context(request)
        if isinstance(resolved, tuple) and len(resolved) == 2:
            return resolved  # type: ignore[return-value]
        crane, authority, station = resolved
        try:
            authority_module.release(authority, station, self.audit)
        except EmergencyStopActive:
            return 409, fail("emergency stop active")
        except NotHolder as exc:
            return 409, fail(str(exc))
        self._freeze_motion()
        return 200, ok(views.authority_for(self, crane.id))

    def handle_authority_ledger(self, request: Request) -> tuple[int, dict[str, Any]]:
        return 200, ok(views.authority_view(self))

    # -------------------------------------------------------- trolley / hoist

    def handle_trolley_state(self, request: Request) -> tuple[int, dict[str, Any]]:
        return 200, ok(views.trolley_view(self))

    def handle_trolley_resume(self, request: Request) -> tuple[int, dict[str, Any]]:
        try:
            trolley_module.move_resume(self.trolley, self.hoist, self.store)
            hoist_module.resume_release(self.hoist, self.feedback, self.store)
        except HookNotLowered:
            return 409, fail("hook not lowered")
        except FeedbackNotConfirmed:
            return 409, fail("feedback not confirmed")
        return 200, ok(
            {"trolley": views.trolley_view(self), "hoist": views.hoist_view(self)}
        )

    def handle_hoist_state(self, request: Request) -> tuple[int, dict[str, Any]]:
        return 200, ok(views.hoist_view(self))

    def handle_hoist_payout(self, request: Request) -> tuple[int, dict[str, Any]]:
        if not request.body_valid:
            return 400, fail("bad request")
        self.drum.pay_out(_number(request.body.get("length")))
        return 200, ok(views.hoist_view(self))

    def handle_hoist_feedback(self, request: Request) -> tuple[int, dict[str, Any]]:
        if not request.body_valid:
            return 400, fail("bad request")
        self.feedback.confirm(_number(request.body.get("speed")))
        try:
            slew_module.persist_feedback(self.feedback, self.store)
        except StoreError as exc:
            return 500, fail(str(exc))
        self.audit.record(self.hoist.id, "speed-feedback", "confirmed")
        return 200, ok(views.hoist_view(self))

    def handle_hoist_release(self, request: Request) -> tuple[int, dict[str, Any]]:
        try:
            hoist_module.release_brake(self.hoist, self.feedback)
        except FeedbackNotConfirmed:
            return 409, fail("feedback not confirmed")
        try:
            slew_module.persist_feedback(self.feedback, self.store)
        except StoreError as exc:
            LOGGER.debug("best-effort feedback persist skipped after release: %s", exc)
        return 200, ok(views.hoist_view(self))

    def handle_hoist_stop(self, request: Request) -> tuple[int, dict[str, Any]]:
        hoist_module.stop(self.hoist, self.audit)
        hoist_module.engage_brake(self.hoist)
        return 200, ok(views.hoist_view(self))

    # ------------------------------------------------------------------ limit

    def handle_limit_state(self, request: Request) -> tuple[int, dict[str, Any]]:
        return 200, ok(views.limit_view(self))

    def handle_limit_check(self, request: Request) -> tuple[int, dict[str, Any]]:
        positions = fleet.current_positions(self.cluster)
        envelopes: list[limit_module.Envelope] = []
        slew_point = positions.get("crane-1")
        trolley_point = positions.get("crane-2")
        if slew_point is not None:
            envelopes.append(
                limit_module.position_envelope(
                    limit_module.AXIS_SLEW, limit_module.slew_position(slew_point.x)
                )
            )
        if trolley_point is not None:
            envelopes.append(
                limit_module.position_envelope(
                    limit_module.AXIS_TROLLEY, limit_module.trolley_position(trolley_point.y)
                )
            )
        exceeded = limit_module.enforce_envelopes(self.limit, envelopes)
        arbitration: limit_module.Arbitration | None = None
        if exceeded:
            arbitration = limit_module.arbitrate(
                self.limit, self.slew, self.trolley, self.hoist
            )
            self.audit.record("system", "limit-enforce", "axes=" + ",".join(exceeded))
        return 200, ok(
            {
                "exceeded": exceeded,
                "margins": limit_module.envelope_margins(envelopes),
                "limit": views.limit_view(self),
                "arbitration": arbitration.to_dict() if arbitration else None,
            }
        )

    # ---------------------------------------------------------------- console

    def handle_config(self, request: Request) -> tuple[int, dict[str, Any]]:
        return 200, ok(site_config.config_view(self.config, self.cluster))

    def handle_config_save(self, request: Request) -> tuple[int, dict[str, Any]]:
        if not request.body_valid:
            return 400, fail("bad request")
        try:
            updated = SiteConfig.from_dict(request.body)
        except ConfigError as exc:
            return 400, fail(str(exc))
        self.config = updated
        site_config.save_config(self.store, updated)
        self.audit.record("system", "config-save", updated.site_name)
        return 200, ok(site_config.config_view(updated, self.cluster))

    def handle_audit(self, request: Request) -> tuple[int, dict[str, Any]]:
        crane_id = request.query_first("crane") or ""
        limit = _positive_int(request.query_first("limit"), 50)
        actions = request.query_all("action")
        if actions:
            entries = self.audit.filter_by_actions(actions)[:limit]
        else:
            entries = self.audit.query(crane_id, limit)
        return 200, ok([entry.to_dict() for entry in entries])


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _positive_int(value: str | None, fallback: int) -> int:
    if value is None:
        return fallback
    try:
        parsed = int(value)
    except ValueError:
        return fallback
    return parsed if parsed > 0 else fallback


def _parse_points(payload: Any) -> dict[str, Any] | None:
    from .geometry import Point

    if not isinstance(payload, dict):
        return None
    points: dict[str, Point] = {}
    for crane_id, raw in payload.items():
        if not isinstance(raw, dict):
            return None
        try:
            points[str(crane_id)] = Point(float(raw["x"]), float(raw["y"]))
        except (KeyError, TypeError, ValueError):
            return None
    return points
