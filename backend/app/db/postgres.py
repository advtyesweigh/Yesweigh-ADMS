"""Optional Postgres durable store for attendance (save-before-ack).

Uses DATABASE_URL when set. Never blocks Firebase. Duplicate-safe via event_id UNIQUE.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from app.config import get_settings

logger = logging.getLogger("adms.postgres")

_pool = None
_init_attempted = False
_last_error: str = ""


def postgres_status() -> dict[str, Any]:
    settings = get_settings()
    url = (settings.database_url or "").strip()
    return {
        "configured": bool(url),
        "ready": _pool is not None,
        "last_error": _last_error,
        "dsn_set": bool(url),
    }


async def init_postgres() -> bool:
    """Create pool + attendance table when DATABASE_URL is set."""
    global _pool, _init_attempted, _last_error
    settings = get_settings()
    url = (settings.database_url or "").strip()
    if not url:
        _last_error = ""
        return False
    if _pool is not None:
        return True
    _init_attempted = True
    try:
        import asyncpg

        _pool = await asyncpg.create_pool(
            dsn=url,
            min_size=max(1, int(settings.postgres_pool_min)),
            max_size=max(1, int(settings.postgres_pool_max)),
            command_timeout=10,
        )
        async with _pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS attendance (
                  id BIGSERIAL PRIMARY KEY,
                  event_id TEXT NOT NULL UNIQUE,
                  employee_code TEXT NOT NULL,
                  device_serial TEXT NOT NULL,
                  device_name TEXT DEFAULT '',
                  branch TEXT DEFAULT '',
                  punch_time TEXT NOT NULL,
                  punch_date TEXT NOT NULL,
                  punch_status TEXT DEFAULT '0',
                  verify_method TEXT DEFAULT '1',
                  work_code TEXT DEFAULT '',
                  punch_label TEXT DEFAULT '',
                  verify_label TEXT DEFAULT '',
                  firebase_status TEXT NOT NULL DEFAULT 'pending',
                  received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                  raw_line TEXT DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_pg_attendance_device ON attendance(device_serial);
                CREATE INDEX IF NOT EXISTS idx_pg_attendance_date ON attendance(punch_date);
                CREATE INDEX IF NOT EXISTS idx_pg_attendance_employee ON attendance(employee_code);
                """
            )
        _last_error = ""
        logger.info("Postgres attendance pool ready")
        return True
    except Exception as exc:  # noqa: BLE001
        _pool = None
        _last_error = str(exc)[:300]
        logger.error("Postgres init failed: %s", exc)
        return False


async def close_postgres() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def upsert_attendance(row: dict[str, Any]) -> bool:
    """Insert attendance if new. Returns True when a new row was written.

    Raises on connection failure so callers can refuse device ACK.
    """
    settings = get_settings()
    if not (settings.database_url or "").strip():
        return False
    if _pool is None:
        ok = await init_postgres()
        if not ok or _pool is None:
            raise RuntimeError(_last_error or "postgres unavailable")

    sql = """
        INSERT INTO attendance (
          event_id, employee_code, device_serial, device_name, branch,
          punch_time, punch_date, punch_status, verify_method, work_code,
          punch_label, verify_label, firebase_status, raw_line
        ) VALUES (
          $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14
        )
        ON CONFLICT (event_id) DO NOTHING
        RETURNING id
    """
    async with _pool.acquire() as conn:
        result = await conn.fetchrow(
            sql,
            row["event_id"],
            row["employee_code"],
            row["device_serial"],
            row.get("device_name") or "",
            row.get("branch") or "",
            row["punch_time"],
            row["punch_date"],
            row.get("punch_status") or "0",
            row.get("verify_method") or "1",
            row.get("work_code") or "",
            row.get("punch_label") or "",
            row.get("verify_label") or "",
            row.get("firebase_status") or "pending",
            row.get("raw_line") or "",
        )
        return result is not None


async def ping() -> dict[str, Any]:
    if not (get_settings().database_url or "").strip():
        return {"ok": False, "error": "DATABASE_URL not set"}
    try:
        if _pool is None:
            await init_postgres()
        if _pool is None:
            return {"ok": False, "error": _last_error or "init failed"}
        async with _pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:300]}
