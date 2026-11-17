from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.adms.protocol import AttendancePunch, punch_label, verify_label
from app.db import database as db

logger = logging.getLogger("adms.attendance")

# Continuous/immediate re-punches within this window are voided (not check-out).
DUPLICATE_WINDOW_SECONDS = 60


def _parse_punch_time(raw: str) -> Optional[datetime]:
    text = (raw or "").strip()[:19]
    if len(text) < 19:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


async def _recent_non_voided(
    *,
    employee_code: str,
    device_serial: str,
    punch_time: str,
    within_seconds: int = DUPLICATE_WINDOW_SECONDS,
) -> Optional[dict]:
    """Return the latest non-voided punch for this person within the duplicate window."""
    ts = _parse_punch_time(punch_time)
    if ts is None:
        return None
    # Look back a few minutes of wall-clock strings (device local time, no TZ).
    window_start = (ts - timedelta(seconds=max(1, within_seconds))).strftime("%Y-%m-%d %H:%M:%S")
    row = await db.fetchone(
        """SELECT id, event_id, punch_time FROM attendance
           WHERE employee_code=? AND device_serial=?
             AND firebase_status != 'voided'
             AND punch_time >= ? AND punch_time <= ?
           ORDER BY punch_time DESC, id DESC LIMIT 1""",
        (employee_code, device_serial, window_start, punch_time),
    )
    if not row:
        return None
    prev = _parse_punch_time(str(row["punch_time"]))
    if prev is None:
        return None
    delta = abs((ts - prev).total_seconds())
    if 0 <= delta <= within_seconds:
        return dict(row)
    return None


async def save_punch(
    *,
    event_id: str,
    punch: AttendancePunch,
    device_serial: str,
    device_name: str,
    branch: str,
) -> bool:
    """Insert attendance if new. Returns True if inserted (non-voided).

    Order: SQLite local mirror → Postgres durable store (when DATABASE_URL set).
    Postgres must succeed before the caller ACKs the device. Firebase is never
    awaited here (async sync_loop only).

    Immediate re-punches within DUPLICATE_WINDOW_SECONDS for the same employee+device
    are stored as firebase_status='voided' so they never become check-out / inflate hours.
    """
    existing = await db.fetchone(
        "SELECT id, firebase_status FROM attendance WHERE event_id=?",
        (event_id,),
    )
    if existing:
        # Still ensure Postgres has the row (recovery / prior SQLite-only insert).
        await _mirror_postgres(
            event_id=event_id,
            punch=punch,
            device_serial=device_serial,
            device_name=device_name,
            branch=branch,
            firebase_status=str(existing["firebase_status"] or "pending"),
        )
        return False

    dup = await _recent_non_voided(
        employee_code=punch.employee_code,
        device_serial=device_serial,
        punch_time=punch.punch_time,
    )
    status = "voided" if dup else "pending"
    await db.execute(
        """INSERT INTO attendance
           (event_id, employee_code, device_serial, device_name, branch, punch_time, punch_date,
            punch_status, verify_method, work_code, raw_line, firebase_status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            status,
        ),
    )
    await _mirror_postgres(
        event_id=event_id,
        punch=punch,
        device_serial=device_serial,
        device_name=device_name,
        branch=branch,
        firebase_status=status,
    )
    if dup:
        logger.info(
            "voided duplicate punch emp=%s device=%s time=%s within %ss of id=%s",
            punch.employee_code,
            device_serial,
            punch.punch_time,
            DUPLICATE_WINDOW_SECONDS,
            dup.get("id"),
        )
        return False
    return True


async def _mirror_postgres(
    *,
    event_id: str,
    punch: AttendancePunch,
    device_serial: str,
    device_name: str,
    branch: str,
    firebase_status: str,
) -> None:
    from app.config import get_settings
    from app.db import postgres as pg

    if not (get_settings().database_url or "").strip():
        return
    await pg.upsert_attendance(
        {
            "event_id": event_id,
            "employee_code": punch.employee_code,
            "device_serial": device_serial,
            "device_name": device_name,
            "branch": branch,
            "punch_time": punch.punch_time,
            "punch_date": punch.punch_date,
            "punch_status": punch.punch_status,
            "verify_method": punch.verify_method,
            "work_code": punch.work_code,
            "punch_label": punch_label(punch.punch_status or "0"),
            "verify_label": verify_label(punch.verify_method or "1"),
            "firebase_status": firebase_status,
            "raw_line": punch.raw_line,
        }
    )


async def void_punch_by_id(punch_id: int, *, reason: str = "duplicate_within_60s") -> bool:
    row = await db.fetchone("SELECT id FROM attendance WHERE id=?", (punch_id,))
    if not row:
        return False
    await db.execute(
        """UPDATE attendance SET firebase_status='voided',
           firebase_last_error=?, firebase_attempts=firebase_attempts
           WHERE id=?""",
        (reason[:500], punch_id),
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
    # Prefer exact device match, then blank-serial row, then any name.
    # Avoid ORDER BY outer refs (a.device_serial) — SQLite 3.37 rejects that in subqueries.
    rows = await db.fetchall(
        f"""SELECT a.*,
                   COALESCE(
                     (SELECT e.name FROM employees e
                      WHERE e.employee_code = a.employee_code
                        AND e.device_serial = a.device_serial
                        AND IFNULL(e.name,'') <> ''
                      LIMIT 1),
                     (SELECT e2.name FROM employees e2
                      WHERE e2.employee_code = a.employee_code
                        AND e2.device_serial = ''
                        AND IFNULL(e2.name,'') <> ''
                      LIMIT 1),
                     (SELECT e3.name FROM employees e3
                      WHERE e3.employee_code = a.employee_code
                        AND IFNULL(e3.name,'') <> ''
                      LIMIT 1),
                     ''
                   ) AS employee_name
            FROM attendance a
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
        """SELECT a.*,
                  COALESCE(
                    (SELECT e.name FROM employees e
                     WHERE e.employee_code = a.employee_code
                       AND e.device_serial = a.device_serial
                       AND IFNULL(e.name,'') <> ''
                     LIMIT 1),
                    (SELECT e2.name FROM employees e2
                     WHERE e2.employee_code = a.employee_code
                       AND e2.device_serial = ''
                       AND IFNULL(e2.name,'') <> ''
                     LIMIT 1),
                    (SELECT e3.name FROM employees e3
                     WHERE e3.employee_code = a.employee_code
                       AND IFNULL(e3.name,'') <> ''
                     LIMIT 1),
                    ''
                  ) AS employee_name
           FROM attendance a
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
