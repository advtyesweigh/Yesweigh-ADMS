"""Daily attendance overview for CRM dashboard."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

from app.db import database as db
from app.services import devices as devices_svc
from app.services import employees as employees_svc

TZ = ZoneInfo("Asia/Kolkata")
DEFAULT_SHIFT_START = "09:00"
DEFAULT_SHIFT_END = "18:00"
LATE_AFTER = "09:15"
SHIFT_LABEL = "Morning (09:00 AM - 06:00 PM)"


def _today() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def _parse_time(punch_time: str) -> Optional[str]:
    """Return HH:MM from punch_time."""
    if not punch_time or len(punch_time) < 16:
        return None
    return punch_time[11:16]


def _fmt_ampm(hhmm: Optional[str]) -> str:
    if not hhmm:
        return "—"
    try:
        h, m = map(int, hhmm.split(":"))
        suffix = "AM" if h < 12 else "PM"
        h12 = h % 12 or 12
        return f"{h12:02d}:{m:02d} {suffix}"
    except ValueError:
        return hhmm


def _working_hours(check_in: Optional[str], check_out: Optional[str]) -> str:
    if not check_in or not check_out:
        return "—"
    try:
        a = datetime.strptime(check_in, "%H:%M")
        b = datetime.strptime(check_out, "%H:%M")
        if b <= a:
            return "—"
        delta = b - a
        mins = int(delta.total_seconds() // 60)
        return f"{mins // 60:02d}:{mins % 60:02d}"
    except ValueError:
        return "—"


def _status(check_in: Optional[str]) -> str:
    if not check_in:
        return "Absent"
    return "Late" if check_in > LATE_AFTER else "On Time"


def _initials(name: str, code: str) -> str:
    parts = [p for p in (name or "").split() if p]
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    if parts:
        return parts[0][:2].upper()
    return (code or "?")[:2].upper()


async def build_overview(
    *,
    date: Optional[str] = None,
    date_to: Optional[str] = None,
    branch: str = "",
    device: str = "",
    status: str = "",
    q: str = "",
) -> dict[str, Any]:
    day = date or _today()
    end = date_to or day

    emp_rows = await employees_svc.list_employees(limit=5000)
    emp_map = {e["employee_code"]: e for e in emp_rows}

    # Ensure codes from attendance exist in map
    att_codes = await db.fetchall(
        "SELECT DISTINCT employee_code FROM attendance WHERE punch_date BETWEEN ? AND ?",
        (day, end),
    )
    for r in att_codes:
        code = r["employee_code"]
        if code not in emp_map:
            emp_map[code] = {"employee_code": code, "name": "", "branch": "", "card": ""}

    # If still empty, all known employees
    if not emp_map:
        all_codes = await db.fetchall("SELECT DISTINCT employee_code FROM attendance")
        for r in all_codes:
            emp_map[r["employee_code"]] = {
                "employee_code": r["employee_code"],
                "name": "",
                "branch": "",
                "card": "",
            }

    punches = await db.fetchall(
        """SELECT employee_code, punch_time, punch_date, device_serial, device_name, branch
           FROM attendance
           WHERE punch_date BETWEEN ? AND ?
           ORDER BY punch_time ASC""",
        (day, end),
    )

    # Group first/last punch per employee for the primary day (or range end day for table)
    table_day = day
    by_emp: dict[str, list] = {}
    for p in punches:
        if p["punch_date"] != table_day and day == end:
            continue
        if day != end and p["punch_date"] != table_day:
            # for multi-day filter, show last day of range in table
            pass
        code = p["employee_code"]
        by_emp.setdefault(code, []).append(p)

    if day != end:
        # Prefer rows for `end` day in table; fall back to first day punches
        by_emp = {}
        for p in punches:
            if p["punch_date"] == end:
                by_emp.setdefault(p["employee_code"], []).append(p)
        table_day = end

    device_list = await devices_svc.list_devices()
    if device:
        device_list = [d for d in device_list if d.get("serial_number") == device or d.get("name") == device]
    online = sum(1 for d in device_list if d.get("online"))
    last_hb = None
    for d in device_list:
        hb = d.get("last_heartbeat_at") or d.get("last_attendance_at")
        if hb and (last_hb is None or hb > last_hb):
            last_hb = hb

    branches = sorted({(e.get("branch") or "").strip() for e in emp_map.values() if (e.get("branch") or "").strip()})
    for d in await devices_svc.list_devices():
        if (d.get("branch") or "").strip():
            branches.append(d["branch"].strip())
    branches = sorted(set(branches))

    rows: list[dict] = []
    present = on_time = late = absent = 0
    checkins = 0

    for code, emp in sorted(emp_map.items(), key=lambda x: (len(x[0]), x[0])):
        plist = by_emp.get(code) or []
        if branch and (emp.get("branch") or "") != branch and not any((p.get("branch") or "") == branch for p in plist):
            continue
        if device and not any(
            (p.get("device_serial") == device or p.get("device_name") == device) for p in plist
        ):
            # still include absent employees when filtering device? skip non-matching presents only
            if plist:
                continue

        first = plist[0] if plist else None
        last = plist[-1] if plist else None
        cin = _parse_time(first["punch_time"]) if first else None
        cout = _parse_time(last["punch_time"]) if last and last is not first else None
        st = _status(cin)
        name = (emp.get("name") or "").strip() or f"Employee {code}"
        emp_branch = (emp.get("branch") or (first.get("branch") if first else "") or "—") or "—"
        device_name = (first.get("device_name") or first.get("device_serial") or "—") if first else "—"

        if q:
            qq = q.lower()
            if qq not in name.lower() and qq not in code.lower():
                continue

        if status and status.lower() not in ("", "all", "all status"):
            want = status.lower().replace("_", " ")
            if st.lower() != want:
                continue

        if cin:
            present += 1
            checkins += 1
            if st == "On Time":
                on_time += 1
            else:
                late += 1
        else:
            absent += 1

        rows.append(
            {
                "employee_code": code,
                "name": name,
                "initials": _initials(name, code),
                "title": "",
                "branch": emp_branch,
                "shift": SHIFT_LABEL,
                "check_in": _fmt_ampm(cin),
                "check_out": _fmt_ampm(cout),
                "status": st,
                "device": device_name,
                "working_hours": _working_hours(cin, cout),
            }
        )

    total = len(rows) if (q or status or branch or device) else max(len(emp_map), present + absent)
    if not (q or status or branch or device):
        # recompute absent against full roster
        total = len(emp_map)
        absent = max(0, total - present)

    on_leave = 0  # no leave source yet
    pct = lambda n: round((n / total) * 100, 1) if total else 0.0

    # 7-day trend ending at table_day
    trend = []
    base = datetime.strptime(table_day, "%Y-%m-%d")
    for i in range(6, -1, -1):
        d = (base - timedelta(days=i)).strftime("%Y-%m-%d")
        day_punches = await db.fetchall(
            """SELECT employee_code, MIN(substr(punch_time,12,5)) AS first_t
               FROM attendance WHERE punch_date=? GROUP BY employee_code""",
            (d,),
        )
        ot = lt = 0
        for r in day_punches:
            t = r["first_t"]
            if t and t <= LATE_AFTER:
                ot += 1
            elif t:
                lt += 1
        trend.append({"date": d, "label": datetime.strptime(d, "%Y-%m-%d").strftime("%b %d"), "on_time": ot, "late": lt})

    last_sync_label = "—"
    if last_hb:
        try:
            ts = datetime.fromisoformat(last_hb.replace("Z", "+00:00")).astimezone(TZ)
            if ts.strftime("%Y-%m-%d") == _today():
                last_sync_label = f"Today, {ts.strftime('%I:%M %p').lstrip('0')}"
            else:
                last_sync_label = ts.strftime("%d %b, %I:%M %p").lstrip("0")
        except ValueError:
            last_sync_label = last_hb

    return {
        "date": table_day,
        "date_from": day,
        "date_to": end,
        "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "kpis": {
            "total_employees": total,
            "present": present,
            "present_pct": pct(present),
            "on_time": on_time,
            "on_time_pct": pct(on_time),
            "late": late,
            "late_pct": pct(late),
            "on_leave": on_leave,
            "on_leave_pct": pct(on_leave),
            "absent": absent,
            "absent_pct": pct(absent),
            "checkins": checkins,
            "devices_online": online,
            "devices_total": len(await devices_svc.list_devices()),
        },
        "summary": {
            "present": present,
            "on_leave": on_leave,
            "absent": absent,
            "total": total,
        },
        "trend": trend,
        "insights": {
            "late_today": late,
            "on_leave": on_leave,
            "upcoming_shift": "Evening (02:00 PM - 10:00 PM)",
            "last_device_sync": last_sync_label,
        },
        "rows": rows,
        "filters": {
            "branches": branches or [],
            "devices": [
                {"serial": d.get("serial_number"), "name": d.get("name") or d.get("serial_number")}
                for d in await devices_svc.list_devices()
            ],
            "statuses": ["On Time", "Late", "On Leave", "Absent"],
            "shifts": [SHIFT_LABEL, "Evening (02:00 PM - 10:00 PM)"],
        },
        "dashboard_url": None,
    }
