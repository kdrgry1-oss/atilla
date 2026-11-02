#!/usr/bin/env bash
# Günlük yedek: mongodump → doğrula → şifrele → AYRI ve GİZLİ R2 yedek bucket'ına yükle →
# buluttaki kopyayı doğrula → ancak o zaman eski yerel kopyaları sil.
#
# GÜVENLİK (denetim #7, #22):
#  * Uygulamanın KENDİ bağlantısı yedeklenir (MONGO_URL + DB_NAME, backend/routes/deps.py ile aynı
#    varsayılanlar) — yanlış veritabanının yedeği alınıp "tamam" denmez.
#  * Yedek, herkese açık medya bucket'ına (R2_BUCKET) ASLA yüklenmez: ayrı R2_BACKUP_BUCKET
#    zorunludur ve R2_BUCKET ile aynı olamaz. Bucket'a public erişim VERİLMEMELİDİR.
#  * Buluta yalnız ŞİFRELİ kopya gider (BACKUP_PASSPHRASE, AES-256 + PBKDF2).
#  * Döküm / doğrulama / yükleme hatası YUTULMAZ: betik sıfır olmayan kodla çıkar (cron log + syslog).
#  * Yerel kopyalar YALNIZ bulut kopyası boyut + SHA-256 ile doğrulandıktan sonra budanır.
set -euo pipefail
APP=/opt/facette
DIR=/var/backups/facette
ENVF="$APP/backend/.env"
KEEP_LOCAL=2
mkdir -p "$DIR"
chmod 700 "$DIR"
umask 077

fail() {
  echo "!! YEDEK BAŞARISIZ: $*" >&2
  logger -t facette-backup "YEDEK BAŞARISIZ: $*" 2>/dev/null || true
  exit 1
}

envval() {  # .env'den tek değer (tırnaklar atılır); yoksa boş
  grep -E "^$1=" "$ENVF" 2>/dev/null | tail -n1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//" || true
}

MONGO_URL=$(envval MONGO_URL); MONGO_URL=${MONGO_URL:-mongodb://127.0.0.1:27017}
DB=$(envval DB_NAME); DB=${DB:-test_database}
STAMP=$(date +%F-%H%M)
FILE="$DIR/${DB}-$STAMP.archive.gz"

# 1) Döküm — uygulamanın bağlandığı URI + veritabanı.
mongodump --quiet --uri="$MONGO_URL" --db="$DB" --archive="$FILE" --gzip || fail "mongodump hatası"
[ -s "$FILE" ] || fail "döküm dosyası boş"
gzip -t "$FILE" || fail "döküm arşivi bozuk (gzip)"
# Geri yüklenebilirlik: arşiv mongorestore tarafından okunabiliyor mu (hiçbir şey yazmaz).
mongorestore --quiet --dryRun --gzip --archive="$FILE" --nsInclude="${DB}.*" >/dev/null 2>&1 \
  || fail "döküm arşivi mongorestore ile okunamadı"
echo "yedek: $FILE ($(du -h "$FILE" | cut -f1))"

# 2) Bulut (sunucu dışı) kopya — zorunlu ayarlar yoksa hata ver (sessizce "tamam" deme).
R2_BACKUP_BUCKET=$(envval R2_BACKUP_BUCKET)
R2_BUCKET=$(envval R2_BUCKET)
PASS=$(envval BACKUP_PASSPHRASE)
[ -n "$R2_BACKUP_BUCKET" ] || fail "R2_BACKUP_BUCKET tanımlı değil — sunucu dışı yedek YOK (yerel kopya: $FILE)"
[ "$R2_BACKUP_BUCKET" != "$R2_BUCKET" ] || fail "R2_BACKUP_BUCKET, herkese açık medya bucket'ı (R2_BUCKET) ile aynı olamaz"
[ ${#PASS} -ge 16 ] || fail "BACKUP_PASSPHRASE en az 16 karakter olmalı — şifresiz yedek buluta yüklenmez"

ENC="$FILE.enc"
BACKUP_PASSPHRASE="$PASS" openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt \
  -in "$FILE" -out "$ENC" -pass env:BACKUP_PASSPHRASE || fail "şifreleme hatası"
SHA=$(sha256sum "$ENC" | cut -d' ' -f1)

"$APP/venv/bin/python" - "$ENC" "$SHA" "$R2_BACKUP_BUCKET" <<'PY' || fail "R2 yükleme/doğrulama hatası (yerel kopya duruyor: $ENC)"
import os, sys, pathlib
env = {}
for line in pathlib.Path("/opt/facette/backend/.env").read_text().splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1); env[k.strip()] = v.strip().strip('"').strip("'")
import boto3
# Yedek bucket'ı için AYRI (yalnız o bucket'a yazabilen) anahtar varsa o kullanılır.
key_id = env.get("R2_BACKUP_ACCESS_KEY_ID") or env["R2_ACCESS_KEY_ID"]
secret = env.get("R2_BACKUP_SECRET_ACCESS_KEY") or env["R2_SECRET_ACCESS_KEY"]
s3 = boto3.client("s3",
    endpoint_url=f"https://{env['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
    aws_access_key_id=key_id, aws_secret_access_key=secret, region_name="auto")
p = pathlib.Path(sys.argv[1]); sha = sys.argv[2]; bucket = sys.argv[3]
key = f"backups/{p.name}"
s3.upload_file(str(p), bucket, key, ExtraArgs={"Metadata": {"sha256": sha}})
head = s3.head_object(Bucket=bucket, Key=key)
if int(head.get("ContentLength") or -1) != p.stat().st_size:
    raise SystemExit("boyut uyuşmuyor")
if (head.get("Metadata") or {}).get("sha256") != sha:
    raise SystemExit("sha256 uyuşmuyor")
print(f"R2'ye yüklendi ve doğrulandı: {bucket}/{key}")
PY

# 3) Yalnız doğrulanmış bulut kopyasından SONRA yerelde son KEEP_LOCAL yedek bırakılır.
rm -f "$ENC"
{ ls -1t "$DIR"/*.gz 2>/dev/null || true; } | tail -n +$((KEEP_LOCAL + 1)) | xargs -r rm -f
logger -t facette-backup "yedek tamam: $(basename "$FILE") sha256=$SHA" 2>/dev/null || true
