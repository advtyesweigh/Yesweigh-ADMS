"""SQLite helpers with a shared WAL connection (avoids open/close storms)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Iterable, Optional

import aiosqlite

from app.config import get_settings

_lock = asyncio.Lock()
_conn: Optional[aiosqlite.Connection] = None


async def _open_conn() -> aiosqlite.Connection:
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(settings.db_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA synchronous=NORMAL")
    await db.execute("PRAGMA foreign_keys=ON")
    await db.execute("PRAGMA busy_timeout=5000")
    return db


async def get_db() -> aiosqlite.Connection:
    global _conn
    if _conn is not None:
        return _conn
    async with _lock:
        if _conn is None:
            _conn = await _open_conn()
        return _conn


async def close_db() -> None:
    global _conn
    async with _lock:
        if _conn is not None:
            await _conn.close()
            _conn = None


async def _table_columns(db: aiosqlite.Connection, table: str) -> set[str]:
    cur = await db.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cur.fetchall()}


async def _ensure_columns(db: aiosqlite.Connection) -> None:
    """Lightweight migrations for existing DBs."""
    emp_cols = await _table_columns(db, "employees")
    for col, ddl in (
        ("card", "TEXT DEFAULT ''"),
        ("device_serial", "TEXT DEFAULT ''"),
        ("company_id", "TEXT DEFAULT 'default'"),
        ("firebase_status", "TEXT DEFAULT 'pending'"),
    ):
        if col not in emp_cols:
            await db.execute(f"ALTER TABLE employees ADD COLUMN {col} {ddl}")

    cur = await db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='employees'"
    )
    row = await cur.fetchone()
    sql = (row[0] or "") if row else ""
    if "employee_code TEXT NOT NULL UNIQUE" in sql.replace("\n", " "):
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS employees_v2 (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              employee_code TEXT NOT NULL,
              device_serial TEXT NOT NULL DEFAULT '',
              company_id TEXT NOT NULL DEFAULT 'default',
              name TEXT DEFAULT '',
              card TEXT DEFAULT '',
              branch TEXT DEFAULT '',
              firebase_id TEXT DEFAULT '',
              firebase_status TEXT NOT NULL DEFAULT 'pending',
              updated_at TEXT NOT NULL DEFAULT (datetime('now')),
              UNIQUE (company_id, device_serial, employee_code)
            );
            INSERT OR IGNORE INTO employees_v2
              (id, employee_code, device_serial, company_id, name, card, branch, firebase_id, firebase_status, updated_at)
            SELECT id, employee_code,
                   COALESCE(device_serial, ''),
                   COALESCE(NULLIF(company_id, ''), 'default'),
                   COALESCE(name, ''),
                   COALESCE(card, ''),
                   COALESCE(branch, ''),
                   COALESCE(firebase_id, ''),
                   COALESCE(NULLIF(firebase_status, ''), 'pending'),
                   COALESCE(updated_at, datetime('now'))
            FROM employees;
            DROP TABLE employees;
            ALTER TABLE employees_v2 RENAME TO employees;
            CREATE INDEX IF NOT EXISTS idx_employees_code ON employees(employee_code);
            CREATE INDEX IF NOT EXISTS idx_employees_device ON employees(device_serial);
            CREATE INDEX IF NOT EXISTS idx_employees_firebase ON employees(firebase_status);
            """
        )

    dev_cols = await _table_columns(db, "devices")
    for col, ddl in (
        ("last_userinfo_at", "TEXT"),
        ("userinfo_supported", "TEXT DEFAULT 'unknown'"),
        ("last_disconnect_at", "TEXT"),
        ("last_reconnect_at", "TEXT"),
        ("last_server_error_at", "TEXT"),
        ("last_server_error", "TEXT DEFAULT ''"),
    ):
        if col not in dev_cols:
            await db.execute(f"ALTER TABLE devices ADD COLUMN {col} {ddl}")

    await db.execute("CREATE INDEX IF NOT EXISTS idx_employees_code ON employees(employee_code)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_employees_device ON employees(device_serial)")
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_employees_firebase ON employees(firebase_status)"
    )


async def init_db() -> None:
    schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
    db = await get_db()
    async with _lock:
        await db.executescript(schema)
        await _ensure_columns(db)
        await db.commit()


async def execute(sql: str, params: Iterable[Any] = ()) -> None:
    db = await get_db()
    async with _lock:
        await db.execute(sql, tuple(params))
        await db.commit()


async def executemany(sql: str, seq: Iterable[Iterable[Any]]) -> None:
    db = await get_db()
    async with _lock:
        await db.executemany(sql, [tuple(x) for x in seq])
        await db.commit()


async def fetchone(sql: str, params: Iterable[Any] = ()) -> Optional[aiosqlite.Row]:
    db = await get_db()
    async with _lock:
        cur = await db.execute(sql, tuple(params))
        return await cur.fetchone()


async def fetchall(sql: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
    db = await get_db()
    async with _lock:
        cur = await db.execute(sql, tuple(params))
        return await cur.fetchall()


def row_to_dict(row: Optional[aiosqlite.Row]) -> Optional[dict]:
    if row is None:
        return None
    return dict(row)
