"""ADMS HTTP endpoint handlers for /iclock/* and legacy .aspx variants."""
from __future__ import annotations

import time
from typing import Optional
from urllib.parse import parse_qs

from fastapi import APIRouter, Request, Response
from fastapi.responses import PlainTextResponse

from app.adms.protocol import (
    handshake_response,
    make_event_id,
    ok_response,
    parse_attlog_body,
    parse_userinfo_body,
    ping_response,
    redact_secrets,
)
from app.config import get_settings
from app.services import attendance as attendance_svc
from app.services import commands as commands_svc
from app.services import devices as devices_svc
from app.services import employees as employees_svc
from app.services import logs as logs_svc

router = APIRouter(tags=["adms"])


def _plain(body: str, status: int = 200) -> PlainTextResponse:
    return PlainTextResponse(content=body, status_code=status, media_type="text/plain")


def _sn(request: Request) -> str:
    sn = request.query_params.get("SN") or request.query_params.get("sn") or ""
    return sn.strip()


async def _guard_device(sn: str, request: Request, port: int) -> tuple[Optional[dict], Optional[Response]]:
    settings = get_settings()
    ip = request.client.host if request.client else ""
    if settings.allowlist_ips and ip not in settings.allowlist_ips:
        await logs_svc.log_comm(sn, ip, port, request.method, str(request.url.path), "reject", 403, 0, "IP not allowlisted")
        return None, _plain("ERROR: IP denied", 403)
    if not sn:
        await logs_svc.log_comm("", ip, port, request.method, str(request.url.path), "reject", 400, 0, "Missing SN")
        return None, _plain("ERROR: Missing SN", 400)

    device = await devices_svc.get_or_create_pending(sn, ip=ip, port=port)
    if device["status"] == "disabled":
        await logs_svc.log_comm(sn, ip, port, request.method, str(request.url.path), "reject", 403, 0, "Device disabled")
        return None, _plain("ERROR: Device disabled", 403)
    # pending devices may handshake but attendance is held until approved
    return device, None


async def handle_cdata(request: Request, listen_port: int) -> Response:
    t0 = time.perf_counter()
    sn = _sn(request)
    device, err = await _guard_device(sn, request, listen_port)
    if err:
        return err
    assert device
    ip = request.client.host if request.client else ""
    options = request.query_params.get("options", "")
    table = (request.query_params.get("table") or "").upper()

    try:
        if request.method == "GET" and (options == "all" or not table):
            body = handshake_response(
                serial=sn,
                stamp_attlog=device.get("stamp_attlog") or "0",
                stamp_operlog=device.get("stamp_operlog") or "0",
                timezone=device.get("timezone") or get_settings().timezone_default,
                heartbeat=get_settings().heartbeat_interval_hint,
            )
            await devices_svc.touch_heartbeat(sn, ip=ip, port=listen_port)
            await logs_svc.log_comm(sn, ip, listen_port, "GET", "/iclock/cdata", "handshake", 200, int((time.perf_counter() - t0) * 1000))
            return _plain(body)

        raw = (await request.body()).decode("utf-8", errors="replace")
        if len(raw.encode("utf-8")) > get_settings().request_body_max_bytes:
            return _plain("ERROR: body too large", 413)

        if table in ("ATTLOG", "ATT"):
            if device["status"] != "approved":
                # Accept into queue only after approval — still ACK to avoid device flood loops carefully
                await logs_svc.log_comm(sn, ip, listen_port, "POST", "/iclock/cdata", "attlog-pending-device", 200, int((time.perf_counter() - t0) * 1000), "device not approved")
                return _plain(ok_response(0))

            punches = parse_attlog_body(raw)
            saved = 0
            for punch in punches:
                eid = make_event_id(sn, punch)
                inserted = await attendance_svc.save_punch(
                    event_id=eid,
                    punch=punch,
                    device_serial=sn,
                    device_name=device.get("name") or sn,
                    branch=device.get("branch") or "",
                )
                if inserted:
                    saved += 1
            stamp = request.query_params.get("Stamp") or request.query_params.get("stamp")
            if stamp:
                await devices_svc.update_stamp(sn, attlog=stamp)
            await devices_svc.bump_records(sn, saved)
            await devices_svc.touch_heartbeat(sn, ip=ip, port=listen_port, attendance=True)
            await logs_svc.log_comm(sn, ip, listen_port, "POST", "/iclock/cdata", "attlog", 200, int((time.perf_counter() - t0) * 1000), detail=f"saved={saved}")
            return _plain(ok_response(saved if saved else len(punches)))

        if table in ("USERINFO", "USER", "OPERLOG", "OPER"):
            users = parse_userinfo_body(raw)
            saved = 0
            if users and device["status"] == "approved":
                # Persist PIN + Name only (no templates / passwords). Does not write back to device.
                saved = await employees_svc.upsert_many(
                    users,
                    device_serial=sn,
                    branch=device.get("branch") or "",
                )
                named = sum(1 for u in users if (u.name or "").strip())
                await devices_svc.mark_userinfo_received(sn, named=named > 0)
            stamp = request.query_params.get("Stamp") or request.query_params.get("stamp")
            if stamp and table in ("OPERLOG", "OPER"):
                await devices_svc.update_stamp(sn, operlog=stamp)
            await devices_svc.touch_heartbeat(sn, ip=ip, port=listen_port)
            snippet = redact_secrets(raw).replace("\n", "\\n")[:240]
            await logs_svc.log_comm(
                sn,
                ip,
                listen_port,
                request.method,
                "/iclock/cdata",
                table.lower(),
                200,
                int((time.perf_counter() - t0) * 1000),
                detail=f"users={len(users)} saved={saved} body={snippet}",
            )
            return _plain(ok_response(saved if saved else len(users)))

        # Biometric / photo tables — ACK only; never store templates
        if table in ("FINGER", "FP", "FACE", "USERPIC", "FPIMAG", "BIODATA", "TEMPLATE"):
            await devices_svc.touch_heartbeat(sn, ip=ip, port=listen_port)
            await logs_svc.log_comm(
                sn,
                ip,
                listen_port,
                request.method,
                "/iclock/cdata",
                table.lower(),
                200,
                int((time.perf_counter() - t0) * 1000),
                detail="ignored-template",
            )
            return _plain(ok_response())

        # Enroll / change-user realtime tables: parse if body carries PIN/NAME; never auto-query unless enabled
        if table in ("ENROLLUSER", "CHGUSER", "ENROLLFP", "CHGFP"):
            users = parse_userinfo_body(raw) if raw else []
            saved = 0
            settings = get_settings()
            if users and device["status"] == "approved":
                # Passive save of enroll payload is safe (device already enrolled locally)
                saved = await employees_svc.upsert_many(
                    users,
                    device_serial=sn,
                    branch=device.get("branch") or "",
                )
                if saved:
                    await devices_svc.mark_userinfo_received(sn, named=any((u.name or "").strip() for u in users))
            # Optional active pull for other users — gated; default off until X305 verified
            if settings.auto_sync_users and device["status"] == "approved":
                await commands_svc.enqueue(sn, "QUERY_USERINFO")
            await devices_svc.touch_heartbeat(sn, ip=ip, port=listen_port)
            await logs_svc.log_comm(
                sn,
                ip,
                listen_port,
                request.method,
                "/iclock/cdata",
                table.lower(),
                200,
                int((time.perf_counter() - t0) * 1000),
                detail=f"users={len(users)} saved={saved} auto_sync={settings.auto_sync_users}",
            )
            return _plain(ok_response(saved if saved else len(users)))

        # other tables — acknowledge without failing the device
        await devices_svc.touch_heartbeat(sn, ip=ip, port=listen_port)
        snippet = redact_secrets(raw).replace("\n", "\\n")[:240] if raw else ""
        await logs_svc.log_comm(
            sn,
            ip,
            listen_port,
            request.method,
            "/iclock/cdata",
            table or "other",
            200,
            int((time.perf_counter() - t0) * 1000),
            detail=snippet,
        )
        return _plain(ok_response())
    except Exception as exc:  # noqa: BLE001
        await logs_svc.log_comm(sn, ip, listen_port, request.method, "/iclock/cdata", "error", 500, int((time.perf_counter() - t0) * 1000), str(exc))
        return _plain("ERROR", 500)


async def handle_getrequest(request: Request, listen_port: int) -> Response:
    t0 = time.perf_counter()
    sn = _sn(request)
    device, err = await _guard_device(sn, request, listen_port)
    if err:
        return err
    assert device
    ip = request.client.host if request.client else ""
    await devices_svc.touch_heartbeat(sn, ip=ip, port=listen_port)

    if device["status"] != "approved":
        await logs_svc.log_comm(sn, ip, listen_port, "GET", "/iclock/getrequest", "poll", 200, int((time.perf_counter() - t0) * 1000))
        return _plain("OK")

    cmds = await commands_svc.next_pending_batch(sn, limit=25)
    if not cmds:
        await logs_svc.log_comm(sn, ip, listen_port, "GET", "/iclock/getrequest", "poll", 200, int((time.perf_counter() - t0) * 1000))
        return _plain("OK")

    # Safe verified commands — one or more C:<id>:<cmd> lines (each ends with \\n)
    lines = [commands_svc.format_device_command(cmd) for cmd in cmds]
    body = "".join(lines)
    await commands_svc.mark_sent_many([cmd["id"] for cmd in cmds])
    await logs_svc.log_comm(
        sn,
        ip,
        listen_port,
        "GET",
        "/iclock/getrequest",
        "command",
        200,
        int((time.perf_counter() - t0) * 1000),
        detail=body.replace("\n", " | ").strip(),
    )
    return _plain(body)


async def handle_devicecmd(request: Request, listen_port: int) -> Response:
    t0 = time.perf_counter()
    sn = _sn(request)
    device, err = await _guard_device(sn, request, listen_port)
    if err:
        return err
    assert device
    ip = request.client.host if request.client else ""
    raw = (await request.body()).decode("utf-8", errors="replace")
    await commands_svc.acknowledge(sn, raw)
    await devices_svc.touch_heartbeat(sn, ip=ip, port=listen_port)
    await logs_svc.log_comm(sn, ip, listen_port, request.method, "/iclock/devicecmd", "ack", 200, int((time.perf_counter() - t0) * 1000))
    return _plain(ok_response())


async def handle_ping(request: Request, listen_port: int) -> Response:
    sn = _sn(request)
    ip = request.client.host if request.client else ""
    if sn:
        await devices_svc.touch_heartbeat(sn, ip=ip, port=listen_port)
    await logs_svc.log_comm(sn, ip, listen_port, "GET", "/iclock/ping", "ping", 200, 0)
    return _plain(ping_response())


async def handle_registry(request: Request, listen_port: int) -> Response:
    sn = _sn(request)
    device, err = await _guard_device(sn, request, listen_port)
    if err:
        return err
    assert device
    ip = request.client.host if request.client else ""
    await devices_svc.touch_heartbeat(sn, ip=ip, port=listen_port)
    await logs_svc.log_comm(sn, ip, listen_port, request.method, "/iclock/registry", "registry", 200, 0)
    return _plain(ok_response())


def mount_adms_routes(app_router: APIRouter, listen_port: int) -> None:
    """Register modern + legacy .aspx style paths for a given listener."""

    async def cdata(request: Request):
        return await handle_cdata(request, listen_port)

    async def getrequest(request: Request):
        return await handle_getrequest(request, listen_port)

    async def devicecmd(request: Request):
        return await handle_devicecmd(request, listen_port)

    async def ping(request: Request):
        return await handle_ping(request, listen_port)

    async def registry(request: Request):
        return await handle_registry(request, listen_port)

    paths = [
        ("/iclock/cdata", cdata, ["GET", "POST"]),
        ("/iclock/cdata.aspx", cdata, ["GET", "POST"]),
        ("/iclock/getrequest", getrequest, ["GET"]),
        ("/iclock/getrequest.aspx", getrequest, ["GET"]),
        ("/iclock/devicecmd", devicecmd, ["GET", "POST"]),
        ("/iclock/devicecmd.aspx", devicecmd, ["GET", "POST"]),
        ("/iclock/ping", ping, ["GET"]),
        ("/iclock/ping.aspx", ping, ["GET"]),
        ("/iclock/registry", registry, ["GET", "POST"]),
        ("/iclock/registry.aspx", registry, ["GET", "POST"]),
    ]
    for path, endpoint, methods in paths:
        app_router.add_api_route(path, endpoint, methods=methods, response_class=PlainTextResponse)
