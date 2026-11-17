"""In-memory ADMS device session registry + stale TCP release.

ZK Push is device-initiated HTTP. After ADMS restart or a dropped poll, some
firmwares leave a half-open TCP to :8081 and stop calling /iclock/getrequest.
We track last peers, mark sessions released when heartbeats go stale, and
best-effort shut down those sockets so the next device poll can re-register.
"""
from __future__ import annotations

import glob
import logging
import os
import socket
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("adms.sessions")

# Process boot time — heartbeats older than this never count as Online.
_service_started_at: Optional[datetime] = None


@dataclass
class DeviceSession:
    serial: str
    last_ip: str = ""
    last_port: int = 0
    last_seen_at: Optional[datetime] = None
    released: bool = False
    released_at: Optional[datetime] = None
    release_reason: str = ""
    reconnect_count: int = 0


_sessions: dict[str, DeviceSession] = {}


def mark_service_started(when: Optional[datetime] = None) -> datetime:
    global _service_started_at
    _service_started_at = when or datetime.now(timezone.utc).replace(microsecond=0)
    return _service_started_at


def service_started_at() -> Optional[datetime]:
    return _service_started_at


def get_session(serial: str) -> Optional[DeviceSession]:
    return _sessions.get(serial)


def list_sessions() -> list[DeviceSession]:
    return list(_sessions.values())


def note_heartbeat(serial: str, ip: str = "", port: Optional[int] = None) -> dict:
    """Record a live poll/handshake. Clears released flag (clean reconnect)."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    sess = _sessions.get(serial)
    reconnected = False
    prior_reason = ""
    if sess is None:
        sess = DeviceSession(serial=serial)
        _sessions[serial] = sess
    elif sess.released:
        reconnected = True
        prior_reason = sess.release_reason or "stale"
        sess.reconnect_count += 1
        logger.info(
            "device %s reconnected after release (%s) from %s",
            serial,
            prior_reason,
            ip or sess.last_ip,
        )
    sess.last_ip = ip or sess.last_ip
    if port is not None:
        sess.last_port = port
    sess.last_seen_at = now
    sess.released = False
    sess.released_at = None
    sess.release_reason = ""
    return {
        "serial": serial,
        "reconnected": reconnected,
        "reconnect_count": sess.reconnect_count,
        "last_ip": sess.last_ip,
        "release_reason": prior_reason,
    }


def release_session(
    serial: str,
    *,
    reason: str = "heartbeat_timeout",
    close_tcp: bool = True,
    peer_ip: str = "",
    local_port: int = 8081,
) -> dict:
    """Mark session released and optionally RST idle TCP to the device peer."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    sess = _sessions.get(serial)
    if sess is None:
        sess = DeviceSession(serial=serial)
        _sessions[serial] = sess
    ip = peer_ip or sess.last_ip
    already = sess.released
    sess.released = True
    sess.released_at = now
    sess.release_reason = reason
    closed = 0
    if close_tcp and ip:
        closed = close_peer_sockets(ip, local_port=local_port)
    if not already:
        logger.info(
            "released session sn=%s reason=%s ip=%s tcp_closed=%s",
            serial,
            reason,
            ip or "-",
            closed,
        )
    return {
        "serial": serial,
        "released": True,
        "already_released": already,
        "reason": reason,
        "peer_ip": ip,
        "tcp_sockets_closed": closed,
        "released_at": now.isoformat(),
    }


def release_all(*, reason: str = "service_restart", close_tcp: bool = False, local_port: int = 8081) -> int:
    """On boot: every device starts Offline until a real heartbeat arrives."""
    mark_service_started()
    n = 0
    for serial, sess in list(_sessions.items()):
        release_session(
            serial,
            reason=reason,
            close_tcp=close_tcp,
            peer_ip=sess.last_ip,
            local_port=local_port,
        )
        n += 1
    logger.info("session registry cleared on %s (tracked=%s)", reason, n)
    return n


def _hex_ip_port_ipv4(entry_local: str) -> tuple[str, int]:
    # /proc/net/tcp: local_address is IP:port hex little-endian
    ip_hex, port_hex = entry_local.split(":")
    port = int(port_hex, 16)
    packed = struct.pack("<L", int(ip_hex, 16))
    ip = socket.inet_ntoa(packed)
    return ip, port


def close_peer_sockets(peer_ip: str, local_port: int = 8081) -> int:
    """Best-effort shutdown of ESTABLISHED sockets to peer_ip on local_port (Linux)."""
    if not peer_ip or not os.path.exists("/proc/net/tcp"):
        return 0

    inode_to_fd: dict[str, int] = {}
    pid = os.getpid()
    for fd_path in glob.glob(f"/proc/{pid}/fd/*"):
        try:
            target = os.readlink(fd_path)
        except OSError:
            continue
        if target.startswith("socket:["):
            inode_to_fd[target[8:-1]] = int(os.path.basename(fd_path))

    targets: list[int] = []
    try:
        with open("/proc/net/tcp", "r", encoding="utf-8") as fh:
            next(fh, None)
            for line in fh:
                parts = line.split()
                if len(parts) < 10:
                    continue
                local = parts[1]
                rem = parts[2]
                state = parts[3]
                inode = parts[9]
                # 01 = ESTABLISHED
                if state != "01":
                    continue
                try:
                    lip, lport = _hex_ip_port_ipv4(local)
                    rip, _rport = _hex_ip_port_ipv4(rem)
                except Exception:  # noqa: BLE001
                    continue
                if lport != local_port:
                    continue
                if rip != peer_ip:
                    continue
                fd = inode_to_fd.get(inode)
                if fd is not None:
                    targets.append(fd)
    except OSError as exc:
        logger.warning("close_peer_sockets parse failed: %s", exc)
        return 0

    closed = 0
    for fd in targets:
        try:
            dup = os.dup(fd)
        except OSError:
            continue
        try:
            sock = socket.socket(fileno=dup)
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
            closed += 1
        except OSError:
            try:
                os.close(dup)
            except OSError:
                pass
    if closed:
        logger.info("shut down %s idle TCP socket(s) to %s:%s", closed, peer_ip, local_port)
    return closed
