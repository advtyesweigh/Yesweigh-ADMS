# ADMS heartbeat watchdog & session release

## Problem

ZK Push devices poll `/iclock/getrequest` (and handshake via `/iclock/cdata`). If the poll loop stalls, some firmwares leave a **half-open TCP** to ADMS `:8081` and never reconnect. ADMS also used to treat a **pre-restart** `last_heartbeat_at` as Online for up to ~180s after `systemctl restart adms`.

## Thresholds (Delay=15 handshake)

| Status | Age since last valid device request |
|--------|-------------------------------------|
| **Online** | ≤ **45s** (~3× `Delay=15`) |
| **Unstable** | **45–120s** |
| **Offline** | **> 120s** (or pre-boot stamp) |

Env: `DEVICE_ONLINE_SECONDS=45`, `DEVICE_OFFLINE_SECONDS=120`.

## Server-side fix (deployed in ADMS)

1. **Boot**: process start time is recorded; Online requires a heartbeat **after** this boot.
2. **Watchdog** (default every 30s): if no heartbeat within `DEVICE_OFFLINE_SECONDS` (**120**), mark the session **released** and **shut down** idle TCP sockets to that device’s last IP.
3. **HTTP**: `/iclock/*` responses send `Connection: close`; uvicorn `timeout_keep_alive` is short (default 5s).
4. **Reconnect**: the next real `getrequest` / `cdata` calls `touch_heartbeat` → session clears released flag → CRM shows Online. Deduped `QUERY_ATTLOG` recovery may be queued.
5. **Handshake**: `Delay=15`, `ErrorDelay=30`, `TransInterval=1`, `Realtime=1` (device-initiated retry cadence).

Manual release:

```bash
curl -u admin:**** -X POST http://127.0.0.1:8081/api/devices/<SN>/release
```

## Device firmware

ADMS cannot dial out to the biometric unit. After TCP release, most units retry within `ErrorDelay` (handshake sets **30s**). If the unit stays silent for several minutes after release/restart, **reboot the device** (or toggle Cloud/ADMS on the panel) so it opens a fresh Push session to `http://103.176.85.102:8081`.

## Verify Online again

1. `curl -s http://127.0.0.1:8081/health` — note `service_started_at` and `handshake` / `thresholds`.
2. Watch journal: `journalctl -u adms -f | grep -E 'iclock|released|reconnected'`.
3. Expect `GET /iclock/getrequest?SN=…` within ~1–2 minutes (or after device reboot / Cloud toggle).
4. `GET /api/devices` → `online: true` and CRM badge **Online**.
5. Confirm no long-lived idle `ESTAB` from the device IP: `ss -tnp | grep 8081`.
