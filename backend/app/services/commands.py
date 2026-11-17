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
}


async def enqueue(device_serial: str, command_code: str, payload: str = "") -> dict:
    code = command_code.upper().strip().replace(" ", "_").replace("-", "_")
    # Accept raw alias
    if code == "DATA_QUERY_USERINFO" or code == "QUERY_USERINFO":
        code = "QUERY_USERINFO"
    if code not in ALLOWED:
        raise ValueError(f"Command not allowed: {command_code}")
    await db.execute(
        "INSERT INTO device_commands (device_serial, command_code, payload, status) VALUES (?, ?, ?, 'pending')",
        (device_serial, code, payload),
    )
    row = await db.fetchone("SELECT * FROM device_commands ORDER BY id DESC LIMIT 1")
    return dict(row)


def format_device_command(cmd: dict) -> str:
    code = cmd["command_code"]
    line = ALLOWED.get(code, code)
    payload = (cmd.get("payload") or "").strip()
    if payload and code == "QUERY_USERINFO":
        # e.g. PIN= or PIN=12 for filtered query
        return f"C:{cmd['id']}:{line} {payload}"
    return f"C:{cmd['id']}:{line}"


async def next_pending(device_serial: str) -> Optional[dict]:
    row = await db.fetchone(
        "SELECT * FROM device_commands WHERE device_serial=? AND status='pending' ORDER BY id ASC LIMIT 1",
        (device_serial,),
    )
    return dict(row) if row else None


async def mark_sent(cmd_id: int) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
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
