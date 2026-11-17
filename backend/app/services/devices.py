from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.config import get_settings
from app.db import database as db
from app.services import connection_history as history_svc
from app.services import sessions as sessions_svc


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_ts(raw: str | None) -> Optional[datetime]:
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def compute_connection_status(last_heartbeat_at: str | None, status: str | None = None) -> str:
    """Online / Unstable / Offline from last device request age (not TCP sockets).

    Online ≤ DEVICE_ONLINE_SECONDS (default 45, ~3× Delay=15)
    Unstable between online and offline thresholds (default 45–120)
    Offline > DEVICE_OFFLINE_SECONDS (default 120) or pre-boot stamp
    """
    if status == "disabled":
        return "Offline"
    ts = _parse_ts(last_heartbeat_at)
    if ts is None:
        return "Offline"
    boot = sessions_svc.service_started_at()
    if boot is not None and ts < boot:
        return "Offline"
    settings = get_settings()
    now = datetime.now(timezone.utc)
    age = (now - ts).total_seconds()
    online_after = max(1, int(settings.device_online_seconds))
    offline_after = max(online_after + 1, int(settings.device_offline_seconds))
    if age <= online_after:
        return "Online"
    if age <= offline_after:
        return "Unstable"
    return "Offline"


def compute_online(last_heartbeat_at: str | None, status: str | None = None) -> bool:
    return compute_connection_status(last_heartbeat_at, status) == "Online"


async def get_or_create_pending(serial: str, ip: str = "", port: Optional[int] = None) -> dict:
    row = await db.fetchone("SELECT * FROM devices WHERE serial_number=?", (serial,))
    if row:
        return dict(row)
    status = "approved" if get_settings().auto_approve_devices else "pending"
    # Do not stamp last_heartbeat_at on insert — wait for a real iclock poll.
    await db.execute(
        """INSERT INTO devices (serial_number, name, status, last_ip, last_port, last_heartbeat_at, protocol)
           VALUES (?, ?, ?, ?, ?, NULL, 'adms-http')""",
        (serial, serial, status, ip, port),
    )
    row = await db.fetchone("SELECT * FROM devices WHERE serial_number=?", (serial,))
    return dict(row)


def _is_loopback_or_self(ip: str) -> bool:
    """Local/VPS smoke tests must not rewrite the device's WAN peer IP."""
    ip = (ip or "").strip()
    if not ip:
        return True
    if ip.startswith("127.") or ip == "::1":
        return True
    # Public VPS address used in curl smoke tests
    if ip == "103.176.85.102":
        return True
    return False


async def touch_heartbeat(
    serial: str,
    ip: str = "",
    port: Optional[int] = None,
    attendance: bool = False,
    *,
    source: str = "poll",
) -> None:
    now = _utcnow()
    # Localhost / VPS self smoke must not clear session release or fake Online.
    if _is_loopback_or_self(ip):
        return

    meta = sessions_svc.note_heartbeat(serial, ip=ip or "", port=port)
    if meta.get("reconnected"):
        await history_svc.record_event(
            serial,
            "reconnect",
            remote_ip=ip,
            detail=f"after={meta.get('release_reason') or 'stale'} source={source}",
        )
        # After a stale-session release, ask the device to push today's ATTLOG once (deduped).
        try:
            from zoneinfo import ZoneInfo

            from app.services import commands as commands_svc

            today = datetime.now(ZoneInfo(get_settings().timezone_default or "Asia/Kolkata")).strftime("%Y-%m-%d")
            payload = f"StartTime={today} 00:00:00\tEndTime={today} 23:59:59"
            await commands_svc.enqueue_attlog_recovery(serial, payload)
        except Exception:  # noqa: BLE001
            pass

    ip_for_db = ip or ""
    if attendance:
        await db.execute(
            """UPDATE devices SET last_heartbeat_at=?, last_attendance_at=?,
               last_ip=COALESCE(NULLIF(?,''), last_ip), last_port=COALESCE(?, last_port), updated_at=?
               WHERE serial_number=?""",
            (now, now, ip_for_db, port, now, serial),
        )
        await history_svc.record_event(serial, "attendance", remote_ip=ip, detail=source)
    else:
        await db.execute(
            """UPDATE devices SET last_heartbeat_at=?,
               last_ip=COALESCE(NULLIF(?,''), last_ip), last_port=COALESCE(?, last_port), updated_at=?
               WHERE serial_number=?""",
            (now, ip_for_db, port, now, serial),
        )
        # Heartbeats are high-volume; devices.last_heartbeat_at is the live stamp.
        # Record handshake only so history stays readable.
        if source == "handshake":
            await history_svc.record_event(serial, "heartbeat", remote_ip=ip, detail=source)

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


def _enrich_device(d: dict) -> dict:
    status = compute_connection_status(d.get("last_heartbeat_at"), d.get("status"))
    d["connection_status"] = status
    d["online"] = status == "Online"
    d["unstable"] = status == "Unstable"
    sess = sessions_svc.get_session(str(d.get("serial_number") or ""))
    d["session_released"] = bool(sess.released) if sess else (status == "Offline")
    if sess and sess.released_at:
        d["session_released_at"] = sess.released_at.isoformat()
        d["session_release_reason"] = sess.release_reason
    if sess and sess.last_seen_at:
        d["session_last_seen_at"] = sess.last_seen_at.isoformat()
    # Ages for UI/API
    ts = _parse_ts(d.get("last_heartbeat_at"))
    if ts is not None:
        d["seconds_since_last_seen"] = int((datetime.now(timezone.utc) - ts).total_seconds())
    else:
        d["seconds_since_last_seen"] = None
    d["online_threshold_seconds"] = get_settings().device_online_seconds
    d["offline_threshold_seconds"] = get_settings().device_offline_seconds
    return d


async def list_devices_raw() -> list[dict]:
    rows = await db.fetchall("SELECT * FROM devices ORDER BY updated_at DESC")
    return [dict(r) for r in rows]


async def list_devices() -> list[dict]:
    return [_enrich_device(d) for d in await list_devices_raw()]


async def get_device(serial: str) -> Optional[dict]:
    row = await db.fetchone("SELECT * FROM devices WHERE serial_number=?", (serial,))
    if not row:
        return None
    return _enrich_device(dict(row))


async def release_device_connection(serial: str, *, reason: str = "manual") -> Optional[dict]:
    """Mark offline session released and drop stale TCP so the device can re-poll."""
    device = await get_device(serial)
    if not device:
        return None
    settings = get_settings()
    result = sessions_svc.release_session(
        serial,
        reason=reason,
        close_tcp=True,
        peer_ip=str(device.get("last_ip") or ""),
        local_port=settings.port,
    )
    await history_svc.record_event(
        serial,
        "disconnect",
        remote_ip=str(device.get("last_ip") or ""),
        detail=f"reason={reason}",
    )
    device = await get_device(serial)
    assert device
    device["session_released"] = True
    device["release"] = result
    return device


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
        "online": sum(1 for d in devices if d.get("connection_status") == "Online"),
        "unstable": sum(1 for d in devices if d.get("connection_status") == "Unstable"),
        "offline": sum(1 for d in devices if d.get("connection_status") == "Offline" and d.get("status") != "disabled"),
        "pending_approval": sum(1 for d in devices if d.get("status") == "pending"),
    }
