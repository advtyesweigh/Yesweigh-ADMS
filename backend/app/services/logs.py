from __future__ import annotations

from datetime import datetime
from typing import Optional

from app.db import database as db


async def log_comm(
    device_serial: str,
    remote_ip: str,
    listen_port: int,
    method: str,
    endpoint: str,
    category: str,
    status_code: int,
    duration_ms: int,
    error: str = "",
    detail: str = "",
) -> None:
    await db.execute(
        """INSERT INTO comm_logs
           (device_serial, remote_ip, listen_port, method, endpoint, category, status_code, duration_ms, error, detail)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            device_serial or "",
            remote_ip or "",
            listen_port,
            method,
            endpoint,
            category,
            status_code,
            duration_ms,
            (error or "")[:500],
            (detail or "")[:500],
        ),
    )


async def recent(limit: int = 100) -> list[dict]:
    rows = await db.fetchall("SELECT * FROM comm_logs ORDER BY id DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]


def _parse_created(raw: str | None) -> Optional[datetime]:
    if not raw:
        return None
    text = str(raw).strip().replace("Z", "+00:00")
    # SQLite datetime('now') is often "YYYY-MM-DD HH:MM:SS" (UTC, no tz)
    try:
        if "T" in text or "+" in text or text.endswith("Z"):
            return datetime.fromisoformat(text)
        return datetime.fromisoformat(text.replace(" ", "T"))
    except ValueError:
        return None


async def poll_metrics(device_serial: str, *, sample: int = 40) -> dict:
    """Recent getrequest/handshake intervals + HTTP error counts for soak monitoring."""
    sn = (device_serial or "").strip()
    if not sn:
        return {"serial": "", "samples": 0}
    n = max(5, min(int(sample), 200))
    rows = await db.fetchall(
        """SELECT created_at, category, status_code, remote_ip, duration_ms, detail
           FROM comm_logs
           WHERE device_serial=?
             AND category IN ('poll', 'handshake', 'command', 'attlog', 'operlog')
             AND method != 'WATCHDOG'
             AND remote_ip NOT IN ('', '127.0.0.1', '::1', '103.176.85.102')
           ORDER BY id DESC LIMIT ?""",
        (sn, n),
    )
    events = [dict(r) for r in rows]
    events_chrono = list(reversed(events))
    intervals: list[float] = []
    prev: Optional[datetime] = None
    for ev in events_chrono:
        ts = _parse_created(ev.get("created_at"))
        if ts is None:
            continue
        if prev is not None:
            gap = (ts - prev).total_seconds()
            if 0 < gap < 3600:
                intervals.append(gap)
        prev = ts
    err_rows = await db.fetchall(
        """SELECT status_code, count(*) AS n
           FROM comm_logs
           WHERE device_serial=? AND status_code >= 400
           GROUP BY status_code
           ORDER BY status_code""",
        (sn,),
    )
    last = events[0] if events else None
    median = None
    mean = None
    if intervals:
        ordered = sorted(intervals)
        mid = len(ordered) // 2
        median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
        mean = sum(intervals) / len(intervals)
    return {
        "serial": sn,
        "samples": len(events),
        "last_event_at": (last or {}).get("created_at"),
        "last_category": (last or {}).get("category"),
        "last_remote_ip": (last or {}).get("remote_ip"),
        "last_status_code": (last or {}).get("status_code"),
        "interval_seconds": {
            "count": len(intervals),
            "median": round(median, 1) if median is not None else None,
            "mean": round(mean, 1) if mean is not None else None,
            "min": round(min(intervals), 1) if intervals else None,
            "max": round(max(intervals), 1) if intervals else None,
            "recent": [round(x, 1) for x in intervals[-12:]],
        },
        "http_errors_by_status": {str(r["status_code"]): int(r["n"]) for r in err_rows},
    }
