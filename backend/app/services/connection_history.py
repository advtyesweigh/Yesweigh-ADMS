"""Device connection history (heartbeat, attendance, disconnect/reconnect, errors)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from app.db import database as db


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


async def record_event(
    serial: str,
    event_type: str,
    *,
    remote_ip: str = "",
    detail: str = "",
) -> None:
    if not serial:
        return
    now = _utcnow()
    await db.execute(
        """INSERT INTO connection_events (created_at, device_serial, event_type, remote_ip, detail)
           VALUES (?, ?, ?, ?, ?)""",
        (now, serial, event_type, remote_ip or "", (detail or "")[:500]),
    )
    if event_type == "disconnect":
        await db.execute(
            "UPDATE devices SET last_disconnect_at=?, updated_at=? WHERE serial_number=?",
            (now, now, serial),
        )
    elif event_type == "reconnect":
        await db.execute(
            "UPDATE devices SET last_reconnect_at=?, updated_at=? WHERE serial_number=?",
            (now, now, serial),
        )
    elif event_type == "server_error":
        await db.execute(
            """UPDATE devices SET last_server_error_at=?, last_server_error=?, updated_at=?
               WHERE serial_number=?""",
            (now, (detail or "")[:500], now, serial),
        )


async def list_events(serial: str, *, limit: int = 100) -> list[dict]:
    rows = await db.fetchall(
        """SELECT * FROM connection_events
           WHERE device_serial=?
           ORDER BY id DESC LIMIT ?""",
        (serial, max(1, min(int(limit), 500))),
    )
    return [dict(r) for r in rows]


async def summary_for(serial: str) -> dict:
    device = await db.fetchone("SELECT * FROM devices WHERE serial_number=?", (serial,))
    d = dict(device) if device else {}
    recent = await list_events(serial, limit=20)
    last_hb = await db.fetchone(
        """SELECT created_at, remote_ip, detail FROM connection_events
           WHERE device_serial=? AND event_type='heartbeat'
           ORDER BY id DESC LIMIT 1""",
        (serial,),
    )
    last_att = await db.fetchone(
        """SELECT created_at, remote_ip, detail FROM connection_events
           WHERE device_serial=? AND event_type='attendance'
           ORDER BY id DESC LIMIT 1""",
        (serial,),
    )
    return {
        "serial": serial,
        "last_heartbeat_at": d.get("last_heartbeat_at"),
        "last_attendance_at": d.get("last_attendance_at"),
        "last_disconnect_at": d.get("last_disconnect_at"),
        "last_reconnect_at": d.get("last_reconnect_at"),
        "last_server_error_at": d.get("last_server_error_at"),
        "last_server_error": d.get("last_server_error") or "",
        "last_heartbeat_event": dict(last_hb) if last_hb else None,
        "last_attendance_event": dict(last_att) if last_att else None,
        "recent_events": recent,
    }
