# Mimari karar kayıtları — Atilla durumu

4 Ekim 2026 · Build `8c34c16c` · Kaynak: güvenlik paketi `mimari-karar-kayitlari.md` (ADR-01…10)

Paketteki ADR'ler öneri olarak gelmişti. Aşağıda her birinin bu sistemdeki karşılığı yazılı. Sistemin topolojisi şöyle:

- **Backend:** Railway (FastAPI).
- **Vitrin ve panel:** Cloudflare Pages (aynı origin).
- **Veritabanı:** MongoDB Atlas.
- **Dosya depolama:** Cloudflare R2. Genel bir depo ve ayrı, herkese kapalı bir arşiv deposu (`R2_ARCHIVE_BUCKET`) var.
- **Ödeme:** tek PSP iyzico (3DS).
- **Diğer:** Doğan e-belge, MNG/DHL kargo, Trendyol/HB/Amazon pazaryerleri, İYS, Resend.

Durum değerleri:

- **uygulandı**: karar kodda var ve testli.
- **kabul edildi**: yönü benimsendi; bir kısmı platformda ya da sonraki işte.
- **ertelendi**: bilinçli olarak sonraya bırakıldı; gerekçesi ve telafisi yazılı.
- **reddedildi**: bu mimariye uymuyor.

| ADR | Konu | Durum |
|---|---|---|
| ADR-01 | Tek VDS ve bağımsız güven sınırları | kabul edildi (yönetilen platformlarla uyarlandı) |
| ADR-02 | Müşteri ve admin kimlik sınırı | uygulandı |
| ADR-03 | DB authoritative durum, cache ve invariant | uygulandı (DB rol ayrımı platformda) |
| ADR-04 | Inbox/outbox ve dış etki mutabakatı | uygulandı (uyarlanmış: broker yok; denetim outbox'ı var) |
| ADR-05 | Parser ve converter izolasyonu | ertelendi (süreç izolasyonu); iş parçacığı + bütçe uygulandı |
| ADR-06 | Harici KMS, veri minimizasyonu, alan şifreleme | kabul edildi; alan şifreleme uygulandı, harici KMS ertelendi |
| ADR-07 | DR tutarlı kesit, fencing ve revocation | kabul edildi; kod tarafı uygulandı, tatbikat platformda |
| ADR-08 | Egress policy ve endpoint sürümü | uygulandı (endpoint sürümleme uygulanamaz) |
| ADR-09 | Audit/monitoring arızasında iş davranışı | uygulandı |
| ADR-10 | Release artifact, rollback ve şema uyumu | kabul edildi; imzalı artefakt ertelendi |

---

## ADR-01 — Tek VDS ve bağımsız güven sınırları

**Durum:** kabul edildi (yönetilen platformlarla uyarlandı).

VDS yok. Güven sınırları zaten ayrı hesaplarda: uygulama (Railway), veritabanı (Atlas), dosya ve arşiv (R2), DNS ve kenar (Cloudflare), kod ve CI (GitHub). Kodda yapılanlar:

- Gizli arşiv deposu genel depodan ayrı. Uygulama arşive yazar ve okur, sonra doğrular; ancak bundan sonra DB kopyasını siler (`test_infra_private_archive.py`).
- Origin, Cloudflare kenarını `CF_EDGE_SECRET` ile tanır.

**Kalan (Kullanıcı-Platform):**

- Atlas'ta uygulamaya ayrı ve yalnız `readWrite` yetkili kullanıcı tanımla.
- Yedek silme yetkisini yalnız MFA'lı proje sahibine bırak.
- Tüm konsollarda MFA zorunlu olsun.
- R2 arşiv tokenında silme izni olmamalı ya da Object Lock kullanılmalı.

Ayrıntı README §4'te.

**Risk:** Konsol hesaplarından birinin ele geçirilmesi o katmanın tamamını etkiler.

## ADR-02 — Müşteri ve admin kimlik sınırı

**Durum:** uygulandı (`1870c1a`, `5779a74`, `e7cedef`).

- Token'da `typ` (staff/customer) taşınır. Personel tokenı 12 saat, müşteri tokenı 7 gün geçerlidir; personel için boşta kalma zaman aşımı vardır.
- Admin kapısı rolü ve `is_admin` bayrağını her istekte DB'den okur.
- Admin MFA fail-closed çalışır ve kanal ayrımı vardır. Faktör değişimi ve onay yakın zamanlı kimlik doğrulama ister.
- Rota envanteri statik testle denetlenir (`test_route_guards.py`).
- Çerez tabanlı oturum yok (Bearer). Impersonation özelliği yok.

**Kanıt:** AUTH01/03/04/05 ve T12 geçti.

**Kalan:**

- Panel vitrinle aynı origin'de çalışıyor. Vitrinde bir XSS, localStorage'daki tokenı okuyabilir; telafi: CSP zorlayıcı modda (`0fda42a2`, host izin listesi — dış host'a script/bağlantı engellenir); `unsafe-inline` kaldığından satır içi XSS yine çalışabilir (BL-030 kalan iş).
- Personel faktör sıfırlaması artık iki kişi onayına bağlı ve testli (`8c34c16`). Tek aktif süper admin varsa onaysız uygulanır ve uyarı üretir; ikinci süper admin tanımlanmalı (İ-04, Kullanıcı-Platform).

## ADR-03 — DB authoritative durum, cache ve invariant

**Durum:** uygulandı; DB rol ayrımı platformda.

- Finansal kesinlik MongoDB üzerinde atomik koşullu güncellemelerle sağlanır:
  - stok düşümü ve oversell koruması,
  - `paid` geçişi,
  - iade tutarı rezervasyonu,
  - hediye çeki bakiyesi.
- Tekil indeksler: `orders.idempotency_key` (partial unique) ve `coupon_redemptions (coupon_id, order_id)`.
- Redis yok. Süreç içi önbellekler yalnız acil anahtarlar (≤10 sn) ve oturum dönemi için var; yetki her istekte DB'den okunur.

**Kanıt:** COM04, COM06, COM07, PAY-03 ve PAY-04 geçti.

**Kalan:**

- Kupon `usage_limit` artık kupon başına sayaçla atomik ayrılıyor (COM03 geçti, `8c34c16`).
- Runtime DB kullanıcısının DDL/admin yetkisi olmaması (Kullanıcı-Platform).

## ADR-04 — Inbox/outbox ve dış etki mutabakatı

**Durum:** uygulandı (uyarlanmış).

Broker yok. Eşdeğer kontroller:

- iyzico sonucu her zaman ödeme-detay sorgusuyla doğrulanır. Replay kalkanı ve atomik tek geçiş vardır; commit edilemeyen webhook 500 döner ve iyzico yeniden dener.
- Kaybolan yan etkiyi süpürme işi bir kez gönderir. Zaman aşımında `needs_reconciliation` işaretlenir ve 15 dk'lık mutabakat cron'u devreye girer.
- 3DS başlatma kilidi var.
- İadede belirsiz sonuç (`unknown`) yeni iadeyi kilitler.
- Doğan'a giden belgelerde UUID ve numara kalıcıdır; zaman aşımında mevcut belge aranır.
- Kargo etiketinde atomik kilit var; MNG'de oluşmuş ama yerelde kaybolmuş etiket sipariş numarasıyla bulunup bağlanır.
- Kritik olmayan denetim kayıtları için outbox (`audit_outbox` + sınırlı yerel dosya) ve 5 dk'lık aktarma işi.
- Günlük salt-okunur iyzico mutabakatı.

**Kanıt:** T03, T06, T07, PAY-05…13, COM09, COM14, FIN-E06, CHAIN07 ve FIN-E07 geçti.

**Kalan:** Komisyon/settlement defteri yok (COM12, İ-05).

## ADR-05 — Parser ve converter izolasyonu

**Durum:** ertelendi.

**Gerekçe:** Railway'de tek servis var. Ayrı işçi servisi maliyet ve işletim yükü getiriyor; mevcut girdiler personel kaynaklı (XML feed, ürün görseli).

**Telafi (testli):**

- XXE ve entity reddi.
- IP'ye sabitlenmiş, bayt tavanlı indirme.
- İçerik koklama, SVG reddi, WebP'ye yeniden kodlama.
- JPEG dönüşüm eşzamanlılığı 2 ile sınırlı.
- Yükleme yalnız personel yapabilir.
- XML ayrıştırma ayrı bir iş parçacığında; derinlik, öğe sayısı ve süre bütçesi ayrıştırıcı döngüsünde (T10 geçti).
- Görsel çözme ayrı ve sınırlı bir iş parçacığı havuzunda, görev zaman aşımıyla; piksel sınırı çözmeden önce başlıktan kontrol edilir.

**Kalan:** Ayrı süreç yok; Pillow'da yerel çökme tüm süreci etkileyebilir (ADM-07 kısmen).

**Yeniden değerlendirme:** Müşteri kaynaklı dosya işleme eklenirse ya da ADM-07 bulgusu çıkarsa (İ-07, 2027-01-31).

## ADR-06 — Harici KMS, veri minimizasyonu ve alan şifreleme

**Durum:** kabul edildi. Alan şifreleme uygulandı (`00b1cb1`, `e7cedef`, `0a3ce4f`); harici KMS ertelendi.

- **Şifrelenen alanlar:** TCKN/VKN, iade IBAN'ı, havale dekontları, MFA sırları ve entegrasyon sırları.
- **Algoritma:** Fernet (AES-128-CBC + HMAC-SHA256). AES-256-GCM değildir; uyum belgelerinde öyle yazılmamalıdır.
- **Arama:** alan ayrımlı HMAC kör indeks (`PII_HASH_SECRET`).
- **Anahtar:** `SECRETS_MASTER_KEY` ortam değişkeni. Eski kayıtlar için JWT'den türetilmiş anahtar yalnız çözmede kullanılır.
- **Öz-test:** tüm şifreli değerlerin çözülebildiğini ve kalan düz metin sayısını raporlar.
- **Kart:** saklanmaz (`registerCard=0`). Ancak kart numarası backend üzerinden iyzico'ya iletilir.

**Ertelenen:** Harici KMS/HSM. Gerekçe: Railway'de yerleşik KMS yok; anahtar zaten DB'den ayrı bir güven alanında (Railway Variables).

**Kalan:**

- Toplu çözme alarmı ve PAN/CVC kanarya testi yapıldı (PRIV07, PRIV01 geçti, `8c34c16`).
- iyzico barındırılan formuna geçiş değerlendirmesi (Kullanıcı kararı).
- `SECRETS_MASTER_KEY` rotasyon betiği.

## ADR-07 — DR tutarlı kesit, fencing ve revocation

**Durum:** kabul edildi; kısmen uygulandı.

**Uygulananlar:**

- `restore_mode` anahtarı dış yazmaları durdurur (testli).
- Oturum dönemi (`Tüm Oturumları Sonlandır`) vardır.
- KVKK mezar taşları R2'de tutulur; gece ve elle yeniden uygulanır (testli).
- Zamanlayıcı lider kilidi, yarım kalan arka plan işlerinin çift çalışmasını önler (`test_backfill_resumes_after_leadership_loss`).
- Denetim zinciri R2'ye aktarılır.
- `restore_mode` kapatılınca otomatik kapanış: oturum dönemi, KVKK mezar taşları, pazarlama retleri ve R2 denetim arşivinden personel pasifleştirme/silmeleri yeniden uygulanır (ADM-11, CHAIN12 geçti).
- Öz-test geri yükleme sonrası arşiv referanslarını örnekler (CHAIN10 geçti).
- `restore_mode` açıkken bütün iade uçları kapalı; kapanınca aynı yazımda "geri yükleme sonrası iade kilidi" açılır ve restore penceresi iyzico mutabakatı arka planda çalışır. Kilit yalnız tam ve temiz mutabakatla ya da süper admin gerekçesiyle (fail-closed denetim) kalkar (CHAIN08 geçti, `280b0c20`).
- XML içe aktarma kilidinde fencing belirteci (CHAIN05 geçti).

**Kalan:**

- Snapshot sonrası rol, davet ve IBAN değişiklikleri otomatik yeniden uygulanmaz (el kitabı §4.9 adım 3).
- Atlas restore tatbikatı, RPO/RTO ve kurtarma manifesti (Kullanıcı-Platform; el kitabı §4.9).

## ADR-08 — Egress policy ve endpoint sürümü

**Durum:** uygulandı (`25a134b`, `c5bcf80`).

`security_fetch` şu kuralları uygular:

- Şema, port ve host doğrulaması.
- DNS sonucunun özel, loopback, link-local, metadata ve IPv4-mapped IPv6 adres kontrolü.
- Bağlantının doğrulanan IP'ye sabitlenmesi.
- Her yönlendirmede yeniden kontrol ve yönlendirme sayısı sınırı.
- Proxy ortam değişkenlerinin yok sayılması.
- Bayt tavanı.

**Kanıt:** T11 geçti (`test_infra_ssrf.py`, `test_security_fetch.py`).

**Uygulanamaz kısım:** Endpoint sürümleme. Partner uçları kodda sabittir; kullanıcı tanımlı webhook hedefi yoktur. Panelden değişebilen tek dış hedef XML feed kaynağıdır; kaynak değişince ilk içe aktarma süper admin zorlaması ister (CHAIN11 geçti).

## ADR-09 — Audit/monitoring arızasında iş davranışı

**Durum:** uygulandı (`e7cedef`, `3654ea8`).

- Kritik admin işlemleri önce "intent" denetim kaydı yazar; yazılamazsa işlem 503 ile yapılmaz. Bu işlemler: iade, IBAN, export, rol, acil anahtar, oturum iptali, kurtarma kodu, KVKK, kasa.
- Ödeme callback'leri denetim kesintisinden etkilenmez.
- Kayıtlar hash zinciriyle bağlıdır. Kurcalama, silme ve kuyruk silme tespit edilir ve alarm üretir. Yazılamayan kayıt "bilinen boşluk" olarak işaretlenir, sahte "kurcalama" alarmı vermez.
- Zincir artımlı olarak R2'ye aktarılır.
- Loglar, denetim kaydı ve entegrasyon logları PII ve sır maskelenerek yazılır.
- Uyarı kanalları: SMTP, ardından Resend, ardından mağaza maili. Teslim hatası kaydedilir.

- Kritik olmayan denetim kaydı yazılamazsa outbox'a düşer ve zincire tam bir kez aktarılır.
- Güvenlik hattı kontrolü 5 dk'da bir arşiv gecikmesini, teslim edilemeyen uyarıyı ve zincir boşluğunu alarmlar.

- SOC-01: uyarı/kritik uyarı e-postasında imzalı, süreli, tek kullanımlık "Gördüm" bağlantısı ve panelde "Onayla"; kritik uyarı 15 dk (İşletme Kuralı) onaylanmazsa bir kez "YANIT YOK" yükseltmesi (+ SMS); aylık otomatik alarm tatbikatı (`security_drills`), öz-testte 35 gün kontrolü.

**Kanıt:** ADM-12, T14, SOC-02, SOC-01, CHAIN07 ve FIN-E07 geçti.

**Kalan:** Senaryo tatbikatı ve irtibat listesi (İ-13); alarm tatbikatı kodda ve aylık.

## ADR-10 — Release artifact, rollback ve şema uyumu

**Durum:** kabul edildi; imzalı artefakt ve provenance ertelendi.

**Uygulananlar:**

- GitHub Actions tam commit SHA'sına pinli.
- gitleaks ikilisi SHA-256 ile doğrulanıyor.
- pip-audit ve yarn audit kapısı var (`test_dependency_audit_gate.py`). pip-audit 0 açık (`d3a85c6`). yarn audit kırmızı: frontend `yarn.lock` güncellemesi onay bekliyor. gitleaks kırmızı: geçmişteki 5 gerçek sırrın rotasyonu bekleniyor.
- CodeQL çalışıyor.
- Güvenlik e2e seti her push'ta çalışıyor (`quality.yml`, `1b3a50c`).
- MongoDB şemasız; şema değişiklikleri geriye uyumlu alan eklemeyle yapılıyor (ör. `_enc` alanları, düz alan doğrulama sonrası kaldırılıyor).

**Ertelenen:** İmzalı artefakt, SBOM ve provenance. Gerekçe: Railway ve Cloudflare Pages git'ten derliyor.

**Telafi (Kullanıcı-Platform):**

- Railway'de "Wait for CI".
- GitHub'da `main` için branch protection.
- Cloudflare Pages'te yalnız `main` dalından production build.

**Yeniden değerlendirme:** İ-11 (2026-10-18).
