"""Pydantic schemas for request validation and response serialization."""

import re
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import MonitorStatus

EMAIL_REGEX = re.compile(
    r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$"
)


class MonitorCreate(BaseModel):
    """Schema for creating/registering a new device monitor."""

    id: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Unique device identifier (letters, numbers, dashes, underscores, dots, colons)",
        examples=["device-123"],
    )
    timeout: int = Field(
        ...,
        gt=0,
        le=2592000,
        description="Timeout duration in seconds (must be > 0 and <= 30 days)",
        examples=[60],
    )
    alert_email: str = Field(
        ...,
        min_length=3,
        max_length=255,
        description="Administrator email address to alert if heartbeats stop",
        examples=["admin@critmon.com"],
    )

    @field_validator("id")
    @classmethod
    def validate_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Monitor ID cannot be empty or whitespace only")
        if not re.match(r"^[a-zA-Z0-9_\-\.:]+$", v):
            raise ValueError("Monitor ID must contain only alphanumeric characters, underscores, dashes, dots, or colons")
        return v

    @field_validator("alert_email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        v = v.strip().lower()
        if not EMAIL_REGEX.match(v):
            raise ValueError("Invalid email address format")
        return v


class MonitorResponse(BaseModel):
    """Schema representing monitor data returned to clients."""

    id: str
    timeout: int
    alert_email: str
    status: MonitorStatus
    last_heartbeat: datetime
    expires_at: datetime
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class HeartbeatResponse(BaseModel):
    """Schema for successful heartbeat acknowledgement."""

    message: str = Field(
        default="Heartbeat acknowledged. Expiration deadline updated.",
        description="Informational status message",
    )
    monitor: MonitorResponse


class PauseResponse(BaseModel):
    """Schema returned when a monitor is paused or was already paused.

    Design decision: Repeated pause requests are idempotent and return 200,
    not 409. The client does not need to track whether the device is already
    paused — issuing the pause command is always safe.
    """

    message: str
    monitor: MonitorResponse


class HealthResponse(BaseModel):
    """Schema for system health check status."""

    status: str
    version: str
    database: str
    timestamp: datetime
    watchdog_running: bool = Field(
        default=False,
        description="Whether the background watchdog evaluation loop is active",
    )


class RootResponse(BaseModel):
    """Schema for root API metadata."""

    title: str
    version: str
    description: str
    docs_url: str
