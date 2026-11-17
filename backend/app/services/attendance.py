from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from app.adms.protocol import AttendancePunch, punch_label, verify_label
from app.db import database as db


async def save_punch(
    *,
    event_id: str,
    punch: AttendancePunch,
    device_serial: str,
    device_name: str,
    branch: str,
) -> bool:
    """Insert attendance if new. Returns True if inserted."""
    existing = await db.fetchone("SELECT id FROM attendance WHERE event_id=?", (event_id,))
    if existing:
        return False
    await db.execute(
        """INSERT INTO attendance
           (event_id, employee_code, device_serial, device_name, branch, punch_time, punch_date,
            punch_status, verify_method, work_code, raw_line, firebase_status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
        (
            event_id,
            punch.employee_code,
            device_serial,
            device_name,
            branch,
            punch.punch_time,
            punch.punch_date,
            punch.punch_status,
            punch.verify_method,
            punch.work_code,
            punch.raw_line,
        ),
    )
    return True


def _decorate_row(r) -> dict:
    d = dict(r)
    d["punch_label"] = punch_label(d.get("punch_status") or "0")
    d["verify_label"] = verify_label(d.get("verify_method") or "1")
    return d


async def list_attendance(
    *,
    limit: int = 100,
    offset: int = 0,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    employee: Optional[str] = None,
    device: Optional[str] = None,
    branch: Optional[str] = None,
    sync_status: Optional[str] = None,
) -> list[dict]:
    clauses = ["1=1"]
    params: list = []
    if date_from:
        clauses.append("punch_date >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("punch_date <= ?")
        params.append(date_to)
    if employee:
        clauses.append("employee_code = ?")
        params.append(employee)
    if device:
        clauses.append("device_serial = ?")
        params.append(device)
    if branch:
        clauses.append("branch = ?")
        params.append(branch)
    if sync_status:
        clauses.append("firebase_status = ?")
        params.append(sync_status)
    params.extend([limit, offset])
    rows = await db.fetchall(
        f"""SELECT a.*, e.name AS employee_name FROM attendance a
            LEFT JOIN employees e ON e.employee_code = a.employee_code
            WHERE {' AND '.join(clauses)}
            ORDER BY a.punch_time DESC, a.id DESC LIMIT ? OFFSET ?""",
        params,
    )
    return [_decorate_row(r) for r in rows]


async def export_since(*, after_id: int = 0, limit: int = 500) -> dict:
    """Incremental export for CRM Postgres sync (ascending by SQLite id)."""
    lim = max(1, min(int(limit), 2000))
    after = max(0, int(after_id))
    rows = await db.fetchall(
        """SELECT a.*, e.name AS employee_name FROM attendance a
           LEFT JOIN employees e ON e.employee_code = a.employee_code
           WHERE a.id > ?
           ORDER BY a.id ASC LIMIT ?""",
        (after, lim),
    )
    items = [_decorate_row(r) for r in rows]
    next_after = int(items[-1]["id"]) if items else after
    total_row = await db.fetchone("SELECT COUNT(*) AS c FROM attendance")
    return {
        "items": items,
        "after_id": after,
        "next_after_id": next_after,
        "count": len(items),
        "total": int(total_row["c"]) if total_row else 0,
        "has_more": len(items) == lim,
    }


async def sync_counts() -> dict:
    pending = await db.fetchone("SELECT COUNT(*) AS c FROM attendance WHERE firebase_status='pending'")
    failed = await db.fetchone("SELECT COUNT(*) AS c FROM attendance WHERE firebase_status='failed'")
    synced = await db.fetchone("SELECT COUNT(*) AS c FROM attendance WHERE firebase_status='synced'")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Asia/Kolkata date approx via local device times stored as punch_date
    from zoneinfo import ZoneInfo

    today_ist = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")
    today_c = await db.fetchone("SELECT COUNT(*) AS c FROM attendance WHERE punch_date=?", (today_ist,))
    last = await db.fetchone("SELECT value FROM sync_meta WHERE key='last_success'")
    return {
        "pending": pending["c"] if pending else 0,
        "failed": failed["c"] if failed else 0,
        "synced": synced["c"] if synced else 0,
        "today": today_c["c"] if today_c else 0,
        "last_success": last["value"] if last else None,
        "today_date": today_ist,
        "utc_date": today,
    }


async def pending_batch(limit: int) -> list[dict]:
    rows = await db.fetchall(
        """SELECT * FROM attendance WHERE firebase_status IN ('pending','failed')
           ORDER BY id ASC LIMIT ?""",
        (limit,),
    )
    return [dict(r) for r in rows]


async def mark_synced(event_id: str) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    await db.execute(
        """UPDATE attendance SET firebase_status='synced', firebase_synced_at=?, firebase_last_error=''
           WHERE event_id=?""",
        (now, event_id),
    )
    await db.execute(
        "INSERT INTO sync_meta(key,value) VALUES('last_success',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (now,),
    )


async def mark_failed(event_id: str, error: str) -> None:
    await db.execute(
        """UPDATE attendance SET firebase_status='failed', firebase_attempts=firebase_attempts+1,
           firebase_last_error=? WHERE event_id=?""",
        (error[:500], event_id),
    )


async def retry_failed(event_id: Optional[str] = None) -> int:
    if event_id:
        await db.execute("UPDATE attendance SET firebase_status='pending' WHERE event_id=? AND firebase_status='failed'", (event_id,))
        return 1
    await db.execute("UPDATE attendance SET firebase_status='pending' WHERE firebase_status='failed'")
    row = await db.fetchone("SELECT changes() AS c")
    return int(row["c"]) if row else 0


async def purge_old_synced(days: int) -> int:
    await db.execute(
        """DELETE FROM attendance WHERE firebase_status='synced'
           AND datetime(firebase_synced_at) < datetime('now', ?)""",
        (f"-{days} days",),
    )
    row = await db.fetchone("SELECT changes() AS c")
    return int(row["c"]) if row else 0
