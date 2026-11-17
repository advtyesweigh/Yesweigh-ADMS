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
from app.db import postgres as pg
from app.db.database import close_db, init_db
from app.services import attendance as attendance_svc
from app.services import commands as commands_svc
from app.services import connection_history as history_svc
from app.services import devices as devices_svc
from app.services import employees as employees_svc
from app.services import firebase_sync
from app.services import logs as logs_svc
from app.services import overview as overview_svc
from app.services import sessions as sessions_svc
from app.services import watchdog as watchdog_svc

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
    await pg.init_postgres()
    # Do not inherit Online from DB timestamps written by a previous process.
    boot = sessions_svc.mark_service_started()
    sessions_svc.release_all(reason="service_restart", close_tcp=False, local_port=settings.port)
    # Seed registry from DB so watchdog can close stale peer sockets immediately.
    for d in await devices_svc.list_devices_raw():
        serial = str(d.get("serial_number") or "")
        if not serial:
            continue
        sessions_svc.release_session(
            serial,
            reason="service_restart",
            close_tcp=False,
            peer_ip=str(d.get("last_ip") or ""),
            local_port=settings.port,
        )
    try:
        expired = await commands_svc.expire_stale_sent(older_than_hours=6)
        if expired:
            logger.info("expired %s stale unacked device command(s)", expired)
    except Exception as exc:  # noqa: BLE001
        logger.warning("expire_stale_sent failed: %s", exc)
    _stop.clear()
    sync_task = asyncio.create_task(firebase_sync.sync_loop(_stop))
    watch_task = asyncio.create_task(watchdog_svc.watchdog_loop(_stop))
    logger.info(
        "ADMS listening — Push port %s only (no 4370/8080/8082); boot=%s; "
        "online≤%ss offline>%ss Delay=%s ErrorDelay=%s; postgres=%s",
        settings.port,
        boot.isoformat(),
        settings.device_online_seconds,
        settings.device_offline_seconds,
        settings.heartbeat_interval_hint,
        settings.handshake_error_delay,
        pg.postgres_status().get("ready"),
    )
    yield
    _stop.set()
    for task in (sync_task, watch_task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    await pg.close_postgres()
    await close_db()


app = FastAPI(title="ADMS", version="1.0.0", lifespan=lifespan)
mount_adms_routes(app.router, listen_port=get_settings().port)


@app.middleware("http")
async def iclock_close_connection(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/iclock"):
        response.headers["Connection"] = "close"
    return response

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/health")
async def health():
    settings = get_settings()
    sync = await attendance_svc.sync_counts()
    fb = firebase_sync.firebase_status()
    boot = sessions_svc.service_started_at()
    return {
        "ok": True,
        "app": "ADMS",
        "port": settings.port,
        "public_base_url": settings.public_base_url,
        "firebase_enabled": settings.firebase_enabled,
        "firebase": fb,
        "postgres": pg.postgres_status(),
        "sync": sync,
        "auto_sync_users": settings.auto_sync_users,
        "auto_approve_devices": settings.auto_approve_devices,
        "service_started_at": boot.isoformat() if boot else None,
        "device_online_seconds": settings.device_online_seconds,
        "device_offline_seconds": settings.device_offline_seconds,
        "thresholds": {
            "online_seconds": settings.device_online_seconds,
            "unstable_band": f"{settings.device_online_seconds}-{settings.device_offline_seconds}s",
            "offline_seconds": settings.device_offline_seconds,
            "note": (
                f"Online ≤{settings.device_online_seconds}s "
                f"(~3× Delay={settings.heartbeat_interval_hint}); "
                f"Unstable {settings.device_online_seconds}–{settings.device_offline_seconds}s; "
                f"Offline >{settings.device_offline_seconds}s"
            ),
        },
        "heartbeat_watchdog_seconds": settings.heartbeat_watchdog_seconds,
        "handshake": {
            "Delay": settings.heartbeat_interval_hint,
            "ErrorDelay": settings.handshake_error_delay,
            "TransInterval": 1,
            "Realtime": 1,
        },
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


@app.post("/api/devices/{serial}/release")
async def api_device_release(serial: str, credentials: HTTPBasicCredentials | None = Depends(security)):
    """Release held ADMS session/TCP for a device so the next getrequest can re-register."""
    _check_auth(credentials)
    updated = await devices_svc.release_device_connection(serial, reason="manual")
    if not updated:
        raise HTTPException(404, "Device not found")
    return updated


@app.get("/api/devices/{serial}/connection-history")
async def api_connection_history(
    serial: str,
    limit: int = 100,
    credentials: HTTPBasicCredentials | None = Depends(security),
):
    """Last heartbeat/attendance, disconnect/reconnect times, server errors."""
    _check_auth(credentials)
    device = await devices_svc.get_device(serial)
    if not device:
        raise HTTPException(404, "Device not found")
    summary = await history_svc.summary_for(serial)
    summary["connection_status"] = device.get("connection_status")
    summary["online"] = device.get("online")
    summary["unstable"] = device.get("unstable")
    summary["seconds_since_last_seen"] = device.get("seconds_since_last_seen")
    settings = get_settings()
    summary["thresholds"] = {
        "online_seconds": settings.device_online_seconds,
        "offline_seconds": settings.device_offline_seconds,
        "unstable_band": f"{settings.device_online_seconds}-{settings.device_offline_seconds}s",
        "note": (
            f"Online ≤{settings.device_online_seconds}s; "
            f"Unstable {settings.device_online_seconds}–{settings.device_offline_seconds}s; "
            f"Offline >{settings.device_offline_seconds}s"
        ),
    }
    summary["poll_metrics"] = await logs_svc.poll_metrics(serial, sample=40)
    if limit and limit < 20:
        summary["recent_events"] = (summary.get("recent_events") or [])[:limit]
    return summary


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
        timeout_keep_alive=max(1, int(settings.http_keep_alive_seconds)),
    )


if __name__ == "__main__":
    run()
