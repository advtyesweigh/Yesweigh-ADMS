from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.config import get_settings
from app.db import database as db


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


async def get_or_create_pending(serial: str, ip: str = "", port: Optional[int] = None) -> dict:
    row = await db.fetchone("SELECT * FROM devices WHERE serial_number=?", (serial,))
    if row:
        return dict(row)
    status = "approved" if get_settings().auto_approve_devices else "pending"
    await db.execute(
        """INSERT INTO devices (serial_number, name, status, last_ip, last_port, last_heartbeat_at, protocol)
           VALUES (?, ?, ?, ?, ?, ?, 'adms-http')""",
        (serial, serial, status, ip, port, _utcnow()),
    )
    row = await db.fetchone("SELECT * FROM devices WHERE serial_number=?", (serial,))
    return dict(row)


async def touch_heartbeat(serial: str, ip: str = "", port: Optional[int] = None, attendance: bool = False) -> None:
    now = _utcnow()
    if attendance:
        await db.execute(
            """UPDATE devices SET last_heartbeat_at=?, last_attendance_at=?,
               last_ip=COALESCE(NULLIF(?,''), last_ip), last_port=COALESCE(?, last_port), updated_at=?
               WHERE serial_number=?""",
            (now, now, ip, port, now, serial),
        )
    else:
        await db.execute(
            """UPDATE devices SET last_heartbeat_at=?,
               last_ip=COALESCE(NULLIF(?,''), last_ip), last_port=COALESCE(?, last_port), updated_at=?
               WHERE serial_number=?""",
            (now, ip, port, now, serial),
        )


async def bump_records(serial: str, n: int) -> None:
    if n <= 0:
        return
    await db.execute(
        "UPDATE devices SET total_records=total_records+?, updated_at=? WHERE serial_number=?",
        (n, _utcnow(), serial),
    )


async def update_stamp(serial: str, attlog: Optional[str] = None, operlog: Optional[str] = None) -> None:
    if attlog is not None:
        await db.execute(
            "UPDATE devices SET stamp_attlog=?, updated_at=? WHERE serial_number=?",
            (attlog, _utcnow(), serial),
        )
    if operlog is not None:
        await db.execute(
            "UPDATE devices SET stamp_operlog=?, updated_at=? WHERE serial_number=?",
            (operlog, _utcnow(), serial),
        )


async def list_devices() -> list[dict]:
    rows = await db.fetchall("SELECT * FROM devices ORDER BY updated_at DESC")
    offline_after = get_settings().device_offline_seconds
    now = datetime.now(timezone.utc)
    out = []
    for r in rows:
        d = dict(r)
        hb = d.get("last_heartbeat_at")
        online = False
        if hb and d.get("status") != "disabled":
            try:
                ts = datetime.fromisoformat(hb.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                online = (now - ts) <= timedelta(seconds=offline_after)
            except ValueError:
                online = False
        d["online"] = online
        out.append(d)
    return out


async def get_device(serial: str) -> Optional[dict]:
    row = await db.fetchone("SELECT * FROM devices WHERE serial_number=?", (serial,))
    return dict(row) if row else None


async def update_device(serial: str, **fields: Any) -> Optional[dict]:
    allowed = {
        "name",
        "model",
        "firmware",
        "branch",
        "timezone",
        "status",
        "userinfo_supported",
        "last_userinfo_at",
    }
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed and v is not None:
            sets.append(f"{k}=?")
            vals.append(v)
    if not sets:
        return await get_device(serial)
    sets.append("updated_at=?")
    vals.append(_utcnow())
    vals.append(serial)
    await db.execute(f"UPDATE devices SET {', '.join(sets)} WHERE serial_number=?", vals)
    return await get_device(serial)


async def mark_userinfo_received(serial: str, *, named: bool) -> None:
    """Record that the device uploaded USERINFO/OPERLOG user rows."""
    now = _utcnow()
    support = "yes" if named else "yes"  # upload path exists even if names blank
    await db.execute(
        """UPDATE devices
           SET last_userinfo_at=?, userinfo_supported=?, updated_at=?
           WHERE serial_number=?""",
        (now, support, now, serial),
    )


async def counts() -> dict:
    devices = await list_devices()
    return {
        "total": len(devices),
        "online": sum(1 for d in devices if d.get("online")),
        "offline": sum(1 for d in devices if not d.get("online") and d.get("status") != "disabled"),
        "pending_approval": sum(1 for d in devices if d.get("status") == "pending"),
    }
