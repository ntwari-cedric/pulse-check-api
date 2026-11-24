"""SQLAlchemy models for Pulse-Check API."""

import enum
from datetime import datetime
from sqlalchemy import Enum as SQLEnum, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base, UTCDateTime


class MonitorStatus(str, enum.Enum):
    """Lifecycle statuses for a monitored device."""

    ONLINE = "ONLINE"
    DOWN = "DOWN"
    PAUSED = "PAUSED"


class Monitor(Base):
    """Database model representing a remote device watchdog monitor."""

    __tablename__ = "monitors"

    id: Mapped[str] = mapped_column(
        String(100),
        primary_key=True,
        index=True,
        comment="Unique identifier for the monitored device",
    )
    timeout: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Timeout duration in seconds before declaring the device DOWN",
    )
    alert_email: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Administrator email address to notify on failure",
    )
    status: Mapped[MonitorStatus] = mapped_column(
        SQLEnum(MonitorStatus, name="monitor_status", values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=MonitorStatus.ONLINE,
        index=True,
        comment="Current operational status of the monitor",
    )
    last_heartbeat: Mapped[datetime] = mapped_column(
        UTCDateTime,
        nullable=False,
        comment="Timestamp (UTC) of the most recently received heartbeat",
    )
    expires_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        nullable=False,
        index=True,
        comment="Timestamp (UTC) after which the device is considered expired/DOWN",
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        nullable=False,
        comment="Timestamp (UTC) when the monitor was registered",
    )

    def __repr__(self) -> str:
        return (
            f"<Monitor id={self.id!r} status={self.status.value!r} "
            f"timeout={self.timeout} expires_at={self.expires_at!r}>"
        )
