"""Alert dispatch service for Pulse-Check API.

Implements structured console alert logging as the notification mechanism.
Console output is the required delivery method for Part 2.
Email/SMTP delivery is designed as a drop-in extension for Part 3.

Design decision: AlertService is stateless — it does not track which alerts
have been sent. Duplicate-alert prevention is the responsibility of the
TimerService, which only triggers alerts during the ONLINE → DOWN state
transition. By enforcing the state transition exactly once in a database
transaction, we guarantee exactly-once alert delivery.
"""

import json
from datetime import datetime, timezone
import logging

logger = logging.getLogger(__name__)

# Dedicated alert logger that writes structured JSON lines to stdout.
# This makes alert output easy to grep, pipe into log aggregators (Loki,
# CloudWatch, Datadog, etc.) or forward to email in a future phase.
alert_logger = logging.getLogger("pulse_check.alerts")


class AlertService:
    """Administrator notification dispatch service.

    Currently implements structured console (JSON log) alerts.
    The interface is designed so that Part 3 can add email/webhook delivery
    by extending the methods below without changing any call sites.
    """

    async def send_down_alert(
        self,
        monitor_id: str,
        alert_email: str,
        last_heartbeat: datetime,
        timeout_seconds: int,
    ) -> bool:
        """Dispatches a DOWN alert when a monitor is declared DOWN.

        Emits a structured JSON alert log entry and a human-readable warning
        so the event is impossible to miss in any log viewer.

        Args:
            monitor_id: Identifier of the failed monitor.
            alert_email: Recipient administrator email address.
            last_heartbeat: Timestamp of the last received heartbeat.
            timeout_seconds: Configured timeout period in seconds.

        Returns:
            True if the alert was successfully dispatched.
        """
        now = datetime.now(timezone.utc)

        # Structured JSON alert payload — matches the spec example format
        alert_payload = {
            "ALERT": f"Device {monitor_id} is down!",
            "time": now.isoformat(),
            "monitor_id": monitor_id,
            "alert_email": alert_email,
            "last_heartbeat": last_heartbeat.isoformat(),
            "timeout_seconds": timeout_seconds,
            "missed_by_seconds": round(
                (now - last_heartbeat).total_seconds() - timeout_seconds
            ),
        }

        # Emit the structured alert as a JSON line — easy to pipe to email/Slack
        alert_logger.warning(json.dumps(alert_payload))

        # Also emit a human-friendly summary at WARNING level for plain log viewers
        logger.warning(
            "🔴 DEVICE DOWN: '%s' | last seen: %s | timeout: %ds | alert→ %s",
            monitor_id,
            last_heartbeat.isoformat(),
            timeout_seconds,
            alert_email,
        )
        return True

    async def send_recovery_alert(
        self,
        monitor_id: str,
        alert_email: str,
    ) -> bool:
        """Dispatches a recovery notification when a DOWN monitor returns ONLINE.

        Reserved for Part 3 — the interface is defined here so callers exist
        and the extension requires zero refactoring.
        """
        now = datetime.now(timezone.utc)

        recovery_payload = {
            "RECOVERY": f"Device {monitor_id} is back online!",
            "time": now.isoformat(),
            "monitor_id": monitor_id,
            "alert_email": alert_email,
        }
        alert_logger.info(json.dumps(recovery_payload))
        logger.info(
            "🟢 DEVICE RECOVERED: '%s' | alert→ %s",
            monitor_id,
            alert_email,
        )
        return True


# Singleton instance — imported directly by TimerService and routers
alert_service = AlertService()
