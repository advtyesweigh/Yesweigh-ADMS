"""Employee master upserted from ZK USERINFO / OPERLOG.

Never stores fingerprint/face templates or device passwords.
Never pushes create/update/delete of users back to the physical device.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from app.adms.protocol import DeviceUser
from app.config import get_settings
from app.db import database as db
from app.employee_exclusions import is_excluded_employee_code


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


async def upsert_user(
    user: DeviceUser,
    *,
    device_serial: str = "",
    branch: str = "",
    company_id: str | None = None,
) -> bool:
    """Insert or update employee. Returns True if row changed."""
    code = (user.employee_code or "").strip()
    if not code or is_excluded_employee_code(code):
        return False
    settings = get_settings()
    company = (company_id or settings.firebase_company_id or "default").strip() or "default"
    serial = (device_serial or "").strip()
    now = _utcnow()
    name = (user.name or "").strip()
    card = (user.card or "").strip()
    # Privilege / Passwd / FP / FACE are intentionally ignored

    existing = await db.fetchone(
        """SELECT * FROM employees
           WHERE company_id=? AND device_serial=? AND employee_code=?""",
        (company, serial, code),
    )
    if not existing and serial:
        # Legacy rows (pre device_serial) — adopt onto this device once
        legacy = await db.fetchone(
            """SELECT * FROM employees
               WHERE company_id=? AND employee_code=? AND (device_serial='' OR device_serial IS NULL)
               LIMIT 1""",
            (company, code),
        )
        if legacy:
            existing = legacy

    if existing:
        row = dict(existing)
        new_name = name or (row.get("name") or "")
        new_card = card or (row.get("card") or "")
        new_branch = branch or (row.get("branch") or "")
        new_serial = serial or (row.get("device_serial") or "")
        if (
            new_name == (row.get("name") or "")
            and new_card == (row.get("card") or "")
            and new_branch == (row.get("branch") or "")
            and new_serial == (row.get("device_serial") or "")
        ):
            return False
        await db.execute(
            """UPDATE employees
               SET name=?, card=?, branch=?, device_serial=?, firebase_status='pending', updated_at=?
               WHERE id=?""",
            (new_name, new_card, new_branch, new_serial, now, row["id"]),
        )
        return True

    await db.execute(
        """INSERT INTO employees
           (employee_code, device_serial, company_id, name, card, branch, firebase_status, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)""",
        (code, serial, company, name, card, branch or "", now),
    )
    return True


async def upsert_many(
    users: list[DeviceUser],
    *,
    device_serial: str = "",
    branch: str = "",
    company_id: str | None = None,
) -> int:
    saved = 0
    for user in users:
        if await upsert_user(
            user,
            device_serial=device_serial,
            branch=branch,
            company_id=company_id,
        ):
            saved += 1
    return saved


async def list_employees(
    limit: int = 500,
    *,
    device_serial: str | None = None,
) -> list[dict]:
    if device_serial:
        rows = await db.fetchall(
            """SELECT * FROM employees
               WHERE device_serial=? OR device_serial='' OR device_serial IS NULL
               ORDER BY CAST(employee_code AS INTEGER), employee_code LIMIT ?""",
            (device_serial, limit),
        )
    else:
        rows = await db.fetchall(
            """SELECT * FROM employees
               ORDER BY CAST(employee_code AS INTEGER), employee_code LIMIT ?""",
            (limit,),
        )
    return [dict(r) for r in rows]


async def count() -> int:
    row = await db.fetchone("SELECT COUNT(*) AS c FROM employees")
    return int(row["c"]) if row else 0


async def get_employee(code: str, device_serial: str = "") -> Optional[dict]:
    if device_serial:
        row = await db.fetchone(
            """SELECT * FROM employees
               WHERE employee_code=? AND device_serial=? LIMIT 1""",
            (code, device_serial),
        )
        if row:
            return dict(row)
    row = await db.fetchone(
        "SELECT * FROM employees WHERE employee_code=? ORDER BY id DESC LIMIT 1",
        (code,),
    )
    return dict(row) if row else None


async def pending_firebase(limit: int = 50) -> list[dict]:
    rows = await db.fetchall(
        """SELECT * FROM employees
           WHERE firebase_status IN ('pending', 'failed')
           ORDER BY id ASC LIMIT ?""",
        (limit,),
    )
    return [dict(r) for r in rows]


async def mark_firebase(employee_id: int, status: str, firebase_id: str = "") -> None:
    now = _utcnow()
    await db.execute(
        """UPDATE employees
           SET firebase_status=?, firebase_id=COALESCE(NULLIF(?, ''), firebase_id), updated_at=?
           WHERE id=?""",
        (status, firebase_id, now, employee_id),
    )


async def last_updated_at(device_serial: str = "") -> Optional[str]:
    if device_serial:
        row = await db.fetchone(
            """SELECT MAX(updated_at) AS t FROM employees
               WHERE device_serial=? OR device_serial=''""",
            (device_serial,),
        )
    else:
        row = await db.fetchone("SELECT MAX(updated_at) AS t FROM employees")
    return str(row["t"]) if row and row["t"] else None
