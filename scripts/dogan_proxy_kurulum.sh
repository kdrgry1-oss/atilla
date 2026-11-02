#!/usr/bin/env bash
# Doğan e-Dönüşüm için SABİT-IP ARACI SUNUCU kurulumu (Ubuntu 22.04 / 24.04).
#
# Neden: Railway çıkış IP'leri paylaşımlı ve zaman zaman değişiyor; Doğan IP izni istiyor.
# Bu betik, sabit IP'li kendi sunucunuza YALNIZ Doğan'a açılan, şifreli küçük bir aracı (Squid)
# kurar. Atilla backend'i yalnız Doğan isteklerini buradan geçirir (DOGAN_PROXY_URL).
#
# - Yalnız connector.doganedonusum.com / efaturatest.doganedonusum.com :443'e tünel açar,
#   başka HİÇBİR adrese izin vermez; şifresiz istek reddedilir. Önbellek yok.
# - Fatura verisi uçtan uca TLS ile şifreli kalır (aracı içeriği göremez).
# - Sunucudaki diğer projelere dokunmaz: ayrı port (varsayılan 8443), ayrı ayar.
# - Squid bu sunucuda ZATEN kuruluysa mevcut ayarı ezmemek için DURUR.
#
# Kullanım (sunucuda root ile):   sudo bash dogan_proxy_kurulum.sh
# Farklı port:                     sudo PORT=9443 bash dogan_proxy_kurulum.sh
# Kaldırma:                        sudo apt-get purge -y squid && sudo rm -rf /etc/squid
set -euo pipefail

PORT="${PORT:-8443}"
USER_NAME="atilla"

if [ "$(id -u)" -ne 0 ]; then
  echo "HATA: root ile çalıştırın:  sudo bash $0"; exit 1
fi
if dpkg -s squid >/dev/null 2>&1 && [ ! -f /etc/squid/.atilla_dogan ]; then
  echo "HATA: Bu sunucuda Squid zaten kurulu ve başka bir amaçla kullanılıyor olabilir."
  echo "Mevcut ayarı ezmemek için durduruldu. Lütfen Atilla destek ile görüşün."; exit 1
fi
if ss -ltn "( sport = :$PORT )" | grep -q ":$PORT"; then
  if ! [ -f /etc/squid/.atilla_dogan ]; then
    echo "HATA: $PORT portu başka bir program tarafından kullanılıyor. Farklı port deneyin:"
    echo "  sudo PORT=9443 bash $0"; exit 1
  fi
fi

echo "==> Squid kuruluyor..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -q >/dev/null
apt-get install -y -q squid openssl curl >/dev/null

PASS="$(openssl rand -hex 24)"
printf '%s:%s\n' "$USER_NAME" "$(openssl passwd -apr1 "$PASS")" > /etc/squid/atilla_passwd
chown proxy:proxy /etc/squid/atilla_passwd
chmod 640 /etc/squid/atilla_passwd

cat > /etc/squid/squid.conf <<EOF
# Atilla → Doğan e-Dönüşüm sabit-IP aracı. Yalnız Doğan, yalnız şifreyle, yalnız 443.
http_port $PORT
auth_param basic program /usr/lib/squid/basic_ncsa_auth /etc/squid/atilla_passwd
auth_param basic realm atilla-dogan
auth_param basic children 3
acl authed proxy_auth REQUIRED
acl dogan dstdomain connector.doganedonusum.com efaturatest.doganedonusum.com
acl SSL_ports port 443
acl CONNECT method CONNECT
http_access deny !authed
http_access deny !CONNECT
http_access deny !SSL_ports
http_access allow dogan
http_access deny all
cache deny all
forwarded_for delete
httpd_suppress_version_string on
access_log daemon:/var/log/squid/access.log
EOF
touch /etc/squid/.atilla_dogan

squid -k parse >/dev/null 2>&1 || { echo "HATA: Squid ayarı geçersiz"; squid -k parse; exit 1; }
systemctl enable squid >/dev/null 2>&1 || true
systemctl restart squid

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow "$PORT"/tcp >/dev/null
  echo "==> Güvenlik duvarında (ufw) $PORT/tcp açıldı."
fi

IP="$(curl -4 -s --max-time 10 https://api.ipify.org || true)"
IP="${IP:-SUNUCU_IP}"

echo
echo "================= KURULUM TAMAM ================="
echo "1) Doğan'a verilecek TEK IP:   $IP"
echo
echo "2) Railway → backend servisi → Variables → New Variable:"
echo "   Ad:    DOGAN_PROXY_URL"
echo "   Değer: http://$USER_NAME:$PASS@$IP:$PORT"
echo
echo "   (Bu değeri kimseyle paylaşmayın; yalnız Railway'e girin.)"
echo
echo "3) Barındırma firmanızın panelinde ayrıca bir güvenlik duvarı varsa"
echo "   $PORT/tcp portuna dışarıdan izin verin."
echo "================================================="
