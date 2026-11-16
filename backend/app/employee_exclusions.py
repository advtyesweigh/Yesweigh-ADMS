"""Past/left enrollment codes — never re-upsert into ADMS employees.

CRM also excludes these from sync/import. New hires start at code 24+.
"""
from __future__ import annotations

import os
from functools import lru_cache

EXCLUDED_EMPLOYEE_CODES: frozenset[str] = frozenset(
    {
        "1",
        "2",
        "4",
        "5",
        "7",
        "8",
        "9",
        "10",
        "11",
        "12",
        "13",
        "15",
        "16",
        "17",
        "21",
        "55",
    }
)


def _env_extra_codes() -> frozenset[str]:
    raw = (os.environ.get("ADMS_EXCLUDED_EMPLOYEE_CODES") or "").strip()
    if not raw:
        return frozenset()
    return frozenset(p.strip() for p in raw.replace(";", ",").split(",") if p.strip())


@lru_cache
def excluded_employee_codes() -> frozenset[str]:
    return frozenset({*EXCLUDED_EMPLOYEE_CODES, *_env_extra_codes()})


def is_excluded_employee_code(code: str | None) -> bool:
    c = (code or "").strip()
    if not c:
        return False
    excluded = excluded_employee_codes()
    if c in excluded:
        return True
    if c.isdigit() and str(int(c)) in excluded:
        return True
    return False
