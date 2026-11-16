from __future__ import annotations

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
