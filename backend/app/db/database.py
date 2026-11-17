"""SQLite helpers (WAL)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Iterable, Optional

import aiosqlite

from app.config import get_settings

_lock = asyncio.Lock()


async def get_db() -> aiosqlite.Connection:
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(settings.db_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA synchronous=NORMAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def _ensure_columns(db: aiosqlite.Connection) -> None:
    """Lightweight migrations for existing DBs."""
    cur = await db.execute("PRAGMA table_info(employees)")
    cols = {row[1] for row in await cur.fetchall()}
    if "card" not in cols:
        await db.execute("ALTER TABLE employees ADD COLUMN card TEXT DEFAULT ''")


async def init_db() -> None:
    settings = get_settings()
    schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
    async with _lock:
        db = await get_db()
        try:
            await db.executescript(schema)
            await _ensure_columns(db)
            await db.commit()
        finally:
            await db.close()


async def execute(sql: str, params: Iterable[Any] = ()) -> None:
    db = await get_db()
    try:
        await db.execute(sql, tuple(params))
        await db.commit()
    finally:
        await db.close()


async def executemany(sql: str, seq: Iterable[Iterable[Any]]) -> None:
    db = await get_db()
    try:
        await db.executemany(sql, [tuple(x) for x in seq])
        await db.commit()
    finally:
        await db.close()


async def fetchone(sql: str, params: Iterable[Any] = ()) -> Optional[aiosqlite.Row]:
    db = await get_db()
    try:
        cur = await db.execute(sql, tuple(params))
        return await cur.fetchone()
    finally:
        await db.close()


async def fetchall(sql: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
    db = await get_db()
    try:
        cur = await db.execute(sql, tuple(params))
        return await cur.fetchall()
    finally:
        await db.close()


def row_to_dict(row: Optional[aiosqlite.Row]) -> Optional[dict]:
    if row is None:
        return None
    return dict(row)
