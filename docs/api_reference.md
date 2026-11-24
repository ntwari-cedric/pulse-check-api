# Pulse-Check API — Full API Reference (Complete)

> Base URL: `http://localhost:8000`

---

## Endpoints Overview

| Method | Path | Status Codes | Part |
|--------|------|-------------|------|
| `POST` | `/monitors` | 201, 409, 422 | 1 |
| `POST` | `/monitors/{id}/heartbeat` | 200, 404, 409 | 1 |
| `POST` | `/monitors/{id}/pause` | 200, 404, 409 | 2 |
| `GET` | `/monitors/{id}` | 200, 404 | 1 |
| `GET` | `/monitors` | 200 | 1 |
| `GET` | `/health` | 200, 503 | 1/2 |
| `GET` | `/` | 200 | 1 |

---

## POST `/monitors`

Registers a new remote device for watchdog monitoring.

### Request Body
```json
{
  "id": "device-123",
  "timeout": 60,
  "alert_email": "admin@critmon.com"
}
```

| Field | Type | Constraints |
|-------|------|-------------|
| `id` | string | 1–100 chars, alphanumeric + `-_.:` |
| `timeout` | integer | 1 to 2,592,000 seconds (max 30 days) |
| `alert_email` | string | Valid email format |

### Response `201 Created`
```json
{
  "id": "device-123",
  "timeout": 60,
  "alert_email": "admin@critmon.com",
  "status": "ONLINE",
  "last_heartbeat": "2026-11-24T12:00:00+00:00",
  "expires_at": "2026-11-24T12:01:00+00:00",
  "created_at": "2026-11-24T12:00:00+00:00"
}
```

### Errors
| Code | Reason |
|------|--------|
| `409` | Monitor with this ID already registered |
| `422` | Validation failure (invalid timeout, bad email, empty ID, etc.) |

---

## POST `/monitors/{id}/heartbeat`

Records a heartbeat for an active device. Resets `expires_at` to `now + timeout`.

**Bonus:** Automatically resumes a `PAUSED` monitor to `ONLINE`.

### Response `200 OK`
```json
{
  "message": "Heartbeat acknowledged for device 'device-123'. Expiration extended to 2026-11-24T12:02:30+00:00.",
  "monitor": {
    "id": "device-123",
    "timeout": 60,
    "alert_email": "admin@critmon.com",
    "status": "ONLINE",
    "last_heartbeat": "2026-11-24T12:01:30+00:00",
    "expires_at": "2026-11-24T12:02:30+00:00",
    "created_at": "2026-11-24T12:00:00+00:00"
  }
}
```

### Errors
| Code | Reason |
|------|--------|
| `404` | Monitor ID not found |
| `409` | Monitor is `DOWN` — heartbeat rejected, explicit recovery required |

---

## POST `/monitors/{id}/pause`

Pauses watchdog monitoring for a device. No expiry transitions or alerts are
generated while the device is `PAUSED`.

**Idempotent:** Safe to call multiple times. Returns `200` even if already `PAUSED`.

**Resume:** Send a heartbeat to automatically transition `PAUSED → ONLINE`.

### Response `200 OK`
```json
{
  "message": "Monitor 'device-123' has been paused. No alerts will be generated until resumed.",
  "monitor": {
    "id": "device-123",
    "timeout": 60,
    "alert_email": "admin@critmon.com",
    "status": "PAUSED",
    "last_heartbeat": "2026-11-24T12:00:00+00:00",
    "expires_at": "2026-11-24T12:01:00+00:00",
    "created_at": "2026-11-24T12:00:00+00:00"
  }
}
```

### Errors
| Code | Reason |
|------|--------|
| `404` | Monitor ID not found |
| `409` | Monitor is already `DOWN` — cannot be paused |

---

## GET `/monitors/{id}`

Retrieves the complete current state of a specific monitor.

**Why this endpoint:** Allows administrators and scripts to check a single
device's health without paginating through the full list. Essential for
dashboards, alerting scripts, and debugging.

### Response `200 OK`
```json
{
  "id": "device-123",
  "timeout": 60,
  "alert_email": "admin@critmon.com",
  "status": "ONLINE",
  "last_heartbeat": "2026-11-24T12:00:00+00:00",
  "expires_at": "2026-11-24T12:01:00+00:00",
  "created_at": "2026-11-24T11:59:00+00:00"
}
```

### Errors
| Code | Reason |
|------|--------|
| `404` | Monitor ID not found |

---

## GET `/monitors`

Lists all registered monitors with optional filtering and pagination.

### Query Parameters
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `status` | enum | — | Filter: `ONLINE`, `DOWN`, or `PAUSED` |
| `skip` | int | 0 | Pagination offset |
| `limit` | int | 100 | Max results (1–500) |

### Response `200 OK`
```json
[
  {
    "id": "device-123",
    "timeout": 60,
    "status": "ONLINE",
    ...
  }
]
```

---

## GET `/health`

Live system and database health check. Also reports whether the background
watchdog engine is currently active.

### Response `200 OK`
```json
{
  "status": "healthy",
  "version": "1.0.0",
  "database": "connected",
  "timestamp": "2026-11-24T12:00:00+00:00",
  "watchdog_running": true
}
```

### Response `503 Service Unavailable`
```json
{
  "status": "unhealthy",
  "version": "1.0.0",
  "database": "disconnected",
  "timestamp": "2026-11-24T12:00:00+00:00",
  "watchdog_running": false
}
```

---

## GET `/`

Returns API metadata.

### Response `200 OK`
```json
{
  "title": "Pulse-Check API",
  "version": "1.0.0",
  "description": "Dead Man's Switch API for remote device watchdog monitoring (CritMon Servers Inc.)",
  "docs_url": "/docs"
}
```

---

## Status Transitions

```
ONLINE ──(watchdog: expires_at <= now)──▶ DOWN
ONLINE ──(POST /pause)───────────────── ▶ PAUSED
PAUSED ──(POST /heartbeat)───────────── ▶ ONLINE
DOWN   ──(explicit recovery, Part 3)─── ▶ ONLINE  [not yet implemented]
```

---

## Alert Log Format

When a monitor transitions `ONLINE → DOWN`, the `pulse_check.alerts` logger emits:

```json
{
  "ALERT": "Device device-123 is down!",
  "time": "2026-11-24T12:01:30+00:00",
  "monitor_id": "device-123",
  "alert_email": "admin@critmon.com",
  "last_heartbeat": "2026-11-24T12:00:00+00:00",
  "timeout_seconds": 60,
  "missed_by_seconds": 90
}
```

A human-readable warning is also emitted:
```
🔴 DEVICE DOWN: 'device-123' | last seen: 2026-11-24T12:00:00+00:00 | timeout: 60s | alert→ admin@critmon.com
```

---

## curl Examples

```bash
# Register a monitor
curl -s -X POST http://localhost:8000/monitors \
  -H "Content-Type: application/json" \
  -d '{"id":"device-123","timeout":60,"alert_email":"admin@critmon.com"}' | python -m json.tool

# Send a heartbeat
curl -s -X POST http://localhost:8000/monitors/device-123/heartbeat | python -m json.tool

# Pause monitoring
curl -s -X POST http://localhost:8000/monitors/device-123/pause | python -m json.tool

# Inspect a device
curl -s http://localhost:8000/monitors/device-123 | python -m json.tool

# List all DOWN monitors
curl -s "http://localhost:8000/monitors?status=DOWN" | python -m json.tool

# Health check
curl -s http://localhost:8000/health | python -m json.tool
```
