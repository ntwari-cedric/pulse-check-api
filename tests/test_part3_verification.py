"""Part 3 verification test suite — Final Testing and Documentation phase.

Covers every category from the Part 3 spec:
  A. Registration (all required edge cases)
  B. Heartbeat (all required edge cases)
  C. Monitoring (expiry, alert, duplicate-alert prevention, PAUSED safety,
                 multiple independent monitors)
  D. Reliability (concurrent heartbeats, restart recovery, DB persistence)

Testing strategy (no real sleeps — time is controlled via DB manipulation)
---------------------------------------------------------------------------
  - Expired monitors: set expires_at = now - timedelta(seconds=N) in DB.
  - evaluate_expired_monitors(db, alert_svc) is called directly with the
    injected test session — never touches the production database.
  - Concurrent tests use the concurrent_client fixture (file-based SQLite,
    thread-safe per-connection access).
  - All assertions check actual HTTP status codes and response bodies.

HTTP status codes exercised
---------------------------
  200 OK            — heartbeat, pause, GET monitor, GET health, GET /
  201 Created       — successful registration
  404 Not Found     — unknown monitor
  409 Conflict      — duplicate ID, heartbeat on DOWN, pause on DOWN
  422 Unprocessable — validation failures (bad timeout, bad email, bad ID)
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Monitor, MonitorStatus
from app.services.alert_service import AlertService
from app.services.timer_service import calculate_expiration, evaluate_expired_monitors


# ---------------------------------------------------------------------------
# Local helpers (mirror conftest style — keep tests self-contained)
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _past(seconds: int) -> datetime:
    return _now() - timedelta(seconds=seconds)


def _future(seconds: int) -> datetime:
    return _now() + timedelta(seconds=seconds)


def _make_monitor(
    db: Session,
    monitor_id: str,
    status: MonitorStatus = MonitorStatus.ONLINE,
    timeout: int = 30,
    expires_offset_seconds: int = -5,
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
# §A — Registration
# ==============================================================================


def test_p3_register_success_returns_201(client: TestClient):
    """Successful registration returns HTTP 201 with all required fields."""
    response = client.post(
        "/monitors",
        json={"id": "p3-dev-reg", "timeout": 60, "alert_email": "admin@critmon.com"},
    )
    assert response.status_code == 201
    data = response.json()

    # All seven model fields must be present
    for field in ("id", "timeout", "alert_email", "status", "last_heartbeat",
                  "expires_at", "created_at"):
        assert field in data, f"Missing field in registration response: {field}"

    assert data["id"] == "p3-dev-reg"
    assert data["timeout"] == 60
    assert data["alert_email"] == "admin@critmon.com"
    assert data["status"] == "ONLINE"

    # expires_at ≈ created_at + timeout
    created = datetime.fromisoformat(data["created_at"])
    expires = datetime.fromisoformat(data["expires_at"])
    delta = (expires - created).total_seconds()
    assert abs(delta - 60) < 2.0, f"expires_at delta was {delta}, expected ~60"


def test_p3_register_duplicate_returns_409(client: TestClient):
    """Duplicate monitor ID returns HTTP 409 Conflict."""
    payload = {"id": "p3-dup", "timeout": 30, "alert_email": "ops@critmon.com"}
    r1 = client.post("/monitors", json=payload)
    assert r1.status_code == 201

    r2 = client.post("/monitors", json=payload)
    assert r2.status_code == 409
    assert "already exists" in r2.json()["detail"]


@pytest.mark.parametrize("bad_timeout", [0, -1, -9999])
def test_p3_register_invalid_timeout_returns_422(client: TestClient, bad_timeout: int):
    """Timeout values ≤ 0 are rejected with HTTP 422 Unprocessable Entity."""
    response = client.post(
        "/monitors",
        json={"id": "p3-timeout-bad", "timeout": bad_timeout, "alert_email": "ops@critmon.com"},
    )
    assert response.status_code == 422


@pytest.mark.parametrize("bad_email", [
    "not-an-email",
    "@nodomain.com",
    "user@",
    "user@domain",          # TLD missing dot
    "",                     # empty
])
def test_p3_register_invalid_email_returns_422(client: TestClient, bad_email: str):
    """Malformed email addresses are rejected with HTTP 422 Unprocessable Entity."""
    response = client.post(
        "/monitors",
        json={"id": "p3-email-bad", "timeout": 60, "alert_email": bad_email},
    )
    assert response.status_code == 422


def test_p3_register_whitespace_id_returns_422(client: TestClient):
    """A whitespace-only monitor ID is rejected with HTTP 422."""
    response = client.post(
        "/monitors",
        json={"id": "   ", "timeout": 60, "alert_email": "ops@critmon.com"},
    )
    assert response.status_code == 422


def test_p3_register_missing_field_returns_422(client: TestClient):
    """Missing required field (alert_email) returns 422."""
    response = client.post(
        "/monitors",
        json={"id": "p3-missing", "timeout": 60},
    )
    assert response.status_code == 422


# ==============================================================================
# §B — Heartbeat
# ==============================================================================


def test_p3_heartbeat_success_200(client: TestClient):
    """Heartbeat on a live monitor returns HTTP 200 with updated timestamps."""
    client.post(
        "/monitors",
        json={"id": "p3-hb-live", "timeout": 120, "alert_email": "admin@critmon.com"},
    )
    response = client.post("/monitors/p3-hb-live/heartbeat")
    assert response.status_code == 200

    data = response.json()
    assert "Heartbeat acknowledged" in data["message"]
    mon = data["monitor"]
    assert mon["status"] == "ONLINE"


def test_p3_heartbeat_resets_deadline(client: TestClient):
    """Heartbeat extends expires_at by exactly timeout seconds from now."""
    client.post(
        "/monitors",
        json={"id": "p3-hb-deadline", "timeout": 300, "alert_email": "admin@critmon.com"},
    )
    response = client.post("/monitors/p3-hb-deadline/heartbeat")
    assert response.status_code == 200

    mon = response.json()["monitor"]
    hb_time = datetime.fromisoformat(mon["last_heartbeat"])
    exp_time = datetime.fromisoformat(mon["expires_at"])
    delta = (exp_time - hb_time).total_seconds()
    assert abs(delta - 300) < 2.0, f"Deadline delta {delta} expected ~300"


def test_p3_heartbeat_missing_device_returns_404(client: TestClient):
    """Heartbeat for non-existent device returns HTTP 404 Not Found."""
    response = client.post("/monitors/no-such-device-p3/heartbeat")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_p3_heartbeat_down_device_returns_409(client: TestClient, db_session: Session):
    """Heartbeat on a DOWN device returns HTTP 409 Conflict."""
    now = _now()
    db_session.add(Monitor(
        id="p3-dev-down-hb",
        timeout=60,
        alert_email="admin@critmon.com",
        status=MonitorStatus.DOWN,
        last_heartbeat=_past(120),
        expires_at=_past(60),
        created_at=_past(200),
    ))
    db_session.commit()

    response = client.post("/monitors/p3-dev-down-hb/heartbeat")
    assert response.status_code == 409
    assert "DOWN" in response.json()["detail"]


def test_p3_heartbeat_resumes_paused_device(client: TestClient, db_session: Session):
    """Heartbeat on a PAUSED device transitions it to ONLINE (auto-resume)."""
    db_session.add(Monitor(
        id="p3-dev-paused-resume",
        timeout=60,
        alert_email="admin@critmon.com",
        status=MonitorStatus.PAUSED,
        last_heartbeat=_past(90),
        expires_at=_past(30),
        created_at=_past(100),
    ))
    db_session.commit()

    response = client.post("/monitors/p3-dev-paused-resume/heartbeat")
    assert response.status_code == 200
    mon = response.json()["monitor"]
    assert mon["status"] == "ONLINE"
    # Deadline must be reset into the future
    exp = datetime.fromisoformat(mon["expires_at"])
    assert exp > _now()


# ==============================================================================
# §C — Monitoring Engine
# ==============================================================================


def test_p3_expired_device_becomes_down(db_session: Session, client: TestClient):
    """An ONLINE device whose expires_at is in the past is marked DOWN by the watchdog."""
    _make_monitor(db_session, "p3-expired-down", expires_offset_seconds=-15)

    # Verify ONLINE before evaluation
    r = client.get("/monitors/p3-expired-down")
    assert r.json()["status"] == "ONLINE"

    _run_tick(db_session)

    r2 = client.get("/monitors/p3-expired-down")
    assert r2.status_code == 200
    assert r2.json()["status"] == "DOWN"


def test_p3_alert_generated_on_expiry(db_session: Session):
    """Alert is dispatched exactly once when a device transitions ONLINE → DOWN."""
    _make_monitor(db_session, "p3-alert-gen", expires_offset_seconds=-5)

    alert_svc = CapturingAlertService()
    count = _run_tick(db_session, alert_svc)

    assert count == 1
    assert len(alert_svc.calls) == 1

    call = alert_svc.calls[0]
    assert call["monitor_id"] == "p3-alert-gen"
    assert call["alert_email"] == "admin@critmon.com"
    assert isinstance(call["last_heartbeat"], datetime)
    assert call["timeout_seconds"] == 30


def test_p3_alert_generated_only_once(db_session: Session):
    """Running the watchdog multiple times after DOWN never re-alerts."""
    _make_monitor(db_session, "p3-no-dup-alert", expires_offset_seconds=-5)

    alert_svc = CapturingAlertService()

    count1 = _run_tick(db_session, alert_svc)
    assert count1 == 1
    assert len(alert_svc.calls) == 1

    # Additional cycles — monitor is already DOWN; rowcount=0
    for _ in range(4):
        count = _run_tick(db_session, alert_svc)
        assert count == 0

    # Alert was sent exactly ONCE regardless of cycle count
    assert len(alert_svc.calls) == 1


def test_p3_paused_device_does_not_expire(db_session: Session, client: TestClient):
    """A PAUSED device is never transitioned to DOWN by the watchdog, even if expired."""
    db_session.add(Monitor(
        id="p3-paused-safe",
        timeout=30,
        alert_email="admin@critmon.com",
        status=MonitorStatus.PAUSED,
        last_heartbeat=_past(80),
        expires_at=_past(50),
        created_at=_past(100),
    ))
    db_session.commit()

    alert_svc = CapturingAlertService()
    count = _run_tick(db_session, alert_svc)

    assert count == 0
    assert len(alert_svc.calls) == 0

    r = client.get("/monitors/p3-paused-safe")
    assert r.json()["status"] == "PAUSED"


def test_p3_multiple_monitors_handled_independently(db_session: Session, client: TestClient):
    """Multiple monitors expire and alert independently in a single watchdog tick."""
    ids = [f"p3-multi-{i}" for i in range(4)]
    for mid in ids:
        _make_monitor(db_session, mid, expires_offset_seconds=-10)

    # Also insert one PAUSED and one healthy ONLINE — these must NOT be touched
    db_session.add(Monitor(
        id="p3-multi-paused",
        timeout=30,
        alert_email="admin@critmon.com",
        status=MonitorStatus.PAUSED,
        last_heartbeat=_past(80),
        expires_at=_past(50),
        created_at=_past(100),
    ))
    _make_monitor(db_session, "p3-multi-healthy", expires_offset_seconds=+300)
    db_session.commit()

    alert_svc = CapturingAlertService()
    count = _run_tick(db_session, alert_svc)

    # Only the 4 expired ONLINE ones should have transitioned
    assert count == 4
    assert len(alert_svc.calls) == 4
    alerted_ids = {c["monitor_id"] for c in alert_svc.calls}
    assert alerted_ids == set(ids)

    # PAUSED stays PAUSED
    r = client.get("/monitors/p3-multi-paused")
    assert r.json()["status"] == "PAUSED"

    # Healthy stays ONLINE
    r2 = client.get("/monitors/p3-multi-healthy")
    assert r2.json()["status"] == "ONLINE"


# ==============================================================================
# §D — Reliability
# ==============================================================================


def test_p3_concurrent_heartbeats(concurrent_client: TestClient):
    """15 simultaneous heartbeat requests all succeed without deadlocks or crashes.

    Uses file-based SQLite (concurrent_client fixture) for thread-safe concurrent access.
    """
    concurrent_client.post(
        "/monitors",
        json={"id": "p3-concurrent", "timeout": 300, "alert_email": "ops@critmon.com"},
    )

    def send_heartbeat(_):
        return concurrent_client.post("/monitors/p3-concurrent/heartbeat").status_code

    with ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(send_heartbeat, range(15)))

    assert all(code == 200 for code in results), (
        f"Some heartbeats failed: {[c for c in results if c != 200]}"
    )


def test_p3_restart_recovery(db_session: Session, client: TestClient):
    """Simulates a server restart: monitors expired during downtime are caught on first tick.

    The polling loop is stateless — it re-evaluates the entire database on
    every tick. No in-memory state is needed; the DB is the source of truth.
    """
    # Simulate a monitor that expired 5 minutes ago (during 'downtime')
    _make_monitor(db_session, "p3-restart-dev", expires_offset_seconds=-300)

    # Verify it shows ONLINE before the first tick (no watchdog ran during downtime)
    r = client.get("/monitors/p3-restart-dev")
    assert r.json()["status"] == "ONLINE"

    # First tick after 'restart' catches all expired monitors
    alert_svc = CapturingAlertService()
    count = _run_tick(db_session, alert_svc)

    assert count == 1
    assert len(alert_svc.calls) == 1

    r2 = client.get("/monitors/p3-restart-dev")
    assert r2.json()["status"] == "DOWN"


def test_p3_database_persistence(client: TestClient, db_session: Session):
    """Data written by one HTTP request is immediately visible in subsequent requests."""
    # Register
    r1 = client.post(
        "/monitors",
        json={"id": "p3-persist", "timeout": 90, "alert_email": "persist@critmon.com"},
    )
    assert r1.status_code == 201

    # Read back — different session, same in-memory DB (StaticPool)
    r2 = client.get("/monitors/p3-persist")
    assert r2.status_code == 200
    data = r2.json()
    assert data["id"] == "p3-persist"
    assert data["timeout"] == 90
    assert data["alert_email"] == "persist@critmon.com"
    assert data["status"] == "ONLINE"

    # Heartbeat updates are also persisted
    r3 = client.post("/monitors/p3-persist/heartbeat")
    assert r3.status_code == 200

    r4 = client.get("/monitors/p3-persist")
    hb_after = datetime.fromisoformat(r4.json()["last_heartbeat"])
    hb_before = datetime.fromisoformat(r1.json()["last_heartbeat"])
    assert hb_after >= hb_before  # last_heartbeat advanced


def test_p3_down_status_persisted_after_watchdog(db_session: Session, client: TestClient):
    """DOWN status written by watchdog evaluation is durable and visible via GET."""
    _make_monitor(db_session, "p3-down-persist", expires_offset_seconds=-10)

    _run_tick(db_session)

    # Expire ORM cache and re-query to confirm DB-level persistence
    db_session.expire_all()
    monitor = db_session.query(Monitor).filter(Monitor.id == "p3-down-persist").first()
    assert monitor is not None
    assert monitor.status == MonitorStatus.DOWN

    # Also visible through the API
    r = client.get("/monitors/p3-down-persist")
    assert r.json()["status"] == "DOWN"


# ==============================================================================
# §E — HTTP status code matrix (explicit table verification)
# ==============================================================================


def test_p3_status_200_on_root(client: TestClient):
    """GET / returns 200 OK."""
    assert client.get("/").status_code == 200


def test_p3_status_200_on_health(client: TestClient):
    """GET /health returns 200 OK with watchdog_running field."""
    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert "watchdog_running" in data
    assert isinstance(data["watchdog_running"], bool)


def test_p3_status_200_on_get_monitor(client: TestClient):
    """GET /monitors/{id} returns 200 OK."""
    client.post("/monitors", json={"id": "p3-get-200", "timeout": 60, "alert_email": "a@b.com"})
    assert client.get("/monitors/p3-get-200").status_code == 200


def test_p3_status_200_on_list_monitors(client: TestClient):
    """GET /monitors returns 200 OK."""
    assert client.get("/monitors").status_code == 200


def test_p3_status_201_on_register(client: TestClient):
    """POST /monitors returns 201 Created."""
    r = client.post("/monitors", json={"id": "p3-201-test", "timeout": 60, "alert_email": "a@b.com"})
    assert r.status_code == 201


def test_p3_status_404_on_missing_monitor(client: TestClient):
    """GET /monitors/{id} returns 404 for unknown device."""
    assert client.get("/monitors/definitely-does-not-exist").status_code == 404


def test_p3_status_404_on_heartbeat_missing(client: TestClient):
    """POST /monitors/{id}/heartbeat returns 404 for unknown device."""
    assert client.post("/monitors/ghost-device/heartbeat").status_code == 404


def test_p3_status_404_on_pause_missing(client: TestClient):
    """POST /monitors/{id}/pause returns 404 for unknown device."""
    assert client.post("/monitors/ghost-device/pause").status_code == 404


def test_p3_status_409_on_duplicate_registration(client: TestClient):
    """POST /monitors with duplicate ID returns 409 Conflict."""
    payload = {"id": "p3-409-dup", "timeout": 60, "alert_email": "a@b.com"}
    client.post("/monitors", json=payload)
    assert client.post("/monitors", json=payload).status_code == 409


def test_p3_status_409_on_heartbeat_to_down(client: TestClient, db_session: Session):
    """POST /monitors/{id}/heartbeat on DOWN device returns 409 Conflict."""
    now = _now()
    db_session.add(Monitor(
        id="p3-409-hb",
        timeout=60,
        alert_email="a@b.com",
        status=MonitorStatus.DOWN,
        last_heartbeat=_past(120),
        expires_at=_past(60),
        created_at=_past(200),
    ))
    db_session.commit()
    assert client.post("/monitors/p3-409-hb/heartbeat").status_code == 409


def test_p3_status_409_on_pause_down(client: TestClient, db_session: Session):
    """POST /monitors/{id}/pause on DOWN device returns 409 Conflict."""
    now = _now()
    db_session.add(Monitor(
        id="p3-409-pause",
        timeout=60,
        alert_email="a@b.com",
        status=MonitorStatus.DOWN,
        last_heartbeat=_past(120),
        expires_at=_past(60),
        created_at=_past(200),
    ))
    db_session.commit()
    assert client.post("/monitors/p3-409-pause/pause").status_code == 409


def test_p3_status_422_on_invalid_timeout(client: TestClient):
    """POST /monitors with invalid timeout returns 422 Unprocessable Entity."""
    r = client.post("/monitors", json={"id": "p3-422", "timeout": 0, "alert_email": "a@b.com"})
    assert r.status_code == 422


def test_p3_status_422_on_invalid_email(client: TestClient):
    """POST /monitors with invalid email returns 422 Unprocessable Entity."""
    r = client.post("/monitors", json={"id": "p3-422e", "timeout": 60, "alert_email": "bad"})
    assert r.status_code == 422


# ==============================================================================
# §F — Pause and Resume
# ==============================================================================


def test_p3_pause_online_monitor(client: TestClient):
    """POST /monitors/{id}/pause on ONLINE device returns 200 and sets status PAUSED."""
    client.post("/monitors", json={"id": "p3-pause-ok", "timeout": 60, "alert_email": "a@b.com"})
    r = client.post("/monitors/p3-pause-ok/pause")
    assert r.status_code == 200
    data = r.json()
    assert data["monitor"]["status"] == "PAUSED"
    assert "paused" in data["message"].lower()


def test_p3_pause_idempotent(client: TestClient):
    """Pausing an already-PAUSED device is idempotent — returns 200, never 409."""
    client.post("/monitors", json={"id": "p3-pause-idem", "timeout": 60, "alert_email": "a@b.com"})
    client.post("/monitors/p3-pause-idem/pause")

    r = client.post("/monitors/p3-pause-idem/pause")
    assert r.status_code == 200
    assert r.json()["monitor"]["status"] == "PAUSED"


def test_p3_full_lifecycle(db_session: Session, client: TestClient):
    """Full state machine: ONLINE → PAUSED → ONLINE (via heartbeat) → DOWN (via watchdog)."""
    # 1. Register → ONLINE
    r = client.post(
        "/monitors",
        json={"id": "p3-lifecycle", "timeout": 30, "alert_email": "ops@critmon.com"},
    )
    assert r.status_code == 201
    assert r.json()["status"] == "ONLINE"

    # 2. Pause → PAUSED
    r = client.post("/monitors/p3-lifecycle/pause")
    assert r.json()["monitor"]["status"] == "PAUSED"

    # 3. Heartbeat → ONLINE (auto-resume)
    r = client.post("/monitors/p3-lifecycle/heartbeat")
    assert r.json()["monitor"]["status"] == "ONLINE"

    # 4. Force expiry via DB, run watchdog → DOWN
    monitor = db_session.query(Monitor).filter(Monitor.id == "p3-lifecycle").first()
    monitor.expires_at = _past(10)
    db_session.commit()

    alert_svc = CapturingAlertService()
    _run_tick(db_session, alert_svc)
    assert len(alert_svc.calls) == 1

    r = client.get("/monitors/p3-lifecycle")
    assert r.json()["status"] == "DOWN"

    # 5. Heartbeat now rejected → 409
    r = client.post("/monitors/p3-lifecycle/heartbeat")
    assert r.status_code == 409
