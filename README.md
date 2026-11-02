# Atilla — Beyaz Etiket E-Ticaret (Atilla altyapısı)

Mağaza + yönetim paneli (FastAPI + MongoDB backend, React frontend). Atilla kod tabanının
beyaz etiket kopyasıdır; firmaya özel her değer panelden / ortam değişkenlerinden gelir.

## Otomatik kurulum (terminal gerekmez)

Her push'ta `.github/workflows/deploy-server.yml` vitrini derler, sunucuya gönderir ve
`deploy/server_deploy.sh` ile kurar/günceller (ilk sefer: MongoDB, Python, Caddy, servis,
güvenlik duvarı, günlük yedek). Elle tetiklemek: **Actions → Sunucuya Otomatik Kurulum → Run workflow**.

### Bir kerelik ayar (GitHub web arayüzünden)
**Settings → Secrets and variables → Actions**

| Secret | Açıklama |
|---|---|
| `SSH_HOST` | Sunucu IP |
| `SSH_USER` | SSH kullanıcı adı (sudo yetkili) |
| `SSH_PASSWORD` | SSH parolası |
| `ADMIN_EMAIL` | Panelin ilk yönetici e-postası |
| `ADMIN_PASSWORD` | İlk yönetici parolası (en az 12 karakter; ilk girişte değiştirilir) |
| `PRODUCT_XML_URL` | Ticimax XMLExport ürün adresi (isteğe bağlı) — stoklu ürünler kategori ve varyantlarıyla aktarılır, sunucuda her 2 saatte bir stok senkronu çalışır |

| Variable (isteğe bağlı) | Açıklama |
|---|---|
| `SITE_DOMAIN` | Ör. `magaza.com` — boşsa site `http://SSH_HOST` adresinde açılır |
| `API_DOMAIN` | Ör. `api.magaza.com` (boşsa `api.SITE_DOMAIN`) |

Alan adı eklenince DNS'te `@`, `www`, `api` A kayıtlarını sunucu IP'sine yönlendirin; Caddy
SSL sertifikasını otomatik alır.

> Bu repo herkese açıktır: Actions log'ları da açıktır. Betikler hiçbir sırrı log'a yazmaz;
> sırlar yalnız GitHub Secrets'ta ve sunucudaki `/opt/atilla/backend/.env` (600) dosyasında durur.

## Beyaz etiket durumu
Plan ve kalan maddeler: `BEYAZ_ETIKET_Kurulum_ve_Denetim.md`.
Bu kopyada kapatılanlar: K1 (güvenlik uyarı alıcıları), K2 (yeni sipariş bildirimi),
K3 (ödeme dönüş izin listesi), ilk yönetici / panel sahibi e-postası.
