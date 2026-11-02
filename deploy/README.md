# Yeni Marka — Tek Sunucu Kurulumu (2 GB RAM / 5 GB disk)

Pazaryeri, İYS ve veri senkronu OLMAYAN bir marka için tek VPS kurulumu.
Çalışanlar: mağaza + panel, iyzico ödeme, kargo, e-fatura, e-posta.

> Gereken: Ubuntu 22.04/24.04, **root/SSH erişimi**. (Yalnız cPanel paneli varsa bu kurulum
> yapılamaz — MongoDB kurulamaz ve sürekli çalışan süreç tutulamaz.)

## 1) DNS (alan adı sağlayıcısında)

| Kayıt | Tip | Hedef |
|---|---|---|
| `yenimarka.com` | A | VPS IP |
| `www` | A | VPS IP |
| `api` | A | VPS IP |

Görseller Cloudflare R2'de durur (`cdn.yenimarka.com` → R2 public bucket). Diskte görsel
tutmayın; 5 GB kısa sürede dolar.

## 2) Kurulum (tek komut)

```bash
ssh root@SUNUCU_IP
curl -fsSL https://raw.githubusercontent.com/<repo>/main/deploy/install.sh -o install.sh
# ya da dosyayı scp ile kopyalayın
bash install.sh yenimarka.com api.yenimarka.com
```

Script şunları yapar: 2 GB swap, MongoDB 7 (önbellek 512 MB'a sabit), Python sanal ortamı,
bağımlılıklar, systemd servisi, Caddy ile otomatik SSL, günlük yedek (03:00).

## 3) Ayarlar

`/opt/facette/backend/.env` dosyasını doldurun (`deploy/env.example` kopyasıdır):
zorunlu olanlar `MONGO_URL`, `DB_NAME`, `JWT_SECRET`, `CORS_ORIGINS`, iyzico anahtarları,
R2 anahtarları, yedek ayarları (`R2_BACKUP_BUCKET`, `BACKUP_PASSPHRASE`), SMTP. Sonra: `systemctl restart facette-api`.

## 4) Panel içi kapatılacaklar (bu markada kullanılmayanlar)

Ayarlar → Entegrasyonlar: Trendyol, Hepsiburada, Amazon, Temu **bağlanmaz** (anahtar
girilmezse zamanlanmış işleri de çalışmaz). İYS/SMS kullanılmayacaksa NetGSM boş bırakılır.

## 5) Kaynak beklentisi (ölçüm)

| | |
|---|---|
| Backend (pazaryerisiz) | ~250–300 MB |
| MongoDB (önbellek 512 MB) | ~700 MB |
| İşletim sistemi + Caddy | ~250 MB |
| **Toplam** | **~1,2 GB / 2 GB** |

Disk: OS ~2 GB + Python paketleri ~0,7 GB + MongoDB ~0,4 GB → veri için ~1,8 GB kalır.
Pazaryeri aynaları olmadığı için veri yavaş büyür; yedekler sunucu dışına alınmalı.

## 6) Veritabanı aynı sunucuda — dikkat edilecek tek şey

MongoDB uygulamayla **aynı sunucuda** çalışır (kurulum bunu yapar): ek ücret yok, ağ
gecikmesi yok, yönetim tek yerden. Bedeli: sunucu giderse veri de gider. Bu yüzden
`deploy/backup.sh` her gece uygulamanın bağlandığı veritabanının (`MONGO_URL` + `DB_NAME`)
`mongodump`'ını alır, arşivi doğrular, **şifreler** (`BACKUP_PASSPHRASE`) ve **ayrı, gizli**
bir R2 bucket'ına (`R2_BACKUP_BUCKET`) yükler; buluttaki kopyanın boyutu + SHA-256'sı
doğrulandıktan sonra diskte yalnız son 2 kopyayı bırakır. Herhangi bir adım başarısızsa
betik hata koduyla çıkar (`/var/log/facette-backup.log` + syslog `facette-backup`).

> ⚠️ Yedekleri **asla** görsellerin durduğu public bucket'a koymayın. Yedek bucket'ında public
> erişim / r2.dev / özel alan adı KAPALI olmalı; mümkünse yalnız o bucket'a yazabilen ayrı
> bir R2 anahtarı kullanın (`R2_BACKUP_ACCESS_KEY_ID` / `R2_BACKUP_SECRET_ACCESS_KEY`).

Kurulum her hafta `deploy/restore_test.sh` ile otomatik **geri yükleme testi** yapar: buluttaki en son
yedek indirilir, SHA-256'sı doğrulanır, şifresi çözülür ve canlıya dokunmadan geçici `restore_test`
veritabanına yüklenip temel koleksiyonların dolu olduğu kontrol edilir (`/var/log/facette-restore-test.log`).

Elle geri yükleme:
```bash
# yerel kopyadan
mongorestore --gzip --archive=/var/backups/facette/<dosya>.archive.gz --drop
# buluttan indirilen şifreli kopyadan
openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -in <dosya>.enc -out <dosya>.archive.gz
mongorestore --gzip --archive=<dosya>.archive.gz --drop
```

MongoDB'yi ileride ayırmak isterseniz (MongoDB Atlas ücretsiz katman) tek yapılacak
`.env` içindeki `MONGO_URL`'i değiştirmek; kod değişmez.

## 7) Frontend

`frontend/` derlenip `/var/www/yenimarka` altına konur (Caddy servis eder) ya da
Cloudflare Pages'e verilir. Derleme sunucuda 2 GB RAM'i zorlar; **kendi bilgisayarınızda
derleyip** `build/` klasörünü kopyalamanız önerilir:

```bash
cd frontend && CI=true npx craco build
scp -r build/* root@SUNUCU_IP:/var/www/yenimarka/
```
