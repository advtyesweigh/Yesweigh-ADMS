"""ADMS configuration."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "ADMS"
    app_env: str = "production"
    secret_key: str = Field(default="CHANGE_ME_GENERATE_A_LONG_RANDOM_SECRET")
    admin_username: str = "admin"
    admin_password_hash: str = ""
    admin_password: str = "ChangeMeNow!"

    data_dir: Path = Path("/opt/adms/data")
    db_path: Path = Path("/opt/adms/data/adms.db")
    backup_dir: Path = Path("/opt/adms/data/backups")
    firebase_credentials_path: Path = Path("/opt/adms/secrets/firebase-service-account.json")

    # Single Push listener (VPS uses PORT=8081). Keep port_primary as alias.
    host: str = "0.0.0.0"
    port: int = 8081
    port_primary: int = 8081
    port_secondary: int = 8083
    port_legacy_http: int = 4370
    enable_primary: bool = True
    enable_secondary: bool = True
    enable_legacy_http: bool = False
    enable_legacy_tcp_pull: bool = False
    legacy_tcp_bind: str = "127.0.0.1"
    legacy_tcp_port: int = 4370

    public_base_url: str = "http://103.176.85.102:8081"
    admin_bind: str = "127.0.0.1"
    admin_port: int = 8091

    timezone_default: str = "Asia/Kolkata"
    device_offline_seconds: int = 180
    heartbeat_interval_hint: int = 30
    retention_days_synced: int = 30
    sync_batch_size: int = 50
    sync_interval_seconds: int = 20
    sync_retry_base_seconds: int = 5
    sync_retry_max_seconds: int = 300
    request_body_max_bytes: int = 1_048_576
    rate_limit_per_minute: int = 600

    # New devices: pending until approved unless true
    auto_approve_devices: bool = False
    # Queue QUERY_USERINFO / treat enroll pushes as auto-import.
    # Keep false until X305 Pro (or target firmware) USERINFO is verified — see docs/X305_PRO_USERINFO.md
    auto_sync_users: bool = False

    firebase_enabled: bool = False
    firebase_company_id: str = "default"
    firebase_devices_path: str = "companies/{companyId}/devices/{deviceId}"
    firebase_attendance_path: str = "companies/{companyId}/attendance_logs/{recordId}"
    firebase_employees_path: str = "companies/{companyId}/employees/{employeeId}"

    ip_allowlist: str = ""
    cors_origins: str = "*"

    @property
    def allowlist_ips(self) -> List[str]:
        if not self.ip_allowlist.strip():
            return []
        return [x.strip() for x in self.ip_allowlist.split(",") if x.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
