"""ZKTeco ADMS / iClock PUSH protocol helpers.

Returns plain-text responses expected by devices (not JSON).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional
from zoneinfo import ZoneInfo

ATTLOG_RE = re.compile(
    r"^(?P<code>\S+)\t(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
    r"(?:\t(?P<status>\S+))?(?:\t(?P<verify>\S+))?(?:\t(?P<workcode>\S+))?.*$"
)


@dataclass
class AttendancePunch:
    employee_code: str
    punch_time: str
    punch_status: str
    verify_method: str
    work_code: str
    raw_line: str

    @property
    def punch_date(self) -> str:
        return self.punch_time[:10]


def parse_attlog_body(body: str) -> List[AttendancePunch]:
    punches: List[AttendancePunch] = []
    for line in body.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = line.strip()
        if not line:
            continue
        m = ATTLOG_RE.match(line)
        if not m:
            # fallback: space-separated
            parts = line.split()
            if len(parts) >= 2 and re.match(r"\d{4}-\d{2}-\d{2}", parts[1]):
                ts = f"{parts[1]} {parts[2]}" if len(parts) > 2 and ":" in parts[2] else parts[1]
                punches.append(
                    AttendancePunch(
                        employee_code=parts[0],
                        punch_time=ts,
                        punch_status=parts[3] if len(parts) > 3 else "0",
                        verify_method=parts[4] if len(parts) > 4 else "1",
                        work_code=parts[5] if len(parts) > 5 else "",
                        raw_line=line,
                    )
                )
            continue
        punches.append(
            AttendancePunch(
                employee_code=m.group("code"),
                punch_time=m.group("ts"),
                punch_status=m.group("status") or "0",
                verify_method=m.group("verify") or "1",
                work_code=m.group("workcode") or "",
                raw_line=line,
            )
        )
    return punches


@dataclass
class DeviceUser:
    employee_code: str
    name: str = ""
    card: str = ""
    privilege: str = ""
    raw_line: str = ""


def _kv_pairs(line: str) -> dict[str, str]:
    """Parse tab/space separated Key=Value tokens (ZK USERINFO / OPERLOG USER lines)."""
    out: dict[str, str] = {}
    # Strip optional leading "USER "
    text = re.sub(r"^USER\s+", "", line.strip(), flags=re.IGNORECASE)
    for token in re.split(r"[\t]+|\s{2,}", text):
        token = token.strip()
        if not token:
            continue
        if "=" in token:
            k, v = token.split("=", 1)
            out[k.strip().upper()] = v.strip()
        else:
            # single space Key=Value still possible without tab
            for part in token.split():
                if "=" in part:
                    k, v = part.split("=", 1)
                    out[k.strip().upper()] = v.strip()
    return out


def _redact_secrets(raw: str) -> str:
    """Strip device passwords from logged/stored raw lines (never import Passwd)."""
    return re.sub(
        r"(?i)\b(PASSWD|PASSWORD|PWD)\s*=\s*[^\t\s]*",
        r"\1=***",
        raw or "",
    )


def _user_from_kv(kv: dict[str, str], raw: str) -> Optional[DeviceUser]:
    code = kv.get("PIN") or kv.get("PIN2") or kv.get("USERID") or kv.get("UID") or ""
    code = str(code).strip()
    if not code:
        return None
    name = kv.get("NAME") or kv.get("UNAME") or ""
    card = kv.get("CARD") or kv.get("CARDNO") or kv.get("VICECARD") or ""
    privilege = kv.get("PRI") or kv.get("PRIVILEGE") or ""
    # Intentionally ignore PASSWD / PASSWORD / PWD — never store device passwords
    return DeviceUser(
        employee_code=code,
        name=name.strip(),
        card=str(card).strip(),
        privilege=str(privilege).strip(),
        raw_line=_redact_secrets(raw),
    )


_PASSWD_RE = re.compile(r"(Passwd|Password|Pwd)\s*=\s*[^\t\s]*", re.IGNORECASE)


def redact_secrets(text: str) -> str:
    """Strip device passwords from log / raw lines (never persist templates or secrets)."""
    return _PASSWD_RE.sub(r"\1=***", text or "")


def parse_userinfo_body(body: str) -> List[DeviceUser]:
    """Parse USERINFO / OPERLOG bodies that carry employee master rows.

    Only enrollment ID (PIN) + name (+ optional card) are kept.
    Passwords and biometric templates are ignored / redacted.
    """
    users: List[DeviceUser] = []
    seen: set[str] = set()
    for line in body.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        raw = line.strip()
        if not raw:
            continue
        upper = raw.upper()
        # Skip pure operation-log rows without user fields
        if upper.startswith("OPLOG") and "PIN=" not in upper and "NAME=" not in upper:
            continue
        if "PIN=" in upper or upper.startswith("USER ") or (upper.startswith("USER") and "=" in raw):
            kv = _kv_pairs(raw)
            # Never carry Passwd into stored structures
            kv.pop("PASSWD", None)
            kv.pop("PASSWORD", None)
            kv.pop("PWD", None)
            safe_raw = redact_secrets(raw)
            user = _user_from_kv(kv, safe_raw)
            if user and user.employee_code not in seen:
                seen.add(user.employee_code)
                users.append(user)
            continue
        # Tab-delimited positional: PIN Name Pri Passwd Card Grp TZ Verify
        # parts[3] is password — intentionally never read or stored
        if "\t" in raw and "=" not in raw:
            parts = raw.split("\t")
            if parts and parts[0].strip() and not re.match(r"^\d{4}-\d{2}-\d{2}", parts[0]):
                code = parts[0].strip()
                if code.upper() in ("PIN", "OPLOG", "FP", "FACE"):
                    continue
                if code not in seen:
                    seen.add(code)
                    safe_parts = list(parts)
                    if len(safe_parts) > 3:
                        safe_parts[3] = "***"
                    users.append(
                        DeviceUser(
                            employee_code=code,
                            name=(parts[1].strip() if len(parts) > 1 else ""),
                            card=(parts[4].strip() if len(parts) > 4 else ""),
                            privilege=(parts[2].strip() if len(parts) > 2 else ""),
                            raw_line="\t".join(safe_parts),
                        )
                    )
    return users


def make_event_id(serial: str, punch: AttendancePunch) -> str:
    raw = f"{serial}|{punch.employee_code}|{punch.punch_time}|{punch.punch_status}|{punch.verify_method}|{punch.work_code}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def handshake_response(
    *,
    serial: str,
    stamp_attlog: str = "0",
    stamp_operlog: str = "0",
    timezone: str = "Asia/Kolkata",
    heartbeat: int = 30,
) -> str:
    """Config blob returned to device on options=all / initial cdata."""
    # Flags commonly accepted by ZKTeco push firmware
    lines = [
        f"GET OPTION FROM: {serial}",
        "Stamp=9999",
        f"OpStamp={stamp_operlog}",
        f"ATTLOGStamp={stamp_attlog}",
        "ErrorDelay=60",
        f"Delay={heartbeat}",
        "TransTimes=00:00;14:00",
        "TransInterval=1",
        "TransFlag=TransData AttLog\tOpLog\tAttPhoto\tEnrollUser\tChgUser\tEnrollFP\tChgFP\tFPImag\tFACE\tUserPic",
        "Realtime=1",
        "Encrypt=0",
        f"TimeZone=5.5",  # Asia/Kolkata
        "ServerVer=2.4.1",
        "PushProtVer=2.4.1",
        "PushOptionsFlag=1",
    ]
    return "\n".join(lines) + "\n"


def ok_response(count: Optional[int] = None) -> str:
    if count is None:
        return "OK"
    return f"OK: {count}"


def ping_response() -> str:
    return "OK"


VERIFY_LABELS = {
    "0": "Password",
    "1": "Fingerprint",
    "2": "Card",
    "3": "Password",
    "4": "Card",
    "15": "Face",
    "25": "Palm",
}

PUNCH_LABELS = {
    "0": "Check-In",
    "1": "Check-Out",
    "2": "Break-Out",
    "3": "Break-In",
    "4": "OT-In",
    "5": "OT-Out",
}


def verify_label(code: str) -> str:
    return VERIFY_LABELS.get(str(code), f"Code {code}")


def punch_label(code: str) -> str:
    return PUNCH_LABELS.get(str(code), f"Status {code}")


def now_iso(tz: str = "Asia/Kolkata") -> str:
    return datetime.now(ZoneInfo(tz)).isoformat(timespec="seconds")
