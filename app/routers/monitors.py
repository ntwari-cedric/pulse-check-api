"""Router for monitor registration, heartbeats, pause/resume, and inspection.

Part 2 additions:
  - POST /monitors/{id}/pause  (User Story: Pause & Resume)
  - GET  /monitors/{id}        already existed from Part 1; documented here
  - Heartbeat now explicitly handles PAUSED → ONLINE resume (was already
    implemented in Part 1 but is now a formal User Story requirement).

Concurrency strategy
--------------------
All write operations (register, heartbeat, pause) fetch the target row with
.with_for_update() to serialise concurrent writes on the same monitor.
The heartbeat handler additionally captures field values from the ORM object
*before* the session expires on commit, avoiding a cross-thread db.refresh()
in multi-threaded test scenarios.
"""

from datetime import datetime, timezone
import logging
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Monitor, MonitorStatus
from app.schemas import (
    HeartbeatResponse,
    MonitorCreate,
    MonitorResponse,
    PauseResponse,
)
from app.services.timer_service import calculate_expiration

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/monitors",
    tags=["Monitors"],
)


# ==============================================================================
# User Story 1: Monitor Registration
# ==============================================================================


@router.post(
    "",
    response_model=MonitorResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new device monitor",
    description=(
        "Registers a new remote device for dead-man's switch monitoring. "
        "Calculates initial heartbeat and expiration timestamp based on the provided timeout."
    ),
)
def register_monitor(
    payload: MonitorCreate,
    db: Session = Depends(get_db),
) -> MonitorResponse:
    """User Story 1: Monitor Registration.

    - Validates payload format via Pydantic (ID, timeout > 0, email format).
    - Checks for duplicate ID and returns HTTP 409 Conflict if already registered.
    - Persists monitor record in SQLite with status ONLINE.
    - Sets initial last_heartbeat and expires_at.
    - Returns HTTP 201 Created.
    """
    existing_monitor = db.query(Monitor).filter(Monitor.id == payload.id).first()
    if existing_monitor is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Monitor with ID '{payload.id}' already exists.",
        )

    now = datetime.now(timezone.utc)
    expires_at = calculate_expiration(now, payload.timeout)

    new_monitor = Monitor(
        id=payload.id,
        timeout=payload.timeout,
        alert_email=payload.alert_email,
        status=MonitorStatus.ONLINE,
        last_heartbeat=now,
        expires_at=expires_at,
        created_at=now,
    )

    try:
        db.add(new_monitor)
        db.commit()
        db.refresh(new_monitor)
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Monitor with ID '{payload.id}' already exists.",
        )
    except Exception as exc:
        db.rollback()
        logger.error("Failed to register monitor '%s': %s", payload.id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database transaction error during registration.",
        )

    logger.info(
        "Registered monitor '%s' (timeout=%ds, alert_email=%s, expires_at=%s)",
        new_monitor.id,
        new_monitor.timeout,
        new_monitor.alert_email,
        new_monitor.expires_at.isoformat(),
    )
    return MonitorResponse.model_validate(new_monitor)


# ==============================================================================
# User Story 2: Heartbeat (also auto-resumes PAUSED monitors)
# ==============================================================================


@router.post(
    "/{id}/heartbeat",
    response_model=HeartbeatResponse,
    status_code=status.HTTP_200_OK,
    summary="Record device heartbeat",
    description=(
        "Records a periodic heartbeat check-in for a device. "
        "Updates last_heartbeat and resets expires_at. "
        "Automatically resumes a PAUSED monitor to ONLINE. "
        "Rejects requests for devices marked DOWN (HTTP 409)."
    ),
)
def record_heartbeat(
    id: str,
    db: Session = Depends(get_db),
) -> HeartbeatResponse:
    """User Story 2 + Bonus: Heartbeat and automatic PAUSED resume.

    DOWN monitors cannot be recovered via heartbeat — an explicit recovery
    mechanism (Part 3) is required to prevent accidental revival of a declared
    dead device.
    """
    now = datetime.now(timezone.utc)

    monitor = db.query(Monitor).filter(Monitor.id == id).with_for_update().first()

    if monitor is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Monitor with ID '{id}' was not found.",
        )

    if monitor.status == MonitorStatus.DOWN:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Monitor '{id}' is marked DOWN. "
                "Heartbeats cannot revive a dead monitor. "
                "Use the explicit recovery endpoint (Part 3)."
            ),
        )

    # PAUSED → ONLINE: heartbeat automatically resumes a paused device
    if monitor.status == MonitorStatus.PAUSED:
        monitor.status = MonitorStatus.ONLINE
        logger.info("Monitor '%s' resumed from PAUSED to ONLINE via heartbeat.", id)

    monitor.last_heartbeat = now
    monitor.expires_at = calculate_expiration(now, monitor.timeout)

    try:
        db.commit()
        # Capture field values from the in-memory ORM object before the session
        # expires post-commit. Avoids a db.refresh() that would issue a new
        # SELECT — which is not thread-safe on shared in-memory SQLite.
        monitor_id = monitor.id
        monitor_timeout = monitor.timeout
        monitor_alert_email = monitor.alert_email
        monitor_status = monitor.status
        monitor_last_heartbeat = monitor.last_heartbeat
        monitor_expires_at = monitor.expires_at
        monitor_created_at = monitor.created_at
    except Exception as exc:
        db.rollback()
        logger.error("Failed to record heartbeat for '%s': %s", id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database transaction error during heartbeat update.",
        )

    logger.info(
        "Heartbeat acknowledged for '%s'. New deadline: %s",
        monitor_id,
        monitor_expires_at.isoformat(),
    )

    return HeartbeatResponse(
        message=f"Heartbeat acknowledged for device '{monitor_id}'. Expiration extended to {monitor_expires_at.isoformat()}.",
        monitor=MonitorResponse(
            id=monitor_id,
            timeout=monitor_timeout,
            alert_email=monitor_alert_email,
            status=monitor_status,
            last_heartbeat=monitor_last_heartbeat,
            expires_at=monitor_expires_at,
            created_at=monitor_created_at,
        ),
    )


# ==============================================================================
# Bonus User Story: Pause
# ==============================================================================


@router.post(
    "/{id}/pause",
    response_model=PauseResponse,
    status_code=status.HTTP_200_OK,
    summary="Pause a device monitor",
    description=(
        "Sets the monitor status to PAUSED. "
        "The watchdog engine skips PAUSED monitors — no DOWN transition or alerts "
        "are generated while paused. "
        "Idempotent: pausing an already-PAUSED monitor is safe and returns HTTP 200. "
        "A subsequent heartbeat automatically resumes the monitor to ONLINE. "
        "Cannot pause a DOWN monitor (HTTP 409)."
    ),
)
def pause_monitor(
    id: str,
    db: Session = Depends(get_db),
) -> PauseResponse:
    """Bonus User Story: Pause and Resume.

    Design decisions:
    - PAUSED monitors are excluded from expiry evaluation in the watchdog loop
      using a WHERE status='ONLINE' filter. No special bookkeeping needed.
    - Idempotency: calling pause on an already-PAUSED monitor is a no-op that
      returns 200. Clients can safely retry without worrying about state.
    - DOWN monitors cannot be paused — the device has already been declared
      dead. This prevents confusion between paused and dead states.
    - Resume happens automatically via heartbeat: the heartbeat handler detects
      PAUSED status and sets it to ONLINE before updating timestamps.
    """
    monitor = db.query(Monitor).filter(Monitor.id == id).with_for_update().first()

    if monitor is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Monitor with ID '{id}' was not found.",
        )

    if monitor.status == MonitorStatus.DOWN:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Monitor '{id}' is already DOWN and cannot be paused. "
                "Use the recovery endpoint to bring it back online first."
            ),
        )

    already_paused = monitor.status == MonitorStatus.PAUSED

    if not already_paused:
        monitor.status = MonitorStatus.PAUSED
        try:
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.error("Failed to pause monitor '%s': %s", id, exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Database transaction error during pause.",
            )
        logger.info("Monitor '%s' paused (was ONLINE).", id)
    else:
        logger.debug("Monitor '%s' pause requested but already PAUSED (no-op).", id)

    message = (
        f"Monitor '{id}' is already paused."
        if already_paused
        else f"Monitor '{id}' has been paused. No alerts will be generated until resumed."
    )

    return PauseResponse(
        message=message,
        monitor=MonitorResponse(
            id=monitor.id,
            timeout=monitor.timeout,
            alert_email=monitor.alert_email,
            status=monitor.status,
            last_heartbeat=monitor.last_heartbeat,
            expires_at=monitor.expires_at,
            created_at=monitor.created_at,
        ),
    )


# ==============================================================================
# Developer's Choice Feature: GET /monitors/{id}
# ==============================================================================


@router.get(
    "/{id}",
    response_model=MonitorResponse,
    status_code=status.HTTP_200_OK,
    summary="Get monitor details by ID",
    description=(
        "Retrieves the complete current state of a specific monitor including "
        "its operational status, last heartbeat timestamp, and expiration deadline. "
        "\n\n"
        "**Why this improves usability:** Administrators can inspect any single device "
        "without paginating through the full list. Essential for dashboards, "
        "alerting scripts, and debugging. Returns HTTP 404 for unknown IDs."
    ),
)
def get_monitor(
    id: str,
    db: Session = Depends(get_db),
) -> MonitorResponse:
    """Developer's Choice Feature: Device Status Inspector.

    Returns the complete monitor record including all timestamps and status.
    Use this endpoint to check whether a specific device is healthy before
    taking operational action.
    """
    monitor = db.query(Monitor).filter(Monitor.id == id).first()
    if monitor is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Monitor with ID '{id}' was not found.",
        )
    return MonitorResponse.model_validate(monitor)


# ==============================================================================
# List Monitors
# ==============================================================================


@router.get(
    "",
    response_model=List[MonitorResponse],
    status_code=status.HTTP_200_OK,
    summary="List all monitors",
    description="Retrieves a paginated list of all registered monitors, with optional status filtering.",
)
def list_monitors(
    status_filter: Optional[MonitorStatus] = Query(
        None,
        alias="status",
        description="Filter monitors by operational status (ONLINE, DOWN, PAUSED)",
    ),
    skip: int = Query(0, ge=0, description="Pagination skip offset"),
    limit: int = Query(100, ge=1, le=500, description="Pagination limit"),
    db: Session = Depends(get_db),
) -> List[MonitorResponse]:
    """Lists registered monitors with optional pagination and status filtering."""
    query = db.query(Monitor)
    if status_filter is not None:
        query = query.filter(Monitor.status == status_filter)
    monitors = query.offset(skip).limit(limit).all()
    return [MonitorResponse.model_validate(m) for m in monitors]
