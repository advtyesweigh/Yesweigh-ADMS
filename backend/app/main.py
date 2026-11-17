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
    return {
        "ok": True,
        "app": "ADMS",
        "port": settings.port,
        "firebase_enabled": settings.firebase_enabled,
        "sync": sync,
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
    return {"devices": devices, "sync": sync, "port": get_settings().port}


@app.get("/api/devices")
async def api_devices(credentials: HTTPBasicCredentials | None = Depends(security)):
    _check_auth(credentials)
    return await devices_svc.list_devices()


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
    _check_auth(credentials)
    n = await attendance_svc.retry_failed()
    return {"retried": n}


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
