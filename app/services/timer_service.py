"""Timer and expiration evaluation service for Pulse-Check API — Part 2.

Background Monitoring Engine Design
=====================================

The TimerService runs a single asyncio background task (started via
asyncio.create_task inside the FastAPI lifespan). It wakes up at a
configurable interval, queries the database for expired ONLINE monitors,
and transitions each one to DOWN while dispatching an alert.

Why a single polling loop instead of one timer per device?
----------------------------------------------------------
Per-device timers (e.g. asyncio.sleep(device.timeout) per monitor) would
require cancellation bookkeeping on every heartbeat, every pause, and every
restart. If the server restarts, in-memory timers are lost entirely. The
polling approach is stateless — on every restart it simply re-evaluates the
database, so recovery is automatic with no special restart logic.

Concurrency safety strategy
----------------------------
The evaluation loop uses a SQL UPDATE with a WHERE clause that acts as a
conditional lock:

    UPDATE monitors
    SET status = 'DOWN'
    WHERE id = :id
      AND status = 'ONLINE'           ← only transition ONLINE monitors
      AND expires_at <= :now          ← only expired ones

SQLAlchemy executes this within a transaction and the database returns
`rowcount` — the number of rows actually updated. We only send an alert
if rowcount == 1 (meaning this exact evaluation cycle won the update).
If another process already set status=DOWN, rowcount == 0 → no alert.

Race condition: heartbeat arrives simultaneously with expiry evaluation
-----------------------------------------------------------------------
Both operations compete for the same row. One of these wins:
- Heartbeat wins: it updates expires_at to future, status stays ONLINE.
  The evaluation's UPDATE finds status=ONLINE but expires_at > now, so
  the WHERE clause fails → rowcount=0 → no DOWN transition.
- Evaluation wins: it sets status=DOWN. The heartbeat handler's
  WITH_FOR_UPDATE fetch returns status=DOWN → heartbeat returns HTTP 409.
Either outcome is correct with no data corruption.

Application restart recovery
-----------------------------
On restart, init_db() recreates the schema and the loop starts immediately.
Any monitor whose expires_at < now and status == ONLINE is detected on the
very first evaluation tick. No startup bookkeeping is required.

Testability
-----------
All DB-touching functions accept a `db: Session` parameter. Tests inject a
test session instead of the production SessionLocal, so evaluations run
against the in-memory SQLite test database.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import logging
from typing import TYPE_CHECKING

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Monitor, MonitorStatus

if TYPE_CHECKING:
    from app.services.alert_service import AlertService

logger = logging.getLogger(__name__)

# Default polling interval. Tests override via TimerService(check_interval_seconds=0).
DEFAULT_CHECK_INTERVAL = 10


def calculate_expiration(base_time: datetime, timeout_seconds: int) -> datetime:
    """Calculates the UTC expiration timestamp from a base time and timeout.

    Args:
        base_time: UTC datetime of the most recent heartbeat (or registration).
        timeout_seconds: How many seconds after base_time the device expires.

    Returns:
        A timezone-aware UTC datetime representing the expiration deadline.
    """
    if base_time.tzinfo is None:
        base_time = base_time.replace(tzinfo=timezone.utc)
    else:
        base_time = base_time.astimezone(timezone.utc)
    return base_time + timedelta(seconds=timeout_seconds)


def evaluate_expired_monitors(db: Session, alert_service: "AlertService") -> int:
    """Core evaluation logic: finds expired ONLINE monitors and marks them DOWN.

    This function is intentionally synchronous so it can run inside a plain
    asyncio task without async session complexity. Alert tasks are scheduled
    as fire-and-forget coroutines using asyncio.ensure_future().

    Strategy for no-duplicate alerts:
        - SELECT expired ONLINE candidates.
        - For each, run UPDATE ... WHERE status=ONLINE AND expires_at<=now.
        - Only alert if rowcount==1 (we won the race).
        - Commit once after all updates.

    Args:
        db: An open SQLAlchemy session (production or test).
        alert_service: The AlertService instance to invoke for DOWN alerts.

    Returns:
        The number of monitors transitioned to DOWN in this cycle.
    """
    now = datetime.now(timezone.utc)
    transitioned = 0

    # Fetch candidates with a lightweight SELECT so we have the data needed
    # for the alert (email, last_heartbeat, timeout) before issuing the UPDATE.
    # We only look at ONLINE monitors whose expiry is in the past.
    candidates = (
        db.query(Monitor)
        .filter(
            Monitor.status == MonitorStatus.ONLINE,
            Monitor.expires_at <= now,
        )
        .all()
    )

    for monitor in candidates:
        # Atomic conditional UPDATE — succeeds only if the row is still ONLINE
        # and still expired at the moment the DB evaluates the WHERE clause.
        result = db.execute(
            update(Monitor)
            .where(
                Monitor.id == monitor.id,
                Monitor.status == MonitorStatus.ONLINE,
                Monitor.expires_at <= now,
            )
            .values(status=MonitorStatus.DOWN)
            .execution_options(synchronize_session=False)
        )

        if result.rowcount == 1:
            # We won the race — this process is responsible for the alert.
            transitioned += 1

            # Snapshot values now — the ORM object may become stale after commit
            monitor_id = monitor.id
            monitor_email = monitor.alert_email
            monitor_last_hb = monitor.last_heartbeat
            monitor_timeout = monitor.timeout

            logger.warning(
                "Monitor '%s' has expired (last_heartbeat=%s, timeout=%ds). "
                "Transitioning ONLINE → DOWN.",
                monitor_id,
                monitor_last_hb.isoformat(),
                monitor_timeout,
            )

            # Schedule the alert coroutine. If an event loop is running (normal
            # production path inside the async watchdog task), use ensure_future.
            # If called from asyncio.run() in tests, we need to handle both cases.
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(
                    alert_service.send_down_alert(
                        monitor_id=monitor_id,
                        alert_email=monitor_email,
                        last_heartbeat=monitor_last_hb,
                        timeout_seconds=monitor_timeout,
                    )
                )
            except RuntimeError:
                # No running event loop — called from a sync context in tests.
                # Run the coroutine to completion synchronously.
                asyncio.run(
                    alert_service.send_down_alert(
                        monitor_id=monitor_id,
                        alert_email=monitor_email,
                        last_heartbeat=monitor_last_hb,
                        timeout_seconds=monitor_timeout,
                    )
                )

    if candidates:
        db.commit()

    return transitioned


# Keep old name as alias for backward compatibility with any import that used it
_evaluate_expired_monitors = evaluate_expired_monitors


class TimerService:
    """Asynchronous background watchdog engine.

    Lifecycle:
        start() → creates a background asyncio.Task that loops forever.
        stop()  → signals the loop to exit cleanly; awaits shutdown.

    The loop is a single task, NOT one per device. This scales to thousands
    of devices without creating thousands of asyncio tasks.

    Usage (in FastAPI lifespan):
        await timer_service.start()
        yield  # FastAPI serves requests
        await timer_service.stop()
    """

    def __init__(self, check_interval_seconds: int = DEFAULT_CHECK_INTERVAL) -> None:
        self.check_interval_seconds = check_interval_seconds
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._alert_service: "AlertService | None" = None

    @property
    def alert_service(self) -> "AlertService":
        """Lazy import of AlertService to avoid circular dependency at module load."""
        if self._alert_service is None:
            from app.services.alert_service import alert_service
            self._alert_service = alert_service
        return self._alert_service

    def is_running(self) -> bool:
        """Returns True if the background watchdog task is currently active."""
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Starts the background watchdog evaluation loop.

        Idempotent — calling start() when already running is a no-op.
        """
        if self.is_running():
            logger.debug("TimerService already running — start() is a no-op.")
            return

        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(
            self._evaluation_loop(),
            name="watchdog-timer",
        )
        logger.info(
            "TimerService started (check_interval=%ds).",
            self.check_interval_seconds,
        )

    async def stop(self) -> None:
        """Signals the loop to stop and waits for clean shutdown."""
        if not self.is_running():
            logger.debug("TimerService not running — stop() is a no-op.")
            return

        logger.info("TimerService stopping…")
        if self._stop_event:
            self._stop_event.set()

        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
                logger.warning("TimerService task did not stop cleanly; cancelled.")

        self._task = None
        logger.info("TimerService stopped.")

    async def _evaluation_loop(self) -> None:
        """Main background loop: sleeps, then evaluates, until stopped.

        Uses asyncio.wait_for with the stop_event so the sleep is interruptible
        — when stop() sets the event, the loop exits without waiting for the
        full interval.
        """
        logger.info("Watchdog evaluation loop active.")
        while True:
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),  # type: ignore[union-attr]
                    timeout=self.check_interval_seconds,
                )
                # stop_event was set → exit loop
                logger.info("Watchdog loop received stop signal. Exiting.")
                break
            except asyncio.TimeoutError:
                # Normal path: interval elapsed, run one evaluation tick
                pass

            await self._run_evaluation_tick()

    async def _run_evaluation_tick(self) -> None:
        """Performs one database evaluation cycle inside a managed session."""
        db = SessionLocal()
        try:
            count = evaluate_expired_monitors(db, self.alert_service)
            if count:
                logger.info("Watchdog tick: %d monitor(s) transitioned to DOWN.", count)
            else:
                logger.debug("Watchdog tick: no expired monitors found.")
        except Exception as exc:
            logger.error("Watchdog evaluation error: %s", exc, exc_info=True)
            db.rollback()
        finally:
            db.close()

    async def run_once(self) -> int:
        """Executes a single evaluation cycle immediately using the production DB.

        Primarily used by health-check endpoints and integration smoke tests.
        Unit tests should call evaluate_expired_monitors() directly with an
        injected test session instead.
        """
        db = SessionLocal()
        try:
            count = evaluate_expired_monitors(db, self.alert_service)
            return count
        except Exception as exc:
            logger.error("run_once evaluation error: %s", exc, exc_info=True)
            db.rollback()
            return 0
        finally:
            db.close()


# Singleton exported to main.py and tests
timer_service = TimerService()
