# X305 Pro/ID disconnect fix (Nov 2026)

## Device
- ZKTeco X305 Pro/ID, Firmware 6.6.1 Build 41 (2018)
- SN `0IN7010596123100370`
- Cloud URL: **http://103.176.85.102:8081** (port **8081 only**) — unchanged

## Root cause notes (multi-hour drop — deeper dig)

### Evidence (comm_logs + journal)
- Push is **device-initiated** short HTTP (`Connection: close`). ADMS never dials the biometric.
- **Recurring sessions** (peer `111.92.117.206`):
  - 2026-11-14: `04:13` → `13:38` UTC (~9.4h)
  - 2026-11-16: `04:20` → `13:31` UTC (~9.2h)
  - 2026-11-17: `04:02` → `09:16` UTC (~5.2h) then silence
- Morning reconnect often starts with handshake + OPERLOG (USER/FP template flood) + ATTLOG, then steady `getrequest`.
- **Nov 17 cut**: last real `getrequest` `09:16:37` → watchdog `heartbeat_timeout` `09:20:03` (`age_s=206`, `tcp_closed=0`). Immediately prior: healthy ~30s polls, `options=all` handshake `09:16:13`, all HTTP **200**, **zero** 4xx/5xx on iclock path.
- **Not** stamp corruption (ATTLOGStamp/OpStamp preserved and echoed).
- **Not** command backlog at drop (empty `OK` polls; stuck `sent` QUERY_USERINFO from Nov 16 are not re-delivered — only `pending` is pushed).
- **Not** ErrorDelay misfire while healthy (intervals matched Delay).
- **Not** Firebase/SQLite on request path (Firebase async; Postgres before ATTLOG ACK).
- Pattern fits **device/firmware or site network stop initiating** after multi-hour Push run (often late afternoon IST / ~13:30 UTC on longer days). Server-side release cannot wake a silent client; recovery is device ErrorDelay retry, panel Cloud toggle, or power cycle.

### Mitigations shipped
1. Handshake: **`Delay=15`**, **`ErrorDelay=30`**, `TransInterval=1`, `Realtime=1`, classic `TransFlag=1111000000`, CRLF, Stamp=ATTLOGStamp (preserved)
2. Status: **Online ≤45s**, **Unstable 45–120s**, **Offline >120s**
3. Watchdog + reconnect TCP nudge; `Connection: close`; systemd `Restart=always` `RestartSec=5`
4. Expire stale unacked `sent` commands on boot; deduped ATTLOG recovery after reconnect
5. Poll interval + HTTP error metrics on `/api/devices/{sn}/connection-history` (`poll_metrics`)

## Verified now (2026-11-17 ~10:07Z redeploy)
- Smoke: `/iclock/cdata`, `/iclock/getrequest`, `/iclock/ping`, `.aspx` aliases → HTTP 200 plain text; empty poll → `OK`
- Handshake body: **Delay=15, ErrorDelay=30, TransInterval=1, Realtime=1**, TransFlag=1111000000, CRLF; stamps preserved (`ATTLOGStamp=863862771`)
- Thresholds: Online ≤45s / Unstable 45–120s / Offline >120s (`/health` + device fields)
- Localhost/VPS smoke no longer updates `last_heartbeat_at` or clears session release
- systemd `Restart=always` `RestartSec=5`; listen **8081 only**; Caddy/Mailcow/CRM untouched
- SN `0IN7010596123100370`: **Offline** — last real peer poll `111.92.117.206` at **09:16:37Z**; no new peer traffic in 90s monitor after redeploy
- Cloud URL unchanged: http://103.176.85.102:8081
- 48h soak **started** at redeploy; use monitoring commands below

## 48h soak — how to monitor

```bash
# Live device polls (expect ~15s after device accepts new Delay)
journalctl -u adms -f | grep -E '111.92.117.206|/iclock/'

# Health + handshake/thresholds
curl -s http://127.0.0.1:8081/health | jq '.handshake,.thresholds,.'

# Device Online + last_seen + poll intervals / HTTP errors
curl -s -u admin:"$ADMIN_PASSWORD" \
  http://127.0.0.1:8081/api/devices/0IN7010596123100370/connection-history | jq .

# Gap hunt (UTC hours with peer traffic)
sqlite3 /opt/adms/data/adms.db \
  "SELECT substr(created_at,1,13), count(*) FROM comm_logs
   WHERE device_serial='0IN7010596123100370' AND remote_ip='111.92.117.206'
     AND method!='WATCHDOG' GROUP BY 1 ORDER BY 1 DESC LIMIT 48;"
```

### Pass criteria
- [ ] Continuous getrequest from SN without multi-hour silent gaps (or documented site power/network cause)
- [ ] After network blip: reconnect within ~ErrorDelay (30s) + recovery ATTLOG (no duplicate event_id)
- [ ] `systemctl restart adms` → Offline until first real peer poll → Online
- [ ] Overnight recovery without deleting attendance / device reset
- [ ] `poll_metrics.interval_seconds.median` near Delay (15) once device accepts new options

### Device-side recovery tip (no firmware push / no machine reset unless soak fails)
If silent after server release: toggle Cloud/ADMS on the panel (preferred) or reboot biometric so it opens a fresh Push session to `http://103.176.85.102:8081`.
