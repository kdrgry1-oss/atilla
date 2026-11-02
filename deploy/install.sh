#!/usr/bin/env bash
# Atilla tek sunucu kurulumu (Ubuntu 22.04/24.04, 2 GB RAM / 5 GB disk için ayarlı).
# Kullanım:  bash install.sh <magaza-alan-adi> <api-alan-adi>
set -euo pipefail

SITE="${1:?kullanim: bash install.sh yenimarka.com api.yenimarka.com}"
API="${2:?api alan adi gerekli}"
APP=/opt/atilla
REPO="${REPO_URL:-}"          # boşsa kodu kendiniz kopyalarsınız (scp/rsync)

echo "▶ 1/8 Sistem güncelleniyor"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl gnupg ca-certificates git python3-venv python3-pip ufw >/dev/null

echo "▶ 2/8 Swap (2 GB) — görselli export ve rapor tepe anları için"
if ! swapon --show | grep -q swapfile; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap -q /swapfile && swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  sysctl -qw vm.swappiness=10
fi

echo "▶ 3/8 MongoDB 7 kuruluyor (önbellek 512 MB'a sabitlenir)"
if ! command -v mongod >/dev/null; then
  curl -fsSL https://pgp.mongodb.com/server-7.0.asc | gpg -o /usr/share/keyrings/mongodb.gpg --dearmor
  . /etc/os-release
  echo "deb [signed-by=/usr/share/keyrings/mongodb.gpg] https://repo.mongodb.org/apt/ubuntu ${UBUNTU_CODENAME:-jammy}/mongodb-org/7.0 multiverse" \
    > /etc/apt/sources.list.d/mongodb-org-7.0.list
  apt-get update -qq && apt-get install -y -qq mongodb-org >/dev/null
fi
python3 - <<'PY'
import re, pathlib
p = pathlib.Path("/etc/mongod.conf"); c = p.read_text()
if "cacheSizeGB" not in c:
    c = c.replace("storage:", "storage:\n  wiredTiger:\n    engineConfig:\n      cacheSizeGB: 0.5", 1)
    p.write_text(c)
PY
systemctl enable --now mongod

echo "▶ 4/8 Uygulama dizini ve Python ortamı"
mkdir -p "$APP"
if [ -n "$REPO" ] && [ ! -d "$APP/.git" ]; then git clone --depth 1 "$REPO" "$APP"; fi
[ -d "$APP/backend" ] || { echo "!! $APP/backend yok — kodu kopyalayın (scp -r backend root@IP:$APP/)"; exit 1; }
python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip wheel
"$APP/venv/bin/pip" install -q -r "$APP/backend/requirements.txt"
[ -f "$APP/backend/.env" ] || cp "$APP/deploy/env.example" "$APP/backend/.env" 2>/dev/null || true

echo "▶ 5/8 systemd servisi (tek işçi — bu bellekte doğrusu bu)"
cat > /etc/systemd/system/atilla-api.service <<UNIT
[Unit]
Description=Atilla API (FastAPI/uvicorn)
After=network.target mongod.service
Requires=mongod.service

[Service]
Type=simple
WorkingDirectory=$APP/backend
EnvironmentFile=$APP/backend/.env
ExecStart=$APP/venv/bin/uvicorn server:app --host 127.0.0.1 --port 8000 --workers 1 --timeout-keep-alive 65
Restart=always
RestartSec=5
MemoryMax=1200M
StandardOutput=append:/var/log/atilla-api.log
StandardError=append:/var/log/atilla-api.log

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload && systemctl enable --now atilla-api

echo "▶ 6/8 Caddy (otomatik SSL) — panel/mağaza + API"
if ! command -v caddy >/dev/null; then
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor -o /usr/share/keyrings/caddy.gpg
  echo "deb [signed-by=/usr/share/keyrings/caddy.gpg] https://dl.cloudsmith.io/public/caddy/stable/deb/debian any-version main" \
    > /etc/apt/sources.list.d/caddy.list
  apt-get update -qq && apt-get install -y -qq caddy >/dev/null
fi
mkdir -p /var/www/$SITE
cat > /etc/caddy/Caddyfile <<CADDY
$SITE, www.$SITE {
    root * /var/www/$SITE
    try_files {path} /index.html          # React yönlendirmeleri
    file_server
    encode gzip zstd
    header /static/* Cache-Control "public, max-age=31536000, immutable"
}
$API {
    reverse_proxy 127.0.0.1:8000
    request_body {
        max_size 25MB
    }
    encode gzip
}
CADDY
systemctl reload caddy || systemctl restart caddy

echo "▶ 7/8 Güvenlik duvarı + günlük yedek (03:00, 7 gün)"
ufw allow OpenSSH >/dev/null; ufw allow 80 >/dev/null; ufw allow 443 >/dev/null; ufw --force enable >/dev/null
install -m 0755 "$APP/deploy/backup.sh" /usr/local/bin/atilla-backup
printf '#!/bin/sh\n/usr/local/bin/atilla-backup >> /var/log/atilla-backup.log 2>&1\n' \
  > /etc/cron.daily/atilla-backup
chmod +x /etc/cron.daily/atilla-backup
# Haftalık geri yükleme testi (denetim #22): yedek gerçekten geri yüklenebiliyor mu (geçici DB'ye).
install -m 0755 "$APP/deploy/restore_test.sh" /usr/local/bin/atilla-restore-test
printf '#!/bin/sh\n/usr/local/bin/atilla-restore-test >> /var/log/atilla-restore-test.log 2>&1\n' \
  > /etc/cron.weekly/atilla-restore-test
chmod +x /etc/cron.weekly/atilla-restore-test

echo "▶ 8/8 Durum"
sleep 3
systemctl --no-pager --lines=0 status atilla-api mongod caddy | grep -E "●|Active:" || true
echo
echo "✔ Kurulum bitti."
echo "  1) $APP/backend/.env dosyasını doldurun → systemctl restart atilla-api"
echo "  2) Frontend derlemesini /var/www/$SITE altına kopyalayın"
echo "  3) Sağlık: curl -s https://$API/api/health"
