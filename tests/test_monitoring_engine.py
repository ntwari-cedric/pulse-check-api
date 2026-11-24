"""Part 2 test suite: Monitoring Engine, Alert System, Pause/Resume, Concurrency.

Testing strategy for time-sensitive scenarios
----------------------------------------------
We NEVER use real time.sleep() in tests. Instead we:
  1. Insert monitor rows with expires_at set IN THE PAST using timedelta.
  2. Call evaluate_expired_monitors(db, alert_service) directly — this runs
     one evaluation cycle synchronously using the same test DB session as
     the HTTP client, so changes are immediately visible.
  3. Assert the resulting DB state.

This means tests run in milliseconds regardless of timeout values.

Duplicate-alert prevention strategy
--------------------------------------
AlertService.send_down_alert() is a coroutine. In tests we pass a subclass
(CapturingAlertService) or AsyncMock to count calls. The count must be
exactly 1 per outage regardless of how many evaluation cycles run.

Session injection pattern
--------------------------
evaluate_expired_monitors(db, alert_service) accepts an explicit SQLAlchemy
session. We pass the same test session that the client fixture uses
(TestingSessionLocal) so both share the same in-memory database state.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Monitor, MonitorStatus
from app.services.timer_service import evaluate_expired_monitors, calculate_expiration
from app.services.alert_service import AlertService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _past(seconds: int) -> datetime:
    """Returns a UTC datetime `seconds` ago."""
    return _now() - timedelta(seconds=seconds)


def _future(seconds: int) -> datetime:
    """Returns a UTC datetime `seconds` from now."""
    return _now() + timedelta(seconds=seconds)


def _make_monitor(
    db: Session,
    monitor_id: str,
    status: MonitorStatus = MonitorStatus.ONLINE,
    timeout: int = 30,
    expires_offset_seconds: int = -5,  # negative = already expired
    alert_email: str = "admin@critmon.com",
) -> Monitor:
    """Creates and persists a Monitor with controllable expiry timing."""
    monitor = Monitor(
        id=monitor_id,
        timeout=timeout,
        alert_email=alert_email,
        status=status,
        last_heartbeat=_past(abs(expires_offset_seconds) + timeout),
        expires_at=_now() + timedelta(seconds=expires_offset_seconds),
        created_at=_past(abs(expires_offset_seconds) + timeout + 5),
    )
    db.add(monitor)
    db.commit()
    return monitor


class CapturingAlertService(AlertService):
    """AlertService subclass that records all send_down_alert calls for assertions."""

    def __init__(self):
        self.calls: list[dict] = []

    async def send_down_alert(self, monitor_id, alert_email, last_heartbeat, timeout_seconds):
        self.calls.append({
            "monitor_id": monitor_id,
            "alert_email": alert_email,
            "last_heartbeat": last_heartbeat,
            "timeout_seconds": timeout_seconds,
        })
        return True


def _run_tick(db: Session, alert_svc: AlertService | None = None) -> int:
    """Runs one watchdog evaluation cycle synchronously using the given test session."""
    if alert_svc is None:
        alert_svc = CapturingAlertService()
    return evaluate_expired_monitors(db, alert_svc)


# ==============================================================================
# §4 — User Story 3: Automatic Failure Detection (ONLINE → DOWN)
# ==============================================================================


def test_expired_monitor_transitions_to_down(db_session: Session, client: TestClient):
    """An ONLINE monitor whose expires_at is in the past is marked DOWN."""
    _make_monitor(db_session, "dev-expired", expires_offset_seconds=-10)

    # Confirm it starts ONLINE via the API
    r = client.get("/monitors/dev-expired")
    assert r.json()["status"] == "ONLINE"

    # Run one watchdog tick using the shared test DB session
    _run_tick(db_session)

    # API now reflects DOWN status
    r2 = client.get("/monitors/dev-expired")
    assert r2.status_code == 200
    assert r2.json()["status"] == "DOWN"


def test_non_expired_monitor_stays_online(db_session: Session, client: TestClient):
    """A monitor with an expiry in the future must NOT be touched by the watchdog."""
    _make_monitor(db_session, "dev-healthy", expires_offset_seconds=+300)

    _run_tick(db_session)

    r = client.get("/monitors/dev-healthy")
    assert r.json()["status"] == "ONLINE"


def test_down_alert_generated_on_expiry(db_session: Session):
    """Alert is dispatched exactly once when a monitor transitions ONLINE → DOWN."""
    _make_monitor(db_session, "dev-alert", expires_offset_seconds=-5)

    alert_svc = CapturingAlertService()
    count = _run_tick(db_session, alert_svc)

    assert count == 1
    assert len(alert_svc.calls) == 1
    assert alert_svc.calls[0]["monitor_id"] == "dev-alert"


def test_no_duplicate_alert_on_multiple_evaluation_cycles(db_session: Session):
    """Running the watchdog multiple times on an already-DOWN monitor never re-alerts."""
    _make_monitor(db_session, "dev-no-dup", expires_offset_seconds=-5)

    alert_svc = CapturingAlertService()

    # First cycle: ONLINE → DOWN, alert fires
    count1 = _run_tick(db_session, alert_svc)
    assert count1 == 1
    assert len(alert_svc.calls) == 1

    # Second cycle: monitor is DOWN, WHERE clause fails → rowcount=0
    count2 = _run_tick(db_session, alert_svc)
    assert count2 == 0

    # Third cycle: same
    count3 = _run_tick(db_session, alert_svc)
    assert count3 == 0

    # Alert was sent exactly ONCE across all three cycles
    assert len(alert_svc.calls) == 1


def test_down_status_persisted_in_database(db_session: Session):
    """DOWN status written by the watchdog survives a fresh DB query."""
    _make_monitor(db_session, "dev-persist", expires_offset_seconds=-5)

    _run_tick(db_session)

    # Expire the ORM cache and re-query to confirm persistence
    db_session.expire_all()
    monitor = db_session.query(Monitor).filter(Monitor.id == "dev-persist").first()
    assert monitor is not None
    assert monitor.status == MonitorStatus.DOWN


def test_multiple_expired_monitors_all_transition(db_session: Session):
    """All simultaneously expired ONLINE monitors are transitioned in one tick."""
    for i in range(5):
        _make_monitor(db_session, f"dev-multi-{i}", expires_offset_seconds=-10)

    alert_svc = CapturingAlertService()
    count = _run_tick(db_session, alert_svc)

    assert count == 5
    assert len(alert_svc.calls) == 5


# ==============================================================================
# §5 — Bonus User Story: Pause and Resume
# ==============================================================================


def test_pause_online_monitor(client: TestClient):
    """POST /monitors/{id}/pause sets an ONLINE monitor to PAUSED."""
    client.post(
        "/monitors",
        json={"id": "dev-pause-1", "timeout": 60, "alert_email": "admin@critmon.com"},
    )
    r = client.post("/monitors/dev-pause-1/pause")
    assert r.status_code == 200
    data = r.json()
    assert data["monitor"]["status"] == "PAUSED"
    assert "paused" in data["message"].lower()


def test_pause_idempotent(client: TestClient):
    """Pausing an already-PAUSED monitor returns 200, not an error."""
    client.post(
        "/monitors",
        json={"id": "dev-pause-idem", "timeout": 60, "alert_email": "admin@critmon.com"},
    )
    client.post("/monitors/dev-pause-idem/pause")  # First pause
    r = client.post("/monitors/dev-pause-idem/pause")  # Second pause — must be 200
    assert r.status_code == 200
    assert r.json()["monitor"]["status"] == "PAUSED"


def test_pause_nonexistent_monitor(client: TestClient):
    """POST /monitors/{id}/pause on unknown ID returns HTTP 404."""
    r = client.post("/monitors/does-not-exist/pause")
    assert r.status_code == 404


def test_pause_down_monitor_rejected(client: TestClient, db_session: Session):
    """Cannot pause a DOWN monitor — returns HTTP 409."""
    now = _now()
    db_session.add(Monitor(
        id="dev-down-pause",
        timeout=60,
        alert_email="admin@critmon.com",
        status=MonitorStatus.DOWN,
        last_heartbeat=now,
        expires_at=now,
        created_at=now,
    ))
    db_session.commit()

    r = client.post("/monitors/dev-down-pause/pause")
    assert r.status_code == 409


def test_paused_monitor_not_expired_by_watchdog(db_session: Session, client: TestClient):
    """The watchdog must NOT transition a PAUSED monitor to DOWN, even if expired."""
    # Insert a PAUSED monitor with expires_at in the past
    db_session.add(Monitor(
        id="dev-paused-safe",
        timeout=30,
        alert_email="admin@critmon.com",
        status=MonitorStatus.PAUSED,
        last_heartbeat=_past(60),
        expires_at=_past(30),  # expired!
        created_at=_past(90),
    ))
    db_session.commit()

    alert_svc = CapturingAlertService()
    count = _run_tick(db_session, alert_svc)

    # Watchdog must have done nothing to the paused monitor
    assert count == 0
    assert len(alert_svc.calls) == 0

    # Status must still be PAUSED
    r = client.get("/monitors/dev-paused-safe")
    assert r.json()["status"] == "PAUSED"


def test_no_alert_for_paused_monitor_mixed(db_session: Session):
    """Watchdog with a mix of PAUSED + expired ONLINE only alerts for the ONLINE one."""
    # Expired ONLINE — should trigger DOWN + alert
    _make_monitor(db_session, "dev-online-expired", expires_offset_seconds=-5,
                  status=MonitorStatus.ONLINE)

    # Expired PAUSED — must NOT trigger anything
    db_session.add(Monitor(
        id="dev-paused-expired",
        timeout=30,
        alert_email="admin@critmon.com",
        status=MonitorStatus.PAUSED,
        last_heartbeat=_past(60),
        expires_at=_past(30),
        created_at=_past(90),
    ))
    db_session.commit()

    alert_svc = CapturingAlertService()
    count = _run_tick(db_session, alert_svc)

    # Only the ONLINE monitor should have been transitioned
    assert count == 1
    assert len(alert_svc.calls) == 1
    assert alert_svc.calls[0]["monitor_id"] == "dev-online-expired"

    # Paused monitor stays PAUSED
    db_session.expire_all()
    paused = db_session.query(Monitor).filter(Monitor.id == "dev-paused-expired").first()
    assert paused.status == MonitorStatus.PAUSED


def test_heartbeat_resumes_paused_monitor(client: TestClient, db_session: Session):
    """A heartbeat on a PAUSED monitor resumes it to ONLINE and resets expiry."""
    db_session.add(Monitor(
        id="dev-resume",
        timeout=120,
        alert_email="admin@critmon.com",
        status=MonitorStatus.PAUSED,
        last_heartbeat=_past(200),
        expires_at=_past(80),
        created_at=_past(300),
    ))
    db_session.commit()

    r = client.post("/monitors/dev-resume/heartbeat")
    assert r.status_code == 200
    data = r.json()
    assert data["monitor"]["status"] == "ONLINE"

    # expires_at must now be in the future (≈ 120 seconds from now)
    expires = datetime.fromisoformat(data["monitor"]["expires_at"])
    assert expires > _now()
    delta = (expires - _now()).total_seconds()
    assert abs(delta - 120) < 5.0


def test_paused_then_watchdog_then_heartbeat_resumes(db_session: Session, client: TestClient):
    """Full scenario: pause → watchdog fires (should not expire) → heartbeat resumes."""
    db_session.add(Monitor(
        id="dev-full-flow",
        timeout=30,
        alert_email="admin@critmon.com",
        status=MonitorStatus.PAUSED,
        last_heartbeat=_past(50),
        expires_at=_past(20),
        created_at=_past(80),
    ))
    db_session.commit()

    # Watchdog fires — must NOT expire the PAUSED monitor
    alert_svc = CapturingAlertService()
    _run_tick(db_session, alert_svc)
    assert len(alert_svc.calls) == 0

    # Heartbeat resumes the monitor
    r = client.post("/monitors/dev-full-flow/heartbeat")
    assert r.status_code == 200
    assert r.json()["monitor"]["status"] == "ONLINE"


# ==============================================================================
# §6 — Developer's Choice Feature: GET /monitors/{id}
# ==============================================================================


def test_get_monitor_all_fields_present(client: TestClient):
    """GET /monitors/{id} returns all required fields per the spec example."""
    client.post(
        "/monitors",
        json={"id": "dev-inspect", "timeout": 60, "alert_email": "admin@critmon.com"},
    )
    r = client.get("/monitors/dev-inspect")
    assert r.status_code == 200
    data = r.json()

    for field in ("id", "timeout", "status", "last_heartbeat", "expires_at",
                  "alert_email", "created_at"):
        assert field in data, f"Missing field: {field}"

    assert data["id"] == "dev-inspect"
    assert data["timeout"] == 60
    assert data["status"] == "ONLINE"
    assert data["alert_email"] == "admin@critmon.com"


def test_get_monitor_after_expiry_shows_down(db_session: Session, client: TestClient):
    """GET /monitors/{id} reflects DOWN status after watchdog evaluation."""
    _make_monitor(db_session, "dev-get-down", expires_offset_seconds=-10)
    _run_tick(db_session)

    r = client.get("/monitors/dev-get-down")
    assert r.status_code == 200
    assert r.json()["status"] == "DOWN"


def test_get_monitor_not_found(client: TestClient):
    """GET /monitors/{id} returns 404 for unknown device."""
    r = client.get("/monitors/no-such-device")
    assert r.status_code == 404


# ==============================================================================
# §7 — Reliability & State Transitions
# ==============================================================================


def test_heartbeat_before_expiry_prevents_down(db_session: Session, client: TestClient):
    """A heartbeat resets expiry so the watchdog does NOT mark it DOWN."""
    # Monitor expiring in 5 seconds — still alive
    _make_monitor(db_session, "dev-beat-first", expires_offset_seconds=+5)

    # Send heartbeat — extends expiry by timeout=30s
    r = client.post("/monitors/dev-beat-first/heartbeat")
    assert r.status_code == 200

    # Run watchdog
    _run_tick(db_session)

    # Still ONLINE because heartbeat extended the deadline
    r2 = client.get("/monitors/dev-beat-first")
    assert r2.json()["status"] == "ONLINE"


def test_down_monitor_cannot_send_heartbeat(client: TestClient, db_session: Session):
    """DOWN monitors reject heartbeat — explicit recovery is required."""
    db_session.add(Monitor(
        id="dev-dead-hb",
        timeout=60,
        alert_email="admin@critmon.com",
        status=MonitorStatus.DOWN,
        last_heartbeat=_past(120),
        expires_at=_past(60),
        created_at=_past(200),
    ))
    db_session.commit()

    r = client.post("/monitors/dev-dead-hb/heartbeat")
    assert r.status_code == 409
    assert "DOWN" in r.json()["detail"]


def test_watchdog_skips_already_down_monitors(db_session: Session):
    """Pre-existing DOWN monitors are not re-processed by the watchdog."""
    db_session.add(Monitor(
        id="dev-already-down",
        timeout=60,
        alert_email="admin@critmon.com",
        status=MonitorStatus.DOWN,
        last_heartbeat=_past(120),
        expires_at=_past(60),
        created_at=_past(200),
    ))
    db_session.commit()

    alert_svc = CapturingAlertService()
    count = _run_tick(db_session, alert_svc)

    assert count == 0
    assert len(alert_svc.calls) == 0


def test_restart_recovery_expired_monitors_detected(db_session: Session, client: TestClient):
    """Simulates app restart: monitors expired during downtime caught on first tick.

    This validates the design guarantee that the polling loop is stateless —
    no in-memory timers are needed because the DB is the source of truth.
    """
    _make_monitor(db_session, "dev-restart", expires_offset_seconds=-120)

    _run_tick(db_session)

    r = client.get("/monitors/dev-restart")
    assert r.json()["status"] == "DOWN"


def test_device_status_transitions_full_lifecycle(db_session: Session, client: TestClient):
    """Verifies the complete state machine: ONLINE → PAUSED → ONLINE → DOWN."""
    # 1. Register (ONLINE)
    r = client.post(
        "/monitors",
        json={"id": "dev-lifecycle", "timeout": 30, "alert_email": "ops@critmon.com"},
    )
    assert r.status_code == 201
    assert r.json()["status"] == "ONLINE"

    # 2. Pause (ONLINE → PAUSED)
    r = client.post("/monitors/dev-lifecycle/pause")
    assert r.status_code == 200
    assert r.json()["monitor"]["status"] == "PAUSED"

    # 3. Heartbeat (PAUSED → ONLINE)
    r = client.post("/monitors/dev-lifecycle/heartbeat")
    assert r.status_code == 200
    assert r.json()["monitor"]["status"] == "ONLINE"

    # 4. Force expiry via DB manipulation then run watchdog (ONLINE → DOWN)
    monitor = db_session.query(Monitor).filter(Monitor.id == "dev-lifecycle").first()
    monitor.expires_at = _past(10)
    db_session.commit()

    _run_tick(db_session)

    r = client.get("/monitors/dev-lifecycle")
    assert r.json()["status"] == "DOWN"

    # 5. Confirm heartbeat now rejected
    r = client.post("/monitors/dev-lifecycle/heartbeat")
    assert r.status_code == 409


# ==============================================================================
# §8 — Health Check now includes watchdog_running
# ==============================================================================


def test_health_includes_watchdog_status(client: TestClient):
    """GET /health now reports watchdog_running boolean."""
    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert "watchdog_running" in data
    assert isinstance(data["watchdog_running"], bool)


# ==============================================================================
# §8 — Alert content validation
# ==============================================================================


def test_alert_content_matches_spec(db_session: Session):
    """Alert payload contains device ID, email, last_heartbeat, and timeout."""
    _make_monitor(db_session, "dev-alert-content", expires_offset_seconds=-5)

    alert_svc = CapturingAlertService()
    _run_tick(db_session, alert_svc)

    assert len(alert_svc.calls) == 1
    call = alert_svc.calls[0]
    assert call["monitor_id"] == "dev-alert-content"
    assert call["alert_email"] == "admin@critmon.com"
    assert isinstance(call["last_heartbeat"], datetime)
    assert call["timeout_seconds"] == 30
