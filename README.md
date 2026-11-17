# ADMS — Attendance Device Management System

Lightweight ZKTeco ADMS HTTP Push gateway.

**Architecture:** Fingerprint Machine → ADMS Push → VPS `:8081` → SQLite → Firebase

## Ports

- **8081 only** — ADMS HTTP Push (`/iclock/cdata`, `/iclock/getrequest`, …)
- Does **not** use 4370, 8080, or 8082

## Local / Dev

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python run.py
```

Open http://127.0.0.1:8081 — login `admin` / `ChangeMeNow!`

## Machine setup (X305 Pro)

- Server address: `103.176.85.102`
- Server port: `8081`
- Enable ADMS / Cloud / Push

## Firebase

Until Firebase is enabled, punches are stored in SQLite with `firebase_status=pending` (this is expected).

To clear the backlog:

1. Place a Firestore service-account JSON at `/opt/adms/secrets/firebase-service-account.json`
2. In `/opt/adms/.env` set `FIREBASE_ENABLED=true` (optional: `FIREBASE_COMPANY_ID=…`)
3. `systemctl restart adms.service`
4. Click **Retry Firebase** on the dashboard (drains ~1000 records per click; background loop continues the rest)

`POST /api/firebase/test` reports whether sync is ready. Without the JSON, Retry cannot invent credentials.
