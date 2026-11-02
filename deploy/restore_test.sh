#!/usr/bin/env bash
# Haftalık GERİ YÜKLEME TESTİ (denetim #22): yedeğin gerçekten geri yüklenebildiği kanıtlanır.
#   1) R2_BACKUP_BUCKET'taki EN SON yedek indirilir, SHA-256'sı (yükleme meta verisi) doğrulanır,
#   2) BACKUP_PASSPHRASE ile şifresi çözülür,
#   3) GEÇİCİ bir veritabanına (restore_test) geri yüklenir — canlı veritabanına DOKUNULMAZ,
#   4) temel koleksiyonlar (products/orders/users/settings) boş değilse BAŞARILI; geçici DB silinir.
# Herhangi bir adım başarısızsa sıfır olmayan kodla çıkar (cron log + syslog).
set -euo pipefail
APP=/opt/atilla
ENVF="$APP/backend/.env"
WORK=$(mktemp -d /var/tmp/atilla-restore.XXXXXX)
umask 077
trap 'rm -rf "$WORK"' EXIT

fail() {
  echo "!! GERİ YÜKLEME TESTİ BAŞARISIZ: $*" >&2
  logger -t atilla-restore-test "BAŞARISIZ: $*" 2>/dev/null || true
  exit 1
}
envval() {
  grep -E "^$1=" "$ENVF" 2>/dev/null | tail -n1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//" || true
}

MONGO_URL=$(envval MONGO_URL); MONGO_URL=${MONGO_URL:-mongodb://127.0.0.1:27017}
DB=$(envval DB_NAME); DB=${DB:-test_database}
PASS=$(envval BACKUP_PASSPHRASE)
BUCKET=$(envval R2_BACKUP_BUCKET)
TMPDB="restore_test"
[ -n "$BUCKET" ] || fail "R2_BACKUP_BUCKET tanımlı değil"
[ -n "$PASS" ] || fail "BACKUP_PASSPHRASE tanımlı değil"
[ "$TMPDB" != "$DB" ] || fail "geçici veritabanı adı canlı veritabanıyla aynı olamaz"

# 1) En son yedeği indir + SHA-256 doğrula
"$APP/venv/bin/python" - "$WORK/latest.enc" "$BUCKET" <<'PY' || fail "en son yedek indirilemedi/doğrulanamadı"
import hashlib, pathlib, sys
env = {}
for line in pathlib.Path("/opt/atilla/backend/.env").read_text().splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1); env[k.strip()] = v.strip().strip('"').strip("'")
import boto3
s3 = boto3.client("s3", endpoint_url=f"https://{env['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
                  aws_access_key_id=env.get("R2_BACKUP_ACCESS_KEY_ID") or env["R2_ACCESS_KEY_ID"],
                  aws_secret_access_key=env.get("R2_BACKUP_SECRET_ACCESS_KEY") or env["R2_SECRET_ACCESS_KEY"],
                  region_name="auto")
out, bucket = sys.argv[1], sys.argv[2]
objs = []
for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="backups/"):
    objs += [o for o in page.get("Contents", []) if o["Key"].endswith(".enc")]
if not objs:
    raise SystemExit("bucket'ta yedek yok")
latest = max(objs, key=lambda o: o["LastModified"])
s3.download_file(bucket, latest["Key"], out)
want = (s3.head_object(Bucket=bucket, Key=latest["Key"]).get("Metadata") or {}).get("sha256")
got = hashlib.sha256(pathlib.Path(out).read_bytes()).hexdigest()
if not want or want != got:
    raise SystemExit("sha256 uyuşmuyor")
print(f"indirildi ve doğrulandı: {latest['Key']} ({latest['LastModified']})")
PY

# 2) Şifre çöz
BACKUP_PASSPHRASE="$PASS" openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 \
  -in "$WORK/latest.enc" -out "$WORK/latest.archive.gz" -pass env:BACKUP_PASSPHRASE || fail "şifre çözülemedi"

# 3) GEÇİCİ veritabanına geri yükle (canlıya dokunmaz)
mongorestore --quiet --uri="$MONGO_URL" --gzip --archive="$WORK/latest.archive.gz" \
  --nsFrom="${DB}.*" --nsTo="${TMPDB}.*" --drop || fail "mongorestore hatası"

# 4) Doğrula + geçici DB'yi sil
"$APP/venv/bin/python" - "$MONGO_URL" "$TMPDB" <<'PY' || fail "geri yüklenen veri doğrulanamadı"
import sys
from pymongo import MongoClient
c = MongoClient(sys.argv[1], serverSelectionTimeoutMS=10000)
d = c[sys.argv[2]]
try:
    counts = {n: d[n].estimated_document_count() for n in ("products", "orders", "users", "settings")}
    print("geri yüklenen kayıt sayıları:", counts)
    empty = [n for n, k in counts.items() if k <= 0]
    if empty:
        raise SystemExit(f"boş koleksiyon: {empty}")
finally:
    c.drop_database(sys.argv[2])
PY
echo "geri yükleme testi BAŞARILI"
logger -t atilla-restore-test "BAŞARILI" 2>/dev/null || true
