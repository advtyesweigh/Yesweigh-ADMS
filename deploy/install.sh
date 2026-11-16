#!/bin/bash
set -euo pipefail
APP=/opt/adms
echo "==> Installing ADMS to $APP"
mkdir -p "$APP/data" "$APP/secrets" "$APP/backend"
python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install --upgrade pip
"$APP/venv/bin/pip" install -r "$APP/backend/requirements.txt"
if [[ ! -f "$APP/.env" ]]; then
  SECRET=$(openssl rand -hex 32)
  cat > "$APP/.env" <<EOF
SECRET_KEY=$SECRET
ADMIN_USERNAME=admin
ADMIN_PASSWORD=ChangeMeNow!
HOST=0.0.0.0
PORT=8081
PUBLIC_BASE_URL=http://103.176.85.102:8081
DATA_DIR=/opt/adms/data
DB_PATH=/opt/adms/data/adms.db
FIREBASE_ENABLED=false
FIREBASE_CREDENTIALS_PATH=/opt/adms/secrets/firebase-service-account.json
TIMEZONE_DEFAULT=Asia/Kolkata
AUTO_APPROVE_DEVICES=true
EOF
  chmod 600 "$APP/.env"
fi
cp "$APP/deploy/adms.service" /etc/systemd/system/adms.service
systemctl daemon-reload
systemctl enable adms.service
systemctl restart adms.service
ufw allow 8081/tcp comment 'adms-push' || true
systemctl --no-pager --full status adms.service | head -25
ss -tlnp | grep ':8081' || true
echo "==> ADMS installed. Dashboard: http://103.176.85.102:8081/"
