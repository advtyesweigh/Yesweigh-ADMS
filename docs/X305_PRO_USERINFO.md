# X305 Pro — USERINFO / auto-sync probe

**Date:** 2026-11-14  
**Device serial:** `0IN7010596123100370`  
**ADMS:** `http://103.176.85.102:8081`

## Observed behaviour

| Check | Result |
|-------|--------|
| ADMS Push (ATTLOG) | Supported — 10k+ punches stored |
| Device online at probe | **Offline** (last heartbeat ~2026-11-14T13:38:24Z) |
| `stamp_operlog` | Still `0` (no OPERLOG stamp advanced) |
| Named employees in ADMS DB | **0 / 20** (codes exist from attendance only) |
| Recent `/api/logs` categories | `poll`, `handshake`, `attlog` only — **no `userinfo` / `user` / `operlog`** |
| Queued `DATA QUERY USERINFO` | Commands id 268 (bulk) + 269 (`PIN=1`) left **pending** until device polls again |

## Conclusion

- **USERINFO / name pull: not verified** on this X305 Pro session (device offline; historically no user-master uploads in recent logs).
- **Auto-sync of new enrollments: keep DISABLED** (`AUTO_SYNC_USERS=false`, default) until a live session confirms the device answers `DATA QUERY USERINFO` and/or pushes `USERINFO`/`OPERLOG` with `PIN`+`NAME`.
- Use CRM **CSV employee import** as fallback for names when USERINFO is unsupported or incomplete.

## How to re-test when the device is online

1. Confirm `online: true` on `GET /api/devices`.
2. `POST /api/devices/{serial}/query-users` with `{}` (bulk) and/or `{"pins":["1"]}`.
3. Wait for getrequest delivery + `POST /iclock/cdata?table=USERINFO` (or OPERLOG).
4. Confirm `GET /api/employees` shows non-empty `name` and logs show `userinfo`/`operlog`.
5. Only then consider setting `AUTO_SYNC_USERS=true` on ADMS.
