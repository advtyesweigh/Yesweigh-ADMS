"""Minimal safe command queue for getrequest polling."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from app.db import database as db

# Map API codes → exact device command line after C:{id}:
ALLOWED = {
    "INFO": "INFO",
    "CHECK": "CHECK",
    # Pull full user master from device (PIN/Name/Card…)
    "QUERY_USERINFO": "DATA QUERY USERINFO",
    "DATA_QUERY_USERINFO": "DATA QUERY USERINFO",
    # Pull attendance logs for a time window (backfill after reconnect)
    "QUERY_ATTLOG": "DATA QUERY ATTLOG",
    "DATA_QUERY_ATTLOG": "DATA QUERY ATTLOG",
}


async def enqueue(device_serial: str, command_code: str, payload: str = "") -> dict:
    code = command_code.upper().strip().replace(" ", "_").replace("-", "_")
    # Accept raw alias
    if code in ("DATA_QUERY_USERINFO", "QUERY_USERINFO"):
        code = "QUERY_USERINFO"
    if code in ("DATA_QUERY_ATTLOG", "QUERY_ATTLOG"):
        code = "QUERY_ATTLOG"
    if code not in ALLOWED:
        raise ValueError(f"Command not allowed: {command_code}")
    await db.execute(
        "INSERT INTO device_commands (device_serial, command_code, payload, status) VALUES (?, ?, ?, 'pending')",
        (device_serial, code, payload),
    )
    row = await db.fetchone("SELECT * FROM device_commands ORDER BY id DESC LIMIT 1")
    return dict(row)


async def enqueue_attlog_recovery(device_serial: str, payload: str) -> Optional[dict]:
    """Queue one QUERY_ATTLOG for interrupted upload recovery; skip if already pending/sent."""
    existing = await db.fetchone(
        """SELECT id, status, payload FROM device_commands
           WHERE device_serial=? AND command_code='QUERY_ATTLOG'
             AND status IN ('pending', 'sent')
           ORDER BY id DESC LIMIT 1""",
        (device_serial,),
    )
    if existing:
        return dict(existing)
    return await enqueue(device_serial, "QUERY_ATTLOG", payload)


def format_device_command(cmd: dict) -> str:
    """Wire format required by ZK push: C:<id>:<cmd>\\n"""
    code = cmd["command_code"]
    line = ALLOWED.get(code, code)
    payload = (cmd.get("payload") or "").strip()
    if payload and code in ("QUERY_USERINFO", "QUERY_ATTLOG"):
        # e.g. PIN=12  or  StartTime=... EndTime=...
        body = f"C:{cmd['id']}:{line} {payload}"
    else:
        body = f"C:{cmd['id']}:{line}"
    return body if body.endswith("\n") else body + "\n"


async def next_pending(device_serial: str) -> Optional[dict]:
    row = await db.fetchone(
        "SELECT * FROM device_commands WHERE device_serial=? AND status='pending' ORDER BY id ASC LIMIT 1",
        (device_serial,),
    )
    return dict(row) if row else None


async def next_pending_batch(device_serial: str, limit: int = 20) -> list[dict]:
    rows = await db.fetchall(
        """SELECT * FROM device_commands
           WHERE device_serial=? AND status='pending'
           ORDER BY id ASC LIMIT ?""",
        (device_serial, limit),
    )
    return [dict(r) for r in rows]


async def mark_sent(cmd_id: int) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    await db.execute("UPDATE device_commands SET status='sent', sent_at=? WHERE id=?", (now, cmd_id))


async def mark_sent_many(cmd_ids: list[int]) -> None:
    if not cmd_ids:
        return
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    for cmd_id in cmd_ids:
        await db.execute("UPDATE device_commands SET status='sent', sent_at=? WHERE id=?", (now, cmd_id))


async def acknowledge(device_serial: str, raw: str) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    cmd_id = None
    for part in raw.replace("\n", "&").split("&"):
        if part.upper().startswith("ID="):
            try:
                cmd_id = int(part.split("=", 1)[1])
            except ValueError:
                pass
    if cmd_id:
        await db.execute(
            "UPDATE device_commands SET status='acked', completed_at=?, result=? WHERE id=? AND device_serial=?",
            (now, raw[:1000], cmd_id, device_serial),
        )


async def list_commands(limit: int = 50) -> list[dict]:
    rows = await db.fetchall("SELECT * FROM device_commands ORDER BY id DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]


async def expire_stale_sent(*, older_than_hours: int = 6) -> int:
    """Mark long-unacked 'sent' commands expired so they cannot confuse ops/backlog views.

    getrequest only delivers status=pending, so stale 'sent' rows are not re-pushed;
    clearing them still keeps the queue healthy after reconnect ATTLOG recovery.
    """
    from datetime import timedelta

    hours = max(1, int(older_than_hours))
    now = datetime.now(timezone.utc).replace(microsecond=0)
    cutoff_s = (now - timedelta(hours=hours)).isoformat()
    before = await db.fetchone(
        "SELECT COUNT(*) AS n FROM device_commands WHERE status='sent' AND (sent_at IS NULL OR sent_at < ?)",
        (cutoff_s,),
    )
    n = int((before["n"] if before else 0) or 0)
    if n <= 0:
        return 0
    await db.execute(
        """UPDATE device_commands
           SET status='timeout', completed_at=?, result='expired-unacked'
           WHERE status='sent' AND (sent_at IS NULL OR sent_at < ?)""",
        (now.isoformat(), cutoff_s),
    )
    return n
