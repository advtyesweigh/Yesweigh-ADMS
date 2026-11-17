"""Heartbeat watchdog — release stale ADMS sessions when polls stop.

Also runs a connection-lost reconnect watcher: while a session stays released,
periodically RST lingering TCP with exponential backoff so the device can
re-open /iclock/getrequest after idle/NAT drops (~evening stalls).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.services import devices as devices_svc
from app.services import logs as logs_svc
from app.services import sessions as sessions_svc

logger = logging.getLogger("adms.watchdog")

# Reconnect nudge backoff (seconds) while awaiting device re-poll
_RECONNECT_BACKOFF = (30, 60, 120, 300)
_nudge_state: dict[str, dict] = {}  # serial -> {attempts, next_at}


async def _release_stale_once() -> int:
    settings = get_settings()
    offline_after = settings.device_offline_seconds
    now = datetime.now(timezone.utc)
    released = 0
    rows = await devices_svc.list_devices_raw()
    for d in rows:
        serial = str(d.get("serial_number") or "")
        if not serial or d.get("status") == "disabled":
            continue
        if serial.upper() == "TEST":
            continue
        hb = d.get("last_heartbeat_at")
        if not hb:
            # Never heartbeated in DB — ensure registry shows released
            sess = sessions_svc.get_session(serial)
            if sess and not sess.released:
                sessions_svc.release_session(
                    serial,
                    reason="no_heartbeat",
                    close_tcp=True,
                    peer_ip=str(d.get("last_ip") or ""),
                    local_port=settings.port,
                )
                released += 1
            continue
        try:
            ts = datetime.fromisoformat(str(hb).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        age = now - ts
        sess = sessions_svc.get_session(serial)
        if age <= timedelta(seconds=offline_after):
            # Only clear nudge when a live post-release session is online again.
            # After ADMS restart the DB stamp can still look "fresh" while the
            # in-memory session stays released until the next real poll.
            if sess and not sess.released:
                _nudge_state.pop(serial, None)
            continue
        if sess and sess.released:
            # Already released; reconnect watcher handles repeated TCP close
            continue
        result = sessions_svc.release_session(
            serial,
            reason="heartbeat_timeout",
            close_tcp=True,
            peer_ip=str(d.get("last_ip") or ""),
            local_port=settings.port,
        )
        released += 1
        _nudge_state[serial] = {"attempts": 0, "next_at": now}
        try:
            from app.services import connection_history as history_svc

            await history_svc.record_event(
                serial,
                "disconnect",
                remote_ip=str(d.get("last_ip") or ""),
                detail=(
                    f"reason=heartbeat_timeout age_s={int(age.total_seconds())} "
                    f"tcp_closed={result.get('tcp_sockets_closed', 0)}"
                ),
            )
            await logs_svc.log_comm(
                serial,
                str(d.get("last_ip") or ""),
                int(d.get("last_port") or settings.port),
                "WATCHDOG",
                "/internal/heartbeat-watchdog",
                "session-release",
                200,
                0,
                detail=(
                    f"reason=heartbeat_timeout age_s={int(age.total_seconds())} "
                    f"tcp_closed={result.get('tcp_sockets_closed', 0)}"
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("watchdog log failed: %s", exc)
    return released


async def _reconnect_nudge_once() -> int:
    """For released sessions: periodically close half-open TCP (backoff)."""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    nudged = 0
    for sess in sessions_svc.list_sessions():
        if not sess.released or sess.serial.upper() == "TEST":
            continue
        ip = sess.last_ip or ""
        if not ip:
            device = await devices_svc.get_device(sess.serial)
            ip = str((device or {}).get("last_ip") or "")
        state = _nudge_state.get(sess.serial) or {"attempts": 0, "next_at": now}
        next_at = state.get("next_at") or now
        if isinstance(next_at, datetime) and now < next_at:
            continue
        closed = 0
        if ip:
            closed = sessions_svc.close_peer_sockets(ip, local_port=settings.port)
        attempts = int(state.get("attempts") or 0)
        delay = _RECONNECT_BACKOFF[min(attempts, len(_RECONNECT_BACKOFF) - 1)]
        _nudge_state[sess.serial] = {
            "attempts": attempts + 1,
            "next_at": now + timedelta(seconds=delay),
        }
        nudged += 1
        logger.info(
            "reconnect nudge sn=%s attempt=%s tcp_closed=%s next_in=%ss",
            sess.serial,
            attempts + 1,
            closed,
            delay,
        )
        try:
            await logs_svc.log_comm(
                sess.serial,
                ip,
                settings.port,
                "WATCHDOG",
                "/internal/reconnect-watcher",
                "awaiting-reconnect",
                200,
                0,
                detail=f"attempt={attempts + 1} tcp_closed={closed} backoff_s={delay}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("reconnect nudge log failed: %s", exc)
    return nudged


async def watchdog_loop(stop: asyncio.Event) -> None:
    settings = get_settings()
    interval = max(10, int(settings.heartbeat_watchdog_seconds))
    logger.info(
        "heartbeat watchdog + reconnect watcher started (every %ss, offline after %ss)",
        interval,
        settings.device_offline_seconds,
    )
    # First pass soon after boot so restart does not leave stale TCP around
    try:
        await asyncio.wait_for(stop.wait(), timeout=5)
        return
    except asyncio.TimeoutError:
        pass
    while not stop.is_set():
        try:
            n = await _release_stale_once()
            if n:
                logger.info("watchdog released %s stale session(s)", n)
            await _reconnect_nudge_once()
        except Exception as exc:  # noqa: BLE001
            logger.exception("watchdog tick failed: %s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            break
        except asyncio.TimeoutError:
            continue
