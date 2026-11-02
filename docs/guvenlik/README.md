# Güvenlik belgeleri — dizin ve kullanım

4 Ekim 2026 · Build `8c34c16c` (ilk değerlendirme aynı gün `c1f556b`)

Bu klasör, kullanıcının güvenlik paketini (blueprint sürüm 4: 42 iş, 151 test, 14 kapı, el kitabı, 10 ADR) **bu sistemin gerçek durumuna** göre doldurur. Belgelerde sır ve müşteri kişisel verisi yoktur.

## 1. Dosyalar

| Dosya | İçerik |
|---|---|
| [`guvenlik-test-kayitlari.json`](guvenlik-test-kayitlari.json) | 151 test kaydı (paket şeması). Her kayıtta durum, build, ortam, tarih, sahip, beklenen ve gerçek sonuç, kanıt (`dosya::test`, gerekçe ya da platform doğrulama adımı) var |
| [`is-durumu.md`](is-durumu.md) | 42 iş (BL-001…042): durum, commit, kanıt, kalan iş ve sahibi |
| [`yayina-cikis-kapisi.md`](yayina-cikis-kapisi.md) | G01–G14 güncel karar ve istisna kaydı (risk, telafi, sahip, bitiş, yeniden test şartı) |
| [`olay-mudahale-el-kitabi.md`](olay-mudahale-el-kitabi.md) | Bu sisteme uyarlanmış olay müdahalesi: **ilk 15 dakika** listesi, acil anahtarlar, 12 senaryo, mutabakat (iyzico, Doğan, MNG/DHL, pazaryerleri, İYS) |
| [`mimari-karar-kayitlari.md`](mimari-karar-kayitlari.md) | ADR-01…10: durum ve nasıl uygulandığı |
| [`kisisel-veri-envanteri.md`](kisisel-veri-envanteri.md) | Müşteri + influencer telefon/adres (IBAN/TC) envanteri: nerede, kim okur, nereye gider, koruma (maske, "Göster" denetimi, şifreleme, saklama süresi) |

İlgili eski belgeler: `docs/INCIDENT_RESPONSE_PLAN.md` (Amazon DPP, 24 saat bildirim), `docs/DATA_PROTECTION_POLICY.md`, `CLAUDE.md` (ödeme değişmezleri).

### Özet sayılar

- **Testler (151):** 125 geçti, 3 kısmen, 9 kod dışı, 14 uygulanamaz. (İlk değerlendirme `c1f556b`: 53 geçti, 75 kısmen. 4–5 Ekim turlarında CHAIN08/09, ADM-07/08, IAM-01, T19, SHOP12, COM12, AUTH14, T18, SHOP02 kapandı.)
- **İşler (42):** 29 tamamlandı, 9 kısmen, 3 kod dışı, 1 uygulanamaz. (İlk: 16 / 22 / 3 / 1.)
- **Kapılar (14):** 6 geçti (G01, G03, G05, G07, G09, G10), 5 koşullu (G02, G04, G06, G08, G14), 3 doğrulanmadı (G11 yedek, G12 deploy, G13 kapasite).
- **İstisnalar:** İ-01, İ-03, İ-06, İ-08, İ-09, İ-16 kapatıldı. 8 kapı istisnası açık (İ-02, 04, 05, 07, 10, 11, 12, 13) ve 4 kapı dışı kalıntı kaydı var (İ-14, İ-15, İ-17, İ-18).

"geçti" yalnız depodaki otomatik bir testin beklenen davranışın tamamını doğruladığı kayıtlar içindir. "kısmen" kayıtlarında neyin kaldığı, nedeni ve sahibi açıkça yazılıdır.

Hâlâ "kısmen" olan 3 test ve nedeni (üçü de kod dışı adım bekliyor):

| Test | Neden kısmen | Sahip |
|---|---|---|
| SDLC-02 | Geçmişteki 5 gerçek sır döndürülmedi (gitleaks kırmızı); ayrıca GitHub Actions faturalama nedeniyle CI işleri başlamıyor | Kullanıcı-Platform |
| SOC-01 | Onay/yükseltme/tatbikat mekanizması testli; ilk canlı tatbikatın 15 dk içinde onaylanması bekleniyor (Güvenlik Paneli → Tatbikat Başlat) | Kullanıcı-Platform |
| ADM-15 | Cihaz silme + asgari sürüm kodda; kaybolan/çevrimdışı cihaz için MDM ve cihaz politikası | Kullanıcı-Platform |

Kod tarafında kapatılan son maddeler: personel uçları en az yetkiye indirildi (IAM-02, 713 uç; mevcut rollerin erişimi korunarak), `/api/settings` bölündü, CSP zorlayıcı modda, vitrin build kimliği doğrulaması, iyzico komisyon defteri, uyarı onayı + tatbikat, kayıt taraması koruması, canlı yük ölçümü (`kanit/canli-yuk-olcumu-2026-10-04.json`).

## 2. Testler nasıl çalıştırılır

Testler gerçek veritabanına ya da dış servise **dokunmaz**. MongoDB bellek içi mongomock ile çalışır. iyzico, Doğan, SMS, e-posta ve R2 taklit edilir; uygulama ASGI üzerinden gerçek rotalarıyla çağrılır.

```bash
# bir kez (sanal ortamda)
python -m pip install -r backend/requirements.txt -r backend/requirements-test.txt

# güvenlik uçtan uca seti + rota kapısı + SSRF
python -m pytest -q backend/tests/security_e2e backend/tests/test_route_guards.py backend/tests/test_security_fetch.py
```

Bugünkü sonuç (build `8c34c16c`):

- `security_e2e`: 736, `test_route_guards`: 3, `test_security_fetch`: 41 → **780 geçti, 0 kaldı** (`security_e2e` ~80 sn).
- Build `280b0c20` (CHAIN08 + CHAIN09): `security_e2e` 769, `test_route_guards` 3, `test_security_fetch` 41 → **813 geçti, 0 kaldı**.
- Quality Gate'teki ek güvenlik testleri (`backend` dizininden ya da `PYTHONPATH=backend` ile) de geçiyor: `test_security_hardening.py` 30, `test_activity_audit.py` 4, `test_dependency_audit_gate.py` 12, `test_google_auth_flow.py` 3, `test_sns_security.py` 13, `test_stock_audit.py` 5.
- Yeni test dosyaları (`8c34c16`): `test_identity2_*` (kimlik, OTP, SMS bütçesi, personel faktörleri, izin matrisi, yönetim işlemleri), `test_commerce2_*` (sipariş erişimi, kupon, para zinciri, durum yarışları, itiraz, gizlilik), `test_infra2_*` (XML bütünlüğü, görsel izolasyonu, tazelik, iyzico mutabakatı, outbox, script/CSP, push, restore), `test_payment_url_parser_confusion.py`.
- Hazırlık ortamı için yük aracı (CI dışı): `backend/tests/load/checkout_latency.py`. Varsayılan yalnız localhost'a izin verir; canlıya yöneltilmez.
- Bağımlılık taraması: pip-audit **0 açık**. yarn audit kırmızı (frontend `yarn.lock` güncellemesi onay bekliyor). gitleaks kırmızı (geçmişte 5 gerçek sır; rotasyon bekliyor).

CI'da her push'ta `.github/workflows/quality.yml` çalışır: backend birim testleri, güvenlik e2e seti ve frontend build. Ayrıca `security.yml` (CodeQL, gitleaks, pip-audit, yarn audit) ve aylık `infra-scan.yml` (Trivy) çalışır.

Ödeme veya sipariş koduna dokunan her değişiklikten sonra `CLAUDE.md` kontrol listesi uygulanır: derleme, bu set, deploy sonrası canlı test ve smoke.

## 3. Canlı öz-test nasıl okunur

`backend/security/selftest.py` canlı ortamda çalışır:

- açılıştan 5 dk sonra,
- her gün 05:13 UTC'de (08:13 TR),
- elle: Güvenlik Paneli → öz-test kartı → Çalıştır (`POST /api/admin/security/selftest/run`, yalnız süper admin).

Öz-testten ayrı olarak **güvenlik hattı kontrolü** 5 dakikada bir çalışır. Denetim arşivine aktarım 30 dk'dan fazla gecikirse, son 60 dk'da teslim edilemeyen uyarı varsa ya da denetim zincirinde yeni boşluk oluşursa kritik `security_pipeline_degraded` uyarısı gönderir.

Diğer güvenlik zamanlanmış işleri (hepsi denetimde `system:<iş>` kimliğiyle görünür):

| İş | Ne zaman | Ne yapar |
|---|---|---|
| `integration_freshness_check` | 15 dk'da bir | Pazaryeri sipariş çekme / stok gönderimi beklenen aralığın 5 katı (en az 30 dk) başarısızsa uyarı; düzelince "yeniden çalışıyor" |
| `audit_outbox_flush` | 5 dk'da bir | Yazılamamış (kritik olmayan) denetim kayıtlarını zincire tam bir kez aktarır |
| `iyzico_daily_reconciliation` | 04:23 UTC | Son 3 günün ödenmiş kart siparişlerini iyzico ile karşılaştırır (salt-okunur); uyuşmazlıkta kritik uyarı |
| `kvkk_suppression_archive` | 03:37 UTC | Pazarlama ret listesini (yalnız HMAC'li e-posta) gizli arşive yazar |

Rapor yalnız sayı ve durum içerir, kişisel veri içermez. Sonuç Güvenlik Paneli kartında, Entegrasyon Logları'nda (service=security) ve e-postada görünür. E-posta günde bir kez ya da bir kontrol HATA verdiğinde gider; alıcılar İşletme Kuralları → `security.alert_emails`.

| Kontrol | TAMAM | UYARI / HATA ne demek, ne yapılır |
|---|---|---|
| Gizli R2 arşivi | Yaz/oku/sil turu başarılı | HATA: `R2_ARCHIVE_BUCKET` yok ya da erişilemiyor → Railway Variables ve R2 token |
| Denetim zinciri ve arşivi | Zincir sağlam, arşive aktarıldı | HATA "zincir bozuk": kayıt silinmiş ya da değişmiş → el kitabı §4.1 adım 9. HATA "arşive aktarım": R2 |
| Şifreli sırlar çözülebiliyor | Tüm `v1:` değerler çözülüyor | HATA: anahtar değişmiş (`SECRETS_MASTER_KEY` ya da `JWT_SECRET`) → değişikliği **geri al**. UYARI: SMK tanımlı değil |
| Güvenlik ortam değişkenleri | Hepsi tanımlı | UYARI: eksik ya da 32 karakterden kısa değişken adı (değer asla okunmaz) |
| Acil anahtarlar | Hepsi kapalı | UYARI: açık anahtar listesi. Olay bittiyse kapat |
| Oturum dönemi | Okunuyor | — |
| KVKK silme kaydı arşivi | Yazılabiliyor | HATA: silme kayıtları yedeklenemez |
| Veri koruma durumu | Düz metin sayıları 0 | UYARI: düz metin dekont, IBAN, vergi/TC no ya da 30 günden eski arşivlenmemiş dekont var. Gece işleri temizler; sürerse Kodlama |
| Personel / süper admin | ≥1 aktif süper admin | HATA: aktif süper admin yok. MFA'sız personel sayısı bilgi amaçlıdır; girişte kod zaten zorunlu |
| Uyarı e-postası teslimi | Alıcı başına "ok/resend" ya da "ok/magaza_maili" | HATA: hiçbir kanal ulaşmadı → el kitabı §4.12 |
| Güvenlik kayıt/uyarı hattı | Arşiv güncel, uyarılar teslim, zincirde yeni boşluk yok | HATA: arşiv gecikmesi, teslim edilemeyen uyarı ya da zincir boşluğu → el kitabı §4.12 ve §4.1 adım 9 |
| Arşiv referansları (restore mutabakatı) | En çok 50 örnek referansın nesnesi arşivde | HATA: dekont / fatura PDF'i / mezar taşı nesnesi yok (çoğunlukla geri yüklemeden sonra) → el kitabı §4.9. UYARI: arşiv kapalı |
| Vitrin script host manifesti | Vitrindeki tüm script host'ları manifestte | HATA: manifest dışı host (panel dışından DB'ye yazılmış olabilir) → el kitabı §4.5. UYARI: manifest henüz oluşmadı |
| Denetim outbox'ı | Bekleyen kayıt yok | UYARI: zincire aktarılmayı bekleyen kayıt var. HATA: yerel outbox tavanı doldu, kayıt düştü → §4.12 |
| Pazarlama ret listesi arşivi | Arşiv güncel | UYARI: gece işi henüz çalışmadı ya da arşiv doğrulanamadı |
| Entegrasyon tazeliği | İzlenen entegrasyonlar zamanında | UYARI: bayat entegrasyon listesi → el kitabı §4.8 |

## 4. İşletme sahibinin platformlarda yapacakları

Bu adımlar kodla yapılamaz. Her biri bir test ya da kapı kaydını kapatır.

### MongoDB Atlas (T15, DR-02, BL-017, BL-018, BL-021)

1. **Ayrı DB kullanıcısı:** Database Access'te uygulama için yalnız `readWrite@<veritabanı>` rolü olan bir kullanıcı tanımla. `atlasAdmin`, `dbAdmin` ve `backup` rolü verilmez. Railway `MONGO_URL` bu kullanıcıyla güncellenir; insanlar bu kullanıcıyı kullanmaz.
2. **Network Access:** `0.0.0.0/0` yerine mümkünse Railway çıkış IP'leri. Railway sabit IP vermiyorsa en azından güçlü, döndürülebilir şifre.
3. **Yedek:** Cloud Backup açık olmalı (sürekli yedek / Point-in-Time tercih edilir). Saklama en az 7 gün günlük ve 4 hafta haftalık. Mümkünse **Backup Compliance Policy** açılır; bu, yedeklerin silinmesini engeller.
4. **Restore tatbikatı (T16/DR-01):** Snapshot'ı ayrı bir cluster'a geri yükle ve el kitabı §4.9 adımlarını dene. RPO ve RTO'yu `yayina-cikis-kapisi.md` İ-10'a yaz.
5. **MFA:** Atlas hesabında MFA zorunlu olmalı. Proje Owner rolünde en az iki kişi, ikisi de MFA'lı.
6. **Alarm:** Alerts bölümünde "backup snapshot deleted / failed", "user created" ve "network access changed" uyarıları tanımlanır.

### Railway (T20, SDLC-01, ADM-09, BL-036)

1. Hesapta **MFA** açık olmalı; ekip üyeleri gözden geçirilir.
2. Servis → Settings → Deploy: **"Wait for CI"** açılır. GitHub'daki Quality Gate ve Security Scan yeşil olmadan deploy başlamaz.
3. GitHub → Settings → Branches → `main` için branch protection kurulur. Zorunlu kontroller: Quality Gate (backend, security-e2e, frontend) ve Security Scan (gitleaks). GitHub hesabında da MFA olmalı.
4. Variables'ta break-glass değişkenleri (`ADMIN_MFA_ENFORCE`, `ADMIN_MFA_CHANNEL_POLICY`, `TWO_PERSON_RULE`, `ALLOW_DIRECT_STAFF_CREATE`, `STAFF_IDLE_TIMEOUT`) **tanımlı olmamalı**. Tanımlıysa her açılışta kritik uyarı gelir.

### Cloudflare (BL-017, DR-02)

1. Hesapta **MFA** açık olmalı; üyeler ve API tokenları gözden geçirilir.
2. Alan adına **Registrar/DNS kilidi** konur ve DNSSEC açılır.
3. **Transform Rule:** API isteklerine `X-Atilla-Edge: <CF_EDGE_SECRET>` başlığı eklenir. Aynı değer Railway'de `CF_EDGE_SECRET` olarak tanımlanır; böylece origin'e doğrudan gelen istek hız sınırını atlatamaz.
4. **R2:** Uygulama tokenı yalnız ilgili bucket'larla sınırlanır. Arşiv bucket'ı için Object Lock ya da retention kuralı değerlendirilir.
5. **Resend alan adı doğrulaması:** Resend → Domains'te gönderen alan adı "Verified" olmalı. SPF, DKIM ve MX kayıtları Cloudflare DNS'e eklenir. Doğrulanana kadar uyarılar mağaza mail altyapısından gider; öz-test bunu "ok/magaza_maili" olarak gösterir.

### Railway Variables — güvenlik ortam değişkenleri

Değerler en az 32 karakter rastgele olmalı (ör. `python -c "import secrets;print(secrets.token_urlsafe(48))"`). Yalnız Railway Variables'a girilir; sohbete, e-postaya ya da koda yazılmaz.

| Değişken | Ne için | Not |
|---|---|---|
| `SECRETS_MASTER_KEY` | Alan ve sır şifreleme ana anahtarı (Fernet biçimi: `python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"`) | Eklenince eski kayıtlar okunmaya devam eder (`0a3ce4f`). **Bir kez eklendikten sonra değiştirilmez**; rotasyon yeniden şifreleme betiği ister (Kodlama) |
| `KVKK_TOMBSTONE_SECRET` | KVKK silme mezar taşı HMAC anahtarı (JWT ve SMK rotasyonundan bağımsız) | Mezar taşları mevcut tüm anahtarlarla (bu değişken, SMK türevi, JWT türevi) özetlenir. Tek anahtar değişimi eşleşmeyi bozmaz, ama gereksiz yere değiştirilmez |
| `PII_HASH_SECRET` | TCKN/VKN tam eşleşme araması (HMAC kör indeks) | Eklenince gece işi yeniden özetler; eskiler bulunmaya devam eder. Öz-test bu değişkeni ayrıca kontrol etmez, elle doğrula |
| `INVOICE_LINK_SECRET` | İmzalı fatura/belge linkleri | Eklenmeden önce üretilmiş linkler de geçerli kalır (`69ff6d7`) |
| `R2_ARCHIVE_BUCKET` | Gizli arşiv deposu (genelden ayrı) | Öz-test "Gizli R2 arşivi" |
| `CF_EDGE_SECRET` | Cloudflare kenar doğrulaması | Yukarıdaki Transform Rule ile birlikte |
| `RESEND_API_KEY`, `RESEND_FROM` | Güvenlik uyarı e-postası | Alıcılar İşletme Kuralları → `security.alert_emails` |
| `PII_REVEAL_HOURLY_MAX` (isteğe bağlı) | Toplu kişisel veri açma alarm eşiği (personel başına saatte tam IBAN/TCKN/VKN/kasa açma) | Tanımsızsa 50. Aşımda kritik uyarı ve 1 saat engel |
| `AUDIT_OUTBOX_DIR`, `AUDIT_OUTBOX_MAX_BYTES` (isteğe bağlı) | Mongo kesintisinde denetim kayıtlarının yazıldığı yerel klasör ve tavanı | Varsayılan `backend/data/audit_outbox`, 20 MB. Railway'de dosya sistemi kalıcı değildir; Mongo kesintisi sürerken deploy yapılmaz |
| **`JWT_SECRET`** | Oturum imzası | **ASLA DEĞİŞTİRME.** Eski şifreli kayıtlar, KVKK mezar taşları, PII arama özeti ve eski fatura linkleri bundan türetilmiş anahtarlara bağlı olabilir; değişirse bu veriler çözülemez ve herkes oturumdan düşer. Sızıntı şüphesinde el kitabı §4.10'daki sıra izlenir (önce Kodlama) |

### SMS sağlayıcısı (BL-006)

Uygulamada küresel SMS tavanı var: İşletme Kuralları → Güvenlik → `sms.global_hourly_cap` (varsayılan 300) ve `sms.global_daily_cap` (varsayılan 2000). Bunları yalnız süper admin değiştirebilir. Sağlayıcı panelinde ayrıca günlük harcama limiti ve bakiye uyarısı tanımlanır, uluslararası gönderim kapatılır. Bu ikinci savunma hattıdır.

### iyzico

1. Merchant Panel kullanıcılarında MFA olmalı.
2. Bildirim (webhook) URL'si ve ortam (canlı) doğrulanır.
3. Aylık mutabakat el kitabı §5'e göre yapılır.
4. Kart verisinin backend'den geçmemesi için iyzico Checkout Form (barındırılan form) değerlendirilir (BL-015).

### GitHub ve bağımlılıklar (BL-036, SDLC-02)

1. **Geçmişteki 5 gerçek sır:** gitleaks bunlar yüzünden kırmızı. Değerler HEAD'de yok, yalnız git geçmişinde. Liste `.gitleaksignore`'da `PENDING-ROTATION` satırlarında (dosya ve kural adı; değer yok). Her biri ilgili sağlayıcı panelinde döndürülür ve eski değerin geçersiz olduğu doğrulanır. Sonra satırdaki `# PENDING-ROTATION ` öneki silinip rotasyon tarihi yazılır. Geçmişi yeniden yazmak rotasyonun yerine geçmez.
2. **Frontend bağımlılık güncellemesi:** yarn audit, `yarn.lock`'taki axios, react-router ve form-data sürümleri yüzünden kırmızı. Güncelleme sahibin onayıyla yapılır (CLAUDE.md: `npm install` yapılmaz) ve `CI=true` build ile doğrulanır.

### Süreç

1. 3 ayda bir masa başı tatbikat yapılır (el kitabı §6).
2. El kitabı §0 irtibat tablosu doldurulur.
3. Bağımsız sızma testi planlanır (BL-041).
4. `yayina-cikis-kapisi.md` istisnaları bitiş tarihinde yeniden değerlendirilir.

## 5. En önemli kalan işler

İlk değerlendirmedeki yedi önemli işin dördü kodda kapatıldı: iki kişi onayı, kanarya kart testi, müşteri OTP testleri ve küresel SMS tavanı, atomik kupon rezervasyonu, restore kapanışında otomatik iptal. Kalanlar:

1. **Geçmişteki 5 gerçek sırrın rotasyonu** (İ-11, SDLC-02). gitleaks kırmızı. Sahip: Kullanıcı.
2. **Railway "Wait for CI" ve branch protection** (İ-11). Sahip: Kullanıcı.
3. **Frontend `yarn.lock` güncellemesi onayı** (İ-11). yarn audit kırmızı. Sahip: Kullanıcı (onay) + Kodlama.
4. **Atlas ayrı kullanıcı, yedek koruması, MFA ve restore tatbikatı** (İ-10). Tatbikatta `freeze_refunds` + iyzico mutabakatı adımı da uygulanır. Sahip: Kullanıcı.
5. **İkinci MFA'lı süper admin** (İ-04). Tek süper adminle iki kişi kuralı uygulanamaz. Sahip: Kullanıcı.
6. **CSP'yi zorlayıcı moda alma** (İ-15). Önce birkaç hafta Report-Only raporları izlenir. Sahip: Kodlama + Kullanıcı kararı.
7. **Masa başı tatbikat ve irtibat listesi** (İ-13). Sahip: Kullanıcı.
8. **Hazırlık ortamında yük ölçümü** (İ-12). Sahip: Kullanıcı (ortam) + Kodlama.
