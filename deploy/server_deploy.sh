#!/usr/bin/env bash
# Otomatik sunucu kurulumu/güncellemesi — GitHub Actions (deploy-server.yml) tarafından root
# olarak çalıştırılır. Tekrar çalıştırılabilir (idempotent): ilk seferde MongoDB, Python, Caddy,
# servis ve güvenlik duvarını kurar; sonraki seferlerde yalnız kodu/vitrini günceller.
#
# Kullanım: bash server_deploy.sh <release-dizini> <site> <api>
#   <site>/<api> alan adı ise HTTPS (Caddy otomatik sertifika),
#   ikisi de aynı IP ise IP modu: http://IP  (API aynı adreste /api altında).
#
# Sırlar ASLA log'a yazılmaz (public repo'da Actions log'u herkese açıktır).
set -euo pipefail

REL="${1:?release dizini}"
SITE="${2:?site}"
API="${3:?api}"
APP=/opt/atilla
WEB=/var/www/atilla
SVC=atilla-api
ENVF="$APP/backend/.env"

is_ip() { [[ "$1" =~ ^[0-9]+(\.[0-9]+){3}$ ]]; }
if is_ip "$SITE"; then
  MODE=ip; SITE_URL="http://$SITE"; API_URL="http://$SITE"
else
  MODE=domain; SITE_URL="https://$SITE"; API_URL="https://$API"
fi
echo "▶ Mod: $MODE  site=$SITE_URL  api=$API_URL"

export DEBIAN_FRONTEND=noninteractive

echo "▶ 1/9 Sistem paketleri"
# CD/DVD'den kurulmuş Ubuntu'da kalan "deb cdrom:" kaynağı apt-get update'i durdurur → devre dışı.
for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list; do
  [ -f "$f" ] && sed -i -E 's/^(deb(-src)? +\[?[^]]*\]? *cdrom:)/# \1/; s/^(deb(-src)? +cdrom:)/# \1/' "$f" || true
done
# İnternet paket kaynağı hiç yoksa (yalnız CD ile kurulmuş) resmi Ubuntu kaynaklarını ekle.
if ! grep -rqsE '^deb +(\[[^]]*\] +)?https?://' /etc/apt/sources.list /etc/apt/sources.list.d/ \
   && ! grep -rqsE '^URIs: *https?://' /etc/apt/sources.list.d/ ; then
  . /etc/os-release
  CN="${UBUNTU_CODENAME:-jammy}"
  cat > /etc/apt/sources.list.d/atilla-ubuntu.list <<SRC
deb http://archive.ubuntu.com/ubuntu $CN main restricted universe multiverse
deb http://archive.ubuntu.com/ubuntu $CN-updates main restricted universe multiverse
deb http://security.ubuntu.com/ubuntu $CN-security main restricted universe multiverse
SRC
fi
if [ ! -f /var/lib/atilla-bootstrap-done ]; then
  apt-get update -qq
  apt-get install -y -qq curl gnupg ca-certificates git rsync ufw openssl >/dev/null
fi

echo "▶ 2/9 Swap (2 GB)"
if ! swapon --show | grep -q swapfile; then
  if [ "$(free -m | awk '/^Mem:/{print $2}')" -lt 4000 ]; then
    fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap -q /swapfile && swapon /swapfile
    grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    sysctl -qw vm.swappiness=10
  fi
fi

echo "▶ 3/9 MongoDB"
if ! command -v mongod >/dev/null; then
  . /etc/os-release
  CODENAME="${UBUNTU_CODENAME:-jammy}"
  MVER=7.0; [ "$CODENAME" = "noble" ] && MVER=8.0
  curl -fsSL "https://pgp.mongodb.com/server-${MVER}.asc" | gpg --batch --yes -o /usr/share/keyrings/mongodb.gpg --dearmor
  echo "deb [signed-by=/usr/share/keyrings/mongodb.gpg] https://repo.mongodb.org/apt/ubuntu ${CODENAME}/mongodb-org/${MVER} multiverse" \
    > /etc/apt/sources.list.d/mongodb-org.list
  apt-get update -qq && apt-get install -y -qq mongodb-org >/dev/null
  if ! grep -q cacheSizeGB /etc/mongod.conf; then
    sed -i 's/^storage:/storage:\n  wiredTiger:\n    engineConfig:\n      cacheSizeGB: 0.5/' /etc/mongod.conf
  fi
fi
systemctl enable --now mongod >/dev/null 2>&1 || true
for i in $(seq 1 20); do mongosh --quiet --eval 'db.runCommand({ping:1}).ok' >/dev/null 2>&1 && break; sleep 2; done

echo "▶ 4/9 Caddy"
if ! command -v caddy >/dev/null; then
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy.gpg
  echo "deb [signed-by=/usr/share/keyrings/caddy.gpg] https://dl.cloudsmith.io/public/caddy/stable/deb/debian any-version main" \
    > /etc/apt/sources.list.d/caddy.list
  apt-get update -qq && apt-get install -y -qq caddy >/dev/null
fi

echo "▶ 5/9 Kod"
mkdir -p "$APP" "$WEB"
rsync -a --delete --exclude '.env' --exclude '__pycache__' --exclude 'uploads/' "$REL/backend/" "$APP/backend/"
rsync -a --delete "$REL/deploy/" "$APP/deploy/"
rsync -a --delete "$REL/frontend_build/" "$WEB/"

echo "▶ 6/9 Python 3.11 ortamı"
if ! command -v uv >/dev/null && [ ! -x /root/.local/bin/uv ]; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh >/dev/null
fi
UV="$(command -v uv || echo /root/.local/bin/uv)"
[ -x "$APP/venv/bin/python" ] || "$UV" venv -q --python 3.11 "$APP/venv"
REQ_HASH="$(sha256sum "$APP/backend/requirements.txt" | cut -d' ' -f1)"
if [ "$(cat "$APP/.req_hash" 2>/dev/null)" != "$REQ_HASH" ]; then
  VIRTUAL_ENV="$APP/venv" "$UV" pip install -q -r "$APP/backend/requirements.txt"
  echo "$REQ_HASH" > "$APP/.req_hash"
fi

echo "▶ 7/9 Ayar dosyası (.env)"
touch "$ENVF"; chmod 600 "$ENVF"
set_kv() {   # anahtarı yaz/değiştir (değer log'a basılmaz)
  local k="$1" v="$2" tmp
  tmp="$(mktemp)"
  grep -v "^${k}=" "$ENVF" > "$tmp" || true
  printf '%s=%s\n' "$k" "$v" >> "$tmp"
  cat "$tmp" > "$ENVF"; rm -f "$tmp"
}
get_kv() { grep -m1 "^$1=" "$ENVF" 2>/dev/null | cut -d= -f2- || true; }
ensure_secret() { [ -n "$(get_kv "$1")" ] || set_kv "$1" "$(openssl rand -hex 32)"; }

[ -n "$(get_kv MONGO_URL)" ] || set_kv MONGO_URL "mongodb://127.0.0.1:27017"
[ -n "$(get_kv DB_NAME)" ]   || set_kv DB_NAME "atilla"
for k in JWT_SECRET SECRETS_MASTER_KEY PII_HASH_SECRET KVKK_TOMBSTONE_SECRET \
         INVOICE_LINK_SECRET ALERT_ACK_SECRET CF_EDGE_SECRET; do
  ensure_secret "$k"
done
# Alan adı/IP'ye bağlı değerler her dağıtımda güncellenir.
if [ "$MODE" = domain ]; then
  set_kv CORS_ORIGINS "https://$SITE,https://www.$SITE"
else
  set_kv CORS_ORIGINS "$SITE_URL"
fi
set_kv SITE_URL "$SITE_URL"
set_kv FRONTEND_URL "$SITE_URL"
set_kv BACKEND_URL "$API_URL"
set_kv PUBLIC_API_URL "$API_URL"
# İlk yönetici (yalnız GitHub Secrets'tan gelirse; dosya okunduktan sonra silinir).
if [ -f "$REL/bootstrap.env" ]; then
  # shellcheck disable=SC1091
  . "$REL/bootstrap.env"
  if [ -n "${ADMIN_EMAIL:-}" ]; then
    set_kv ADMIN_INITIAL_EMAIL "$ADMIN_EMAIL"
    set_kv OWNER_SUPER_ADMIN_EMAILS "$ADMIN_EMAIL"
    [ -n "$(get_kv ALERT_TO_EMAIL)" ] || set_kv ALERT_TO_EMAIL "$ADMIN_EMAIL"
    [ -n "$(get_kv ORDER_NOTIFY_EMAIL)" ] || set_kv ORDER_NOTIFY_EMAIL "$ADMIN_EMAIL"
  fi
  [ -n "${ADMIN_PASSWORD:-}" ] && set_kv ADMIN_INITIAL_PASSWORD "$ADMIN_PASSWORD"
  [ -n "${PRODUCT_XML_URL:-}" ] && set_kv PRODUCT_XML_URL "$PRODUCT_XML_URL"
  shred -u "$REL/bootstrap.env" 2>/dev/null || rm -f "$REL/bootstrap.env"
fi

echo "▶ 8/9 Servis + Caddy + güvenlik duvarı"
cat > /etc/systemd/system/$SVC.service <<UNIT
[Unit]
Description=Atilla API (FastAPI/uvicorn)
After=network.target mongod.service
Requires=mongod.service

[Service]
Type=simple
WorkingDirectory=$APP/backend
EnvironmentFile=$ENVF
ExecStart=$APP/venv/bin/uvicorn server:app --host 127.0.0.1 --port 8000 --workers 1 --timeout-keep-alive 65
Restart=always
RestartSec=5
MemoryMax=1200M
StandardOutput=append:/var/log/$SVC.log
StandardError=append:/var/log/$SVC.log

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable $SVC >/dev/null 2>&1
systemctl restart $SVC

if [ "$MODE" = domain ]; then
cat > /etc/caddy/Caddyfile <<CADDY
$SITE, www.$SITE {
    root * $WEB
    try_files {path} /index.html
    file_server
    encode gzip zstd
    header /static/* Cache-Control "public, max-age=31536000, immutable"
}
$API {
    reverse_proxy 127.0.0.1:8000
    request_body { max_size 25MB }
    encode gzip
}
CADDY
else
cat > /etc/caddy/Caddyfile <<CADDY
:80 {
    encode gzip zstd
    handle /api/* {
        request_body { max_size 25MB }
        reverse_proxy 127.0.0.1:8000
    }
    handle {
        root * $WEB
        try_files {path} /index.html
        file_server
        header /static/* Cache-Control "public, max-age=31536000, immutable"
    }
}
CADDY
fi
systemctl enable caddy >/dev/null 2>&1 || true
systemctl reload caddy 2>/dev/null || systemctl restart caddy

ufw allow OpenSSH >/dev/null; ufw allow 80 >/dev/null; ufw allow 443 >/dev/null; ufw --force enable >/dev/null
install -m 0755 "$APP/deploy/backup.sh" /usr/local/bin/atilla-backup
printf '#!/bin/sh\n/usr/local/bin/atilla-backup >> /var/log/atilla-backup.log 2>&1\n' > /etc/cron.daily/atilla-backup
chmod +x /etc/cron.daily/atilla-backup
touch /var/lib/atilla-bootstrap-done

echo "▶ 9/9 Sağlık kontrolü"
ok=0
for i in $(seq 1 45); do
  if curl -fsS -o /dev/null http://127.0.0.1:8000/api/health; then ok=1; break; fi
  sleep 2
done
systemctl --no-pager --lines=0 status $SVC mongod caddy | grep -E "●|Active:" || true
if [ "$ok" = 1 ]; then
  echo "✔ API ayakta. Site: $SITE_URL"
else
  echo "✘ API sağlık kontrolü geçmedi — son log satırları:"
  tail -n 40 /var/log/$SVC.log | grep -viE 'secret|password|token|key=' || true
  exit 1
fi

echo "▶ Ürün aktarımı (XML)"
if [ -n "$(get_kv PRODUCT_XML_URL)" ]; then
  # Stok senkronu: her 2 saatte bir (stoğu biten ürün satılamasın, yeni stoklu ürün gelsin).
  cat > /etc/cron.d/atilla-xml-import <<CRON
SHELL=/bin/bash
17 */2 * * * root cd $APP/backend && flock -n /run/atilla-xml.lock $APP/venv/bin/python scripts/import_ticimax_xml.py >> /var/log/atilla-xml-import.log 2>&1
CRON
  chmod 644 /etc/cron.d/atilla-xml-import
  cd "$APP/backend" && timeout 1800 flock /run/atilla-xml.lock "$APP/venv/bin/python" scripts/import_ticimax_xml.py \
    2>&1 | grep -viE 'https?://|secret|password|token' | tail -n 5 || echo "!! ürün aktarımı tamamlanamadı (sonraki zamanlanmış çalıştırma tekrar dener)"
else
  rm -f /etc/cron.d/atilla-xml-import
  echo "PRODUCT_XML_URL tanımlı değil — atlandı."
fi
