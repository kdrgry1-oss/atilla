# Beyaz Etiket — Yeni Firma Kurulum Planı + Güncel Sabit Değer Denetimi

**Güncelleme:** 01.11.2026 (önceki sürüm: 10.08.2026)
**Amaç:** Sistemi (site + yönetim paneli + mobil uygulamalar) başka bir firma için, Atilla'ya
dokunmadan, ayrı ve bağımsız bir kurulum olarak çalıştırmak.

---

## 1. Durum özeti

Altyapı hazır: firma kimliği tek merkezden (`tenant_config` → `company.py`) okunuyor ve panelden
düzenlenebiliyor. Mağaza adı, logo, unvan, vergi, IBAN, adres, iletişim, sosyal medya, alan adları,
para birimi, sipariş no öneki, kargo göndericisi ve SEO şablonları buraya bağlı.

**Ancak** kodun birçok yerinde ya doğrudan "Atilla" yazıyor ya da ayar boşsa Atilla değerine
düşülüyor. 10.08 denetimindeki 17 maddenin hiçbiri tam kapanmamış, yeni kritik maddeler de var.
Bunlar temizlenmeden yeni firma kurulursa o firmanın müşterileri Atilla adını, adresini,
telefonunu görür; bazı veriler (güvenlik uyarıları, yeni sipariş e-postası) Atilla'ya gider;
ödeme dönüşü ve tarayıcı güvenlik başlıkları yeni alan adında çalışmaz.

## 2. Kritik bulgular (yeni firmada mutlaka düzeltilmeli)

| # | Alan | Sorun |
|---|---|---|
| K1 | Güvenlik uyarıları | Yeni kurulumda uyarı alıcıları Atilla kişilerine (e-posta + SMS) ayarlanıyor (`scheduler.py` tohumlama) |
| K2 | Yeni sipariş bildirimi | Firma e-postası boşsa sipariş bildirimi info@atilla.com.tr'ye gidiyor (`orders.py`, `payment.py`) |
| K3 | Ödeme | Dönüş/callback izin listesi yalnız atilla.com.tr; yeni alan adında ödeme dönüşü bozulur (`payment.py`); iyzico alıcı yedeği "Atilla" |
| K4 | Tarayıcı güvenliği | `frontend/public/_headers` CSP yalnız api/cdn.atilla.com.tr'ye izin veriyor → yeni alan adında site API'ye ulaşamaz |
| K5 | İade / kargo | İade sözleşme no (490059279), iade adresi, telefon, MNG gönderici adı sabit (`orders.py`, `rooftr_returns.py`, `ReturnRequest.jsx`) |
| K6 | Fatura / e-Fatura | Fatura başlığı "ATILLA", tedarikçi VKN/unvan/adres yedekleri Atilla (`orders.py`, `dogan_client.py`) |
| K7 | Pazaryeri | Trendyol/HB/Temu GPSR üretici-ithalatçı bilgisi her zaman Atilla (`atilla_defaults.py`); varsayılan ürün markası "ATILLA" |
| K8 | SMS / e-posta metinleri | Sipariş, OTP, hoş geldin, durum, kimlik doğrulama, pazarlama, abonelikten çık metinleri "Atilla"; gönderen adı yedeği "ATILLA" |
| K9 | Vitrin | Header/Footer logosu sabit `/logo.webp`; footer iletişim, "Atilla Kulübü", SSS, gizlilik e-postaları, sipariş onayı, anasayfa görselleri |
| K10 | Yapay zekâ | Sohbet botu ve WhatsApp botu kendini "Atilla" olarak tanıtıyor; ürün açıklaması üreticisi "Sen ATILLA…" |
| K11 | Hazır içerik | "Varsayılan sayfaları yükle" KVKK/sözleşme/SSS metinleri Atilla unvan, VKN, Mersis, adres içeriyor |
| K12 | Mobil | Uygulama kimlikleri (tr.com.atilla.admin / com.atilla.app), adı, URL şeması, codemagic, CORS şeması sabit |

Orta/düşük öncelikli: index.html statik meta/JSON-LD, robots.txt, llms.txt, Google Merchant feed
başlığı, CAPI/pixel marka-affiliation yedekleri, CDN host kontrolleri, panel içi "ATILLA"
yazıları, TOTP adı, indirim rozeti rengi (#7b1e2b), `deploy/env.example` eksik değişkenler.

## 3. Plan

### Faz 1 — Kodu tamamen ayardan okur hale getirmek (Atilla'yı bozmadan)
1. Atilla'nın şu an koddan gelen değerleri (iade adresi, sözleşme no, GPSR, fatura başlığı, footer,
   e-posta/SMS imzası…) **önce Atilla'nın kendi ayarlarına** yazılır (yalnız boş alanlar;
   kullanıcının girdiği değerler korunur). Böylece Atilla'da hiçbir şey değişmez.
2. Ardından koddaki Atilla sabitleri/yedekleri kaldırılır; her yer ayardan okur, ayar boşsa
   nötr davranır (ör. "Mağaza", boş alan) — asla başka firmanın verisi değil.
3. Yeni ayar alanları: iade sözleşme no + iade adresi, GPSR üretici/ithalatçı, varsayılan ürün
   markası, e-posta gönderen adı, bot kişiliği, marka rengi (rozet), mobil mağaza linkleri/şema.
4. Güvenlik uyarı alıcıları, sipariş bildirim adresi, ödeme izin listesi, CSP → tamamen
   kurulumun kendi alan adı/ayarından.
5. Vitrin logosu/favikon/OG görseli firma ayarından; statik index/robots/llms şablon hale.
6. Testler + canlı Atilla smoke (sitede/e-postalarda/faturada hâlâ Atilla doğru görünmeli).

### Faz 2 — Yeni firma kurulumu (Atilla'dan tamamen ayrı)
- **Hesaplar firmanın kendisine ait olmalı:** MongoDB Atlas, Railway (backend), Cloudflare
  (site + alan adı + R2 görsel deposu), GitHub (kod kopyası), Apple/Google geliştirici hesapları.
- Yeni anahtarlar: `JWT_SECRET`, `SECRETS_MASTER_KEY`, `PII_HASH_SECRET`, `KVKK_TOMBSTONE_SECRET`,
  `INVOICE_LINK_SECRET`, `ALERT_ACK_SECRET`, `CF_EDGE_SECRET`, `ADMIN_INITIAL_PASSWORD`
  (Atilla'nınkiler ASLA kopyalanmaz).
- Alan adı değişkenleri: `SITE_URL`, `CORS_ORIGINS`, `PUBLIC_API_URL`, `REACT_APP_BACKEND_URL`,
  `CDN_URL`/`R2_*`, `OWNER_SUPER_ADMIN_EMAILS`, `ALERT_TO_EMAIL`.
- Panelden: firma bilgileri, logo, iade adresi, e-posta/SMS gönderen, şablonlar, KVKK/sözleşme
  sayfaları (firmanın kendi hukuki metinleri), güvenlik uyarı alıcıları.
- Firmanın kendi sağlayıcı hesapları: iyzico, NetGSM + İYS başlığı, MNG/kargo, Doğan e-Fatura,
  pazaryeri API anahtarları, Meta/TikTok/Google pixel, WhatsApp, e-posta (SES/Brevo/ZeptoMail).
- Veri: boş veritabanıyla başlar; Atilla'nın müşteri/sipariş/ürün verisi taşınmaz.
- Kurulum sonrası smoke + test siparişi.

### Faz 3 — Mobil uygulamalar (isteğe bağlı)
- Yeni uygulama kimliği, adı, ikon/splash, URL şeması, codemagic iş akışı, firmanın kendi
  App Store Connect / Google Play hesabı, APNs anahtarı.

## 4. Kurulum kontrol listesi (yeni firma)
- [ ] Faz 1 kod temizliği canlıda, Atilla smoke temiz
- [ ] Firmaya ait Atlas / Railway / Cloudflare / GitHub hesapları açıldı
- [ ] Yeni sırlar üretildi, env değişkenleri girildi (`deploy/env.example` güncel listesiyle)
- [ ] Alan adı + api alt alan adı + CDN bağlandı
- [ ] Panel: firma bilgileri, logo, iade, gönderen adları, şablonlar, yasal sayfalar
- [ ] Sağlayıcı hesapları (ödeme, SMS/İYS, kargo, e-Fatura, pazaryeri, pixel, e-posta)
- [ ] Güvenlik uyarı alıcıları firmanın kendi kişileri
- [ ] Test siparişi: ödeme → e-posta/SMS → fatura → kargo etiketi → iade; hiçbir yerde "Atilla" yok
- [ ] (İsteğe bağlı) mobil uygulamalar
