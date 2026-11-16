#!/usr/bin/env python3
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Local/dev defaults when not on VPS
os.environ.setdefault("DATA_DIR", str(ROOT / "data"))
os.environ.setdefault("DB_PATH", str(ROOT / "data" / "adms.db"))
os.environ.setdefault("HOST", "127.0.0.1")
os.environ.setdefault("PORT", "8081")
os.environ.setdefault("ADMIN_PASSWORD", "ChangeMeNow!")

from app.main import run  # noqa: E402

if __name__ == "__main__":
    run()
