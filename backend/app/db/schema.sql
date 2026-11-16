PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'admin',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS devices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  serial_number TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL DEFAULT '',
  model TEXT DEFAULT '',
  firmware TEXT DEFAULT '',
  branch TEXT DEFAULT '',
  timezone TEXT DEFAULT 'Asia/Kolkata',
  status TEXT NOT NULL DEFAULT 'pending', -- pending|approved|disabled
  protocol TEXT DEFAULT 'adms-http',
  last_ip TEXT DEFAULT '',
  last_port INTEGER,
  last_heartbeat_at TEXT,
  last_attendance_at TEXT,
  last_userinfo_at TEXT,
  userinfo_supported TEXT DEFAULT 'unknown', -- unknown|yes|no
  total_records INTEGER NOT NULL DEFAULT 0,
  firebase_sync_status TEXT DEFAULT 'unknown',
  stamp_attlog TEXT DEFAULT '0',
  stamp_operlog TEXT DEFAULT '0',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS attendance (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL UNIQUE,
  employee_code TEXT NOT NULL,
  device_serial TEXT NOT NULL,
  device_name TEXT DEFAULT '',
  branch TEXT DEFAULT '',
  punch_time TEXT NOT NULL,
  punch_date TEXT NOT NULL,
  punch_status TEXT DEFAULT '0',
  verify_method TEXT DEFAULT '1',
  work_code TEXT DEFAULT '',
  received_at TEXT NOT NULL DEFAULT (datetime('now')),
  firebase_status TEXT NOT NULL DEFAULT 'pending', -- pending|synced|failed
  firebase_attempts INTEGER NOT NULL DEFAULT 0,
  firebase_last_error TEXT DEFAULT '',
  firebase_synced_at TEXT,
  raw_line TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_attendance_pending ON attendance(firebase_status, id);
CREATE INDEX IF NOT EXISTS idx_attendance_date ON attendance(punch_date);
CREATE INDEX IF NOT EXISTS idx_attendance_employee ON attendance(employee_code);
CREATE INDEX IF NOT EXISTS idx_attendance_device ON attendance(device_serial);

CREATE TABLE IF NOT EXISTS employees (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  employee_code TEXT NOT NULL,
  device_serial TEXT NOT NULL DEFAULT '',
  company_id TEXT NOT NULL DEFAULT 'default',
  name TEXT DEFAULT '',
  card TEXT DEFAULT '',
  branch TEXT DEFAULT '',
  firebase_id TEXT DEFAULT '',
  firebase_status TEXT NOT NULL DEFAULT 'pending', -- pending|synced|failed|skipped
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (company_id, device_serial, employee_code)
);

CREATE INDEX IF NOT EXISTS idx_employees_code ON employees(employee_code);
CREATE INDEX IF NOT EXISTS idx_employees_device ON employees(device_serial);
CREATE INDEX IF NOT EXISTS idx_employees_firebase ON employees(firebase_status);

CREATE TABLE IF NOT EXISTS device_commands (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device_serial TEXT NOT NULL,
  command_code TEXT NOT NULL,
  payload TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending', -- pending|sent|acked|completed|failed|timeout
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  sent_at TEXT,
  completed_at TEXT,
  result TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS comm_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  device_serial TEXT DEFAULT '',
  remote_ip TEXT DEFAULT '',
  listen_port INTEGER,
  method TEXT DEFAULT '',
  endpoint TEXT DEFAULT '',
  category TEXT DEFAULT '',
  status_code INTEGER,
  duration_ms INTEGER,
  error TEXT DEFAULT '',
  detail TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_comm_logs_created ON comm_logs(created_at DESC);

CREATE TABLE IF NOT EXISTS audit_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  actor TEXT DEFAULT '',
  action TEXT NOT NULL,
  detail TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sync_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
