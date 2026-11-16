"""Async Firebase sync with retry. Never deletes machine records or templates."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from app.config import get_settings
from app.services import attendance as attendance_svc
from app.services import employees as employees_svc

logger = logging.getLogger("adms.firebase")

_app = None
_db = None


def _init_firebase() -> bool:
    global _app, _db
    settings = get_settings()
    if not settings.firebase_enabled:
        return False
    if _db is not None:
        return True
    path = settings.firebase_credentials_path
    if not path.exists():
        logger.warning("Firebase credentials missing: %s", path)
        return False
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore

        if not firebase_admin._apps:
            cred = credentials.Certificate(str(path))
            _app = firebase_admin.initialize_app(cred)
        _db = firestore.client()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("Firebase init failed: %s", exc)
        return False


def firebase_status() -> dict[str, Any]:
    """Lightweight status for /health and dashboard (no network I/O)."""
    settings = get_settings()
    path = settings.firebase_credentials_path
    present = path.exists()
    if not settings.firebase_enabled:
        return {
            "enabled": False,
            "credentials_present": present,
            "credentials_path": str(path),
            "ready": False,
            "reason": "FIREBASE_ENABLED=false — sync is off; punches stay pending in SQLite",
        }
    if not present:
        return {
            "enabled": True,
            "credentials_present": False,
            "credentials_path": str(path),
            "ready": False,
            "reason": "credentials file missing",
        }
    return {
        "enabled": True,
        "credentials_present": True,
        "credentials_path": str(path),
        "ready": True,
        "reason": "",
        "company_id": settings.firebase_company_id,
    }


def _doc_path(template: str, company_id: str, record_id: str, device_id: str = "") -> tuple[str, ...]:
    path = template.format(
        companyId=company_id,
        recordId=record_id,
        deviceId=device_id or record_id,
        employeeId=record_id,
    )
    return tuple(p for p in path.split("/") if p)


def _ref_from_parts(parts: tuple[str, ...]) -> Any:
    assert _db is not None
    ref: Any = _db
    for i, part in enumerate(parts):
        if i % 2 == 0:
            ref = ref.collection(part)
        else:
            ref = ref.document(part)
    return ref


def _write_attendance(record: dict) -> None:
    settings = get_settings()
    parts = _doc_path(
        settings.firebase_attendance_path,
        settings.firebase_company_id,
        record["event_id"],
        record.get("device_serial") or "",
    )
    ref = _ref_from_parts(parts)
    payload = {
        "eventId": record["event_id"],
        "employeeCode": record["employee_code"],
        "employeeName": record.get("employee_name") or "",
        "deviceSerial": record["device_serial"],
        "deviceName": record.get("device_name") or "",
        "branch": record.get("branch") or "",
        "punchTime": record["punch_time"],
        "punchDate": record["punch_date"],
        "punchStatus": record.get("punch_status") or "0",
        "verifyMethod": record.get("verify_method") or "1",
        "workCode": record.get("work_code") or "",
        "receivedAt": record.get("received_at"),
        "source": "adms-push",
        "syncedAt": datetime.now(timezone.utc).isoformat(),
    }
    ref.set(payload, merge=True)


def _write_employee(emp: dict) -> str:
    """Upsert employee master (PIN + name only). Returns document id."""
    settings = get_settings()
    company = emp.get("company_id") or settings.firebase_company_id or "default"
    serial = (emp.get("device_serial") or "").strip()
    code = (emp.get("employee_code") or "").strip()
    doc_id = f"{serial}:{code}" if serial else code
    parts = _doc_path(settings.firebase_employees_path, company, doc_id, serial)
    ref = _ref_from_parts(parts)
    payload = {
        "employeeCode": code,
        "enrollmentId": code,
        "name": emp.get("name") or "",
        "card": emp.get("card") or "",
        "branch": emp.get("branch") or "",
        "deviceSerial": serial,
        "companyId": company,
        "source": "adms-userinfo",
        "syncedAt": datetime.now(timezone.utc).isoformat(),
    }
    ref.set(payload, merge=True)
    return doc_id


async def sync_employees_once() -> int:
    settings = get_settings()
    if not settings.firebase_enabled or not _init_firebase():
        return 0
    batch = await employees_svc.pending_firebase(settings.sync_batch_size)
    done = 0
    for emp in batch:
        try:
            doc_id = await asyncio.to_thread(_write_employee, emp)
            await employees_svc.mark_firebase(int(emp["id"]), "synced", doc_id)
            done += 1
        except Exception as exc:  # noqa: BLE001
            await employees_svc.mark_firebase(int(emp["id"]), "failed")
            logger.warning("employee sync failed %s: %s", emp.get("employee_code"), exc)
    return done


async def sync_once() -> int:
    settings = get_settings()
    if not settings.firebase_enabled or not _init_firebase():
        return 0
    batch = await attendance_svc.pending_batch(settings.sync_batch_size)
    done = 0
    for rec in batch:
        try:
            await asyncio.to_thread(_write_attendance, rec)
            await attendance_svc.mark_synced(rec["event_id"])
            done += 1
        except Exception as exc:  # noqa: BLE001
            await attendance_svc.mark_failed(rec["event_id"], str(exc))
            logger.warning("sync failed %s: %s", rec["event_id"], exc)
    done += await sync_employees_once()
    return done


async def sync_batches(max_batches: int = 20) -> dict[str, Any]:
    """Drain pending attendance (+ employees) in bounded batches."""
    total = 0
    batches = 0
    for _ in range(max(1, max_batches)):
        n = await sync_once()
        batches += 1
        total += n
        if n == 0:
            break
    return {"ok": True, "synced": total, "batches": batches}


async def sync_loop(stop_event: asyncio.Event) -> None:
    settings = get_settings()
    backoff = settings.sync_interval_seconds
    while not stop_event.is_set():
        try:
            n = await sync_once()
            backoff = settings.sync_interval_seconds if n else min(backoff * 2, 120)
        except Exception as exc:  # noqa: BLE001
            logger.error("sync loop error: %s", exc)
            backoff = min(backoff * 2, 120)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=backoff)
        except asyncio.TimeoutError:
            pass


async def test_connection() -> dict:
    settings = get_settings()
    if not settings.firebase_enabled:
        return {"ok": False, "error": "firebase_enabled=false"}
    if not _init_firebase():
        return {"ok": False, "error": "init failed or missing credentials"}
    try:
        assert _db is not None
        await asyncio.to_thread(lambda: list(_db.collection("companies").limit(1).stream()))
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
