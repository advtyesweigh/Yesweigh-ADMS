"""ADMS — single process on TCP 8081 (ZKTeco Push + admin UI)."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
import secrets

from app.adms.handlers import mount_adms_routes
from app.config import get_settings
from app.db.database import init_db
from app.services import attendance as attendance_svc
from app.services import commands as commands_svc
from app.services import devices as devices_svc
from app.services import employees as employees_svc
from app.services import firebase_sync
from app.services import logs as logs_svc
from app.services import overview as overview_svc

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("adms")

security = HTTPBasic(auto_error=False)
_stop = asyncio.Event()
STATIC_DIR = Path(__file__).resolve().parent / "static"


def _check_auth(credentials: HTTPBasicCredentials | None) -> None:
    settings = get_settings()
    if credentials is None:
        raise HTTPException(status_code=401, detail="Auth required", headers={"WWW-Authenticate": "Basic"})
    user_ok = secrets.compare_digest(credentials.username, settings.admin_username)
    pass_ok = secrets.compare_digest(credentials.password, settings.admin_password)
    if not (user_ok and pass_ok):
        raise HTTPException(status_code=401, detail="Invalid credentials", headers={"WWW-Authenticate": "Basic"})


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    await init_db()
    _stop.clear()
    task = asyncio.create_task(firebase_sync.sync_loop(_stop))
    logger.info("ADMS listening — Push port %s only (no 4370/8080/8082)", settings.port)
    yield
    _stop.set()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="ADMS", version="1.0.0", lifespan=lifespan)
mount_adms_routes(app.router, listen_port=get_settings().port)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/health")
async def health():
    settings = get_settings()
    sync = await attendance_svc.sync_counts()
    fb = firebase_sync.firebase_status()
    return {
        "ok": True,
        "app": "ADMS",
        "port": settings.port,
        "firebase_enabled": settings.firebase_enabled,
        "firebase": fb,
        "sync": sync,
        "auto_sync_users": settings.auto_sync_users,
        "auto_approve_devices": settings.auto_approve_devices,
    }


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    index = STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(index)
    return HTMLResponse("<h1>ADMS</h1><p>Dashboard missing.</p>")


@app.get("/api/summary")
async def api_summary(credentials: HTTPBasicCredentials | None = Depends(security)):
    _check_auth(credentials)
    devices = await devices_svc.counts()
    sync = await attendance_svc.sync_counts()
    return {
        "devices": devices,
        "sync": sync,
        "port": get_settings().port,
        "firebase": firebase_sync.firebase_status(),
    }


@app.get("/api/devices")
async def api_devices(credentials: HTTPBasicCredentials | None = Depends(security)):
    _check_auth(credentials)
    devices = await devices_svc.list_devices()
    # Enrich with employee name coverage for CRM device picker
    for d in devices:
        serial = str(d.get("serial_number") or "")
        if not serial:
            continue
        rows = await employees_svc.list_employees(limit=5000, device_serial=serial)
        d["employee_count"] = len(rows)
        d["named_count"] = sum(1 for r in rows if (r.get("name") or "").strip())
        d["last_userinfo_at"] = d.get("last_userinfo_at")
        d["userinfo_supported"] = d.get("userinfo_supported") or "unknown"
    return devices


@app.patch("/api/devices/{serial}")
async def api_device_update(serial: str, request: Request, credentials: HTTPBasicCredentials | None = Depends(security)):
    _check_auth(credentials)
    body = await request.json()
    updated = await devices_svc.update_device(serial, **body)
    if not updated:
        raise HTTPException(404, "Device not found")
    return updated


@app.get("/api/attendance")
async def api_attendance(
    limit: int = 100,
    credentials: HTTPBasicCredentials | None = Depends(security),
):
    _check_auth(credentials)
    return await attendance_svc.list_attendance(limit=min(limit, 500))


@app.get("/api/attendance/export")
async def api_attendance_export(
    after_id: int = 0,
    limit: int = 500,
    credentials: HTTPBasicCredentials | None = Depends(security),
):
    """Cursor export for CRM Postgres ingest (does not require Firebase)."""
    _check_auth(credentials)
    return await attendance_svc.export_since(after_id=after_id, limit=limit)




@app.get("/api/employees")
async def api_employees(
    limit: int = 500,
    device_serial: str | None = None,
    credentials: HTTPBasicCredentials | None = Depends(security),
):
    _check_auth(credentials)
    serial = (device_serial or "").strip() or None
    rows = await employees_svc.list_employees(limit=min(limit, 2000), device_serial=serial)
    return {
        "count": len(rows),
        "employees": rows,
        "device_serial": serial or "",
        "last_updated_at": await employees_svc.last_updated_at(serial or ""),
        "named_count": sum(1 for r in rows if (r.get("name") or "").strip()),
    }


@app.get("/api/devices/{serial}/userinfo-status")
async def api_userinfo_status(serial: str, credentials: HTTPBasicCredentials | None = Depends(security)):
    """Probe result / last USERINFO sync for a device (used by CRM import UI)."""
    _check_auth(credentials)
    devices = await devices_svc.list_devices()
    device = next((d for d in devices if d.get("serial_number") == serial), None)
    if not device:
        raise HTTPException(404, "Device not found")
    rows = await employees_svc.list_employees(limit=2000, device_serial=serial)
    named = sum(1 for r in rows if (r.get("name") or "").strip())
    return {
        "serial": serial,
        "online": bool(device.get("online")),
        "status": device.get("status"),
        "model": device.get("model") or "",
        "firmware": device.get("firmware") or "",
        "last_heartbeat_at": device.get("last_heartbeat_at"),
        "last_userinfo_at": device.get("last_userinfo_at"),
        "userinfo_supported": device.get("userinfo_supported") or "unknown",
        "employee_count": len(rows),
        "named_count": named,
        "unnamed_count": len(rows) - named,
        "auto_sync_users": get_settings().auto_sync_users,
        "last_employee_updated_at": await employees_svc.last_updated_at(serial),
        "note": (
            "Auto-sync is off until USERINFO is verified on this firmware "
            "(see docs/X305_PRO_USERINFO.md)."
            if not get_settings().auto_sync_users
            else "Auto-sync is enabled."
        ),
    }


@app.get("/api/overview")
async def api_overview(
    date: str | None = None,
    date_to: str | None = None,
    branch: str = "",
    device: str = "",
    status: str = "",
    q: str = "",
    credentials: HTTPBasicCredentials | None = Depends(security),
):
    _check_auth(credentials)
    return await overview_svc.build_overview(
        date=date,
        date_to=date_to,
        branch=branch,
        device=device,
        status=status,
        q=q,
    )


@app.post("/api/devices/{serial}/query-users")
async def api_query_users(serial: str, request: Request, credentials: HTTPBasicCredentials | None = Depends(security)):
    """Queue DATA QUERY USERINFO so the device uploads emp master.

    Body (optional JSON):
      {"pins": ["1","2"]}  — query those PINs
      {"all_known": true}  — query every employee_code already in DB/attendance
      {} / omitted         — one bulk DATA QUERY USERINFO
    """
    _check_auth(credentials)
    device = await devices_svc.get_device(serial)
    if not device:
        raise HTTPException(404, "Device not found")
    if device.get("status") != "approved":
        raise HTTPException(400, "Device not approved")

    pins: list[str] = []
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if isinstance(body, dict):
        if body.get("pins"):
            pins = [str(p).strip() for p in body["pins"] if str(p).strip()]
        elif body.get("all_known"):
            rows = await employees_svc.list_employees(limit=2000)
            pins = [r["employee_code"] for r in rows]
            if not pins:
                # fall back to distinct attendance codes
                from app.db import database as db

                att = await db.fetchall(
                    "SELECT DISTINCT employee_code FROM attendance ORDER BY CAST(employee_code AS INTEGER)"
                )
                pins = [r["employee_code"] for r in att]

    if pins:
        cmds = []
        for pin in pins:
            cmds.append(await commands_svc.enqueue(serial, "QUERY_USERINFO", f"PIN={pin}"))
        return {"queued": True, "mode": "per_pin", "count": len(cmds), "commands": cmds}

    cmd = await commands_svc.enqueue(serial, "QUERY_USERINFO")
    return {"queued": True, "mode": "bulk", "command": cmd}


@app.get("/api/logs")
async def api_logs(limit: int = 100, credentials: HTTPBasicCredentials | None = Depends(security)):
    _check_auth(credentials)
    return await logs_svc.recent(limit=min(limit, 500))


@app.post("/api/firebase/test")
async def api_firebase_test(credentials: HTTPBasicCredentials | None = Depends(security)):
    _check_auth(credentials)
    return await firebase_sync.test_connection()


@app.post("/api/firebase/retry")
async def api_firebase_retry(credentials: HTTPBasicCredentials | None = Depends(security)):
    """Reset failed → pending, then drain up to ~1000 records in safe batches."""
    _check_auth(credentials)
    reset = await attendance_svc.retry_failed()
    result = await firebase_sync.sync_batches(max_batches=20)
    result["failed_reset"] = reset
    return result


def run() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    run()
