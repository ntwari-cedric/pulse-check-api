"""Comprehensive test suite for Pulse-Check API Part 1."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Monitor, MonitorStatus
from app.services.timer_service import calculate_expiration


def test_root_endpoint(client: TestClient):
    """Verifies that the root endpoint returns API metadata and docs link."""
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["title"] == "Pulse-Check API"
    assert data["version"] == "1.0.0"
    assert data["docs_url"] == "/docs"


def test_health_check(client: TestClient):
    """Verifies that the health check reports healthy application and database."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["database"] == "connected"
    assert data["version"] == "1.0.0"
    assert "timestamp" in data


# ==============================================================================
# User Story 1: Registration Tests
# ==============================================================================


def test_register_monitor_success(client: TestClient):
    """User Story 1: Successfully registers a new monitor."""
    payload = {
        "id": "device-123",
        "timeout": 60,
        "alert_email": "admin@critmon.com",
    }
    response = client.post("/monitors", json=payload)
    assert response.status_code == 201

    data = response.json()
    assert data["id"] == "device-123"
    assert data["timeout"] == 60
    assert data["alert_email"] == "admin@critmon.com"
    assert data["status"] == "ONLINE"
    assert "created_at" in data
    assert "last_heartbeat" in data
    assert "expires_at" in data

    # Verify expires_at is roughly created_at + 60s
    created = datetime.fromisoformat(data["created_at"])
    expires = datetime.fromisoformat(data["expires_at"])
    delta = (expires - created).total_seconds()
    assert abs(delta - 60) < 2.0


def test_register_duplicate_monitor_rejected(client: TestClient):
    """User Story 1: Duplicate registration must return HTTP 409 Conflict."""
    payload = {
        "id": "device-duplicate",
        "timeout": 120,
        "alert_email": "admin@critmon.com",
    }
    # First registration
    r1 = client.post("/monitors", json=payload)
    assert r1.status_code == 201

    # Second registration with duplicate ID
    r2 = client.post("/monitors", json=payload)
    assert r2.status_code == 409
    assert "already exists" in r2.json()["detail"]


@pytest.mark.parametrize(
    "invalid_timeout",
    [0, -1, -500],
)
def test_register_invalid_timeout(client: TestClient, invalid_timeout: int):
    """User Story 1: Invalid timeouts (<=0) must be rejected with 422."""
    payload = {
        "id": "device-invalid-timeout",
        "timeout": invalid_timeout,
        "alert_email": "admin@critmon.com",
    }
    response = client.post("/monitors", json=payload)
    assert response.status_code == 422


@pytest.mark.parametrize(
    "invalid_email",
    ["not-an-email", "@missing-user.com", "user@", "user@domain"],
)
def test_register_invalid_email(client: TestClient, invalid_email: str):
    """User Story 1: Malformed emails must be rejected with 422."""
    payload = {
        "id": "device-invalid-email",
        "timeout": 60,
        "alert_email": invalid_email,
    }
    response = client.post("/monitors", json=payload)
    assert response.status_code == 422


def test_register_invalid_id(client: TestClient):
    """User Story 1: Empty or whitespace IDs must be rejected."""
    payload = {
        "id": "   ",
        "timeout": 60,
        "alert_email": "admin@critmon.com",
    }
    response = client.post("/monitors", json=payload)
    assert response.status_code == 422


# ==============================================================================
# User Story 2: Heartbeat Tests
# ==============================================================================


def test_heartbeat_success(client: TestClient):
    """User Story 2: Heartbeat successfully updates last_heartbeat and resets expires_at."""
    # 1. Register device
    reg_payload = {
        "id": "device-heartbeat-1",
        "timeout": 300,
        "alert_email": "admin@critmon.com",
    }
    reg_resp = client.post("/monitors", json=reg_payload)
    assert reg_resp.status_code == 201
    initial_data = reg_resp.json()

    # 2. Send heartbeat
    hb_resp = client.post(f"/monitors/{reg_payload['id']}/heartbeat")
    assert hb_resp.status_code == 200
    hb_data = hb_resp.json()

    assert "Heartbeat acknowledged" in hb_data["message"]
    monitor = hb_data["monitor"]
    assert monitor["id"] == "device-heartbeat-1"
    assert monitor["status"] == "ONLINE"

    # Verify timestamp updated and expiration reset
    hb_last = datetime.fromisoformat(monitor["last_heartbeat"])
    hb_expires = datetime.fromisoformat(monitor["expires_at"])
    delta = (hb_expires - hb_last).total_seconds()
    assert abs(delta - 300) < 2.0


def test_heartbeat_nonexistent_monitor(client: TestClient):
    """User Story 2: Heartbeat for non-existent monitor returns HTTP 404."""
    response = client.post("/monitors/non-existent-device/heartbeat")
    assert response.status_code == 404
    assert "was not found" in response.json()["detail"]


def test_heartbeat_down_monitor_rejected(client: TestClient, db_session: Session):
    """User Story 2: Heartbeat for monitor already marked DOWN returns HTTP 409."""
    now = datetime.now(timezone.utc)
    down_monitor = Monitor(
        id="device-dead",
        timeout=60,
        alert_email="admin@critmon.com",
        status=MonitorStatus.DOWN,
        last_heartbeat=now,
        expires_at=now,
        created_at=now,
    )
    db_session.add(down_monitor)
    db_session.commit()

    # Attempt heartbeat on dead device
    response = client.post("/monitors/device-dead/heartbeat")
    assert response.status_code == 409
    assert "marked DOWN" in response.json()["detail"]


def test_heartbeat_paused_monitor_reactivates(client: TestClient, db_session: Session):
    """Heartbeat on a PAUSED monitor should safely resume it to ONLINE."""
    now = datetime.now(timezone.utc)
    paused_monitor = Monitor(
        id="device-paused",
        timeout=60,
        alert_email="admin@critmon.com",
        status=MonitorStatus.PAUSED,
        last_heartbeat=now,
        expires_at=calculate_expiration(now, 60),
        created_at=now,
    )
    db_session.add(paused_monitor)
    db_session.commit()

    response = client.post("/monitors/device-paused/heartbeat")
    assert response.status_code == 200
    assert response.json()["monitor"]["status"] == "ONLINE"


# ==============================================================================
# Inspection Endpoints Tests
# ==============================================================================


def test_get_monitor_by_id(client: TestClient):
    """Verifies GET /monitors/{id} returns monitor details."""
    payload = {
        "id": "device-inspect-1",
        "timeout": 45,
        "alert_email": "ops@critmon.com",
    }
    client.post("/monitors", json=payload)

    response = client.get("/monitors/device-inspect-1")
    assert response.status_code == 200
    assert response.json()["id"] == "device-inspect-1"
    assert response.json()["timeout"] == 45


def test_get_monitor_by_id_not_found(client: TestClient):
    """Verifies GET /monitors/{id} returns 404 for missing monitor."""
    response = client.get("/monitors/does-not-exist")
    assert response.status_code == 404


def test_list_monitors_with_filtering(client: TestClient):
    """Verifies GET /monitors lists registered monitors and filters by status."""
    client.post(
        "/monitors",
        json={"id": "dev-a", "timeout": 60, "alert_email": "a@critmon.com"},
    )
    client.post(
        "/monitors",
        json={"id": "dev-b", "timeout": 60, "alert_email": "b@critmon.com"},
    )

    # List all
    res = client.get("/monitors")
    assert res.status_code == 200
    items = res.json()
    assert len(items) == 2

    # Filter by ONLINE
    res_online = client.get("/monitors?status=ONLINE")
    assert res_online.status_code == 200
    assert len(res_online.json()) == 2

    # Filter by DOWN (should be empty)
    res_down = client.get("/monitors?status=DOWN")
    assert res_down.status_code == 200
    assert len(res_down.json()) == 0


# ==============================================================================
# Concurrency / Simultaneous Heartbeats Test
# ==============================================================================


def test_concurrent_heartbeats(concurrent_client: TestClient):
    """Simulates simultaneous concurrent heartbeat requests to ensure transaction safety.

    Uses a file-based SQLite database (via concurrent_client fixture) rather than
    in-memory StaticPool to avoid the sqlite3 thread-safety limitation where all
    threads would share one connection object, causing InterfaceError under load.
    """
    # Register device
    concurrent_client.post(
        "/monitors",
        json={"id": "device-concurrent", "timeout": 120, "alert_email": "ops@critmon.com"},
    )

    def send_hb():
        return concurrent_client.post("/monitors/device-concurrent/heartbeat").status_code

    with ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(lambda _: send_hb(), range(15)))

    # Every concurrent request must succeed with 200 OK without deadlocks or crashes
    assert all(code == 200 for code in results)
