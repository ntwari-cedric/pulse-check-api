"""Main FastAPI application entry point for Pulse-Check API (Watchdog Sentinel)."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import logging
from fastapi import Depends, FastAPI, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import __version__
from app.database import get_db, init_db
from app.routers import monitors
from app.schemas import HealthResponse, RootResponse
from app.services.timer_service import timer_service

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("pulse_check")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager — runs startup before yield, shutdown after.

    Startup order:
        1. Create/verify database schema via init_db().
        2. Start the background watchdog TimerService.

    Shutdown order:
        3. Signal the watchdog to stop and await clean exit.

    Why this order? The DB must exist before the watchdog can query it.
    The watchdog must be started after DB init so the first tick doesn't
    race against table creation.
    """
    logger.info("Initializing database schema…")
    init_db()
    logger.info("Database schema ready.")

    logger.info("Starting watchdog TimerService…")
    await timer_service.start()
    logger.info("Pulse-Check API v%s ready. Watchdog active.", __version__)

    yield  # FastAPI serves requests here

    logger.info("Shutting down — stopping TimerService…")
    await timer_service.stop()
    logger.info("Pulse-Check API shut down cleanly.")


app = FastAPI(
    title="Pulse-Check API",
    version=__version__,
    description=(
        "Production-minded Dead Man's Switch API for remote device watchdog monitoring. "
        "Automatically detects devices that stop sending heartbeats and raises alerts."
    ),
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

# Register routers
app.include_router(monitors.router)


@app.get(
    "/",
    response_model=RootResponse,
    status_code=status.HTTP_200_OK,
    tags=["General"],
    summary="Root metadata endpoint",
)
def root() -> RootResponse:
    """Returns general metadata about the Pulse-Check API."""
    return RootResponse(
        title="Pulse-Check API",
        version=__version__,
        description="Dead Man's Switch API for remote device watchdog monitoring (CritMon Servers Inc.)",
        docs_url="/docs",
    )


@app.get(
    "/health",
    response_model=HealthResponse,
    status_code=status.HTTP_200_OK,
    tags=["General"],
    summary="Health check endpoint",
)
def health_check(db: Session = Depends(get_db)) -> HealthResponse:
    """Performs a live system and database connectivity check.

    Now also reports whether the background watchdog is active via
    watchdog_running in the response body.
    """
    try:
        db.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception as exc:
        logger.error("Database health check failed: %s", exc)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "unhealthy",
                "version": __version__,
                "database": "disconnected",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "watchdog_running": timer_service.is_running(),
            },
        )

    return HealthResponse(
        status="healthy",
        version=__version__,
        database=db_status,
        timestamp=datetime.now(timezone.utc),
        watchdog_running=timer_service.is_running(),
    )
