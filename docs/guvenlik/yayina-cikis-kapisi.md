# Yayına çıkış kapısı — G01…G14 güncel karar

4 Ekim 2026 · Build `8c34c16c` · Ortam: mongomock + ASGI test (CI) ve canlı öz-test · Önceki karar: build `c1f556b` (1 geçti, 10 koşullu, 3 doğrulanmadı)

Sistem zaten canlıda. Bu form "yeniden açılış" kararı vermez. Her kapının bugünkü **kanıt durumunu** ve geçmeyen kapılar için **istisna kaydını** tutar. Kapı kararları:

- **geçti**: zorunlu kanıtların tamamı otomatik testle doğrulanıyor.
- **koşullu**: ana risk testli; kalan boşluk aşağıdaki istisna kaydıyla telafi ediliyor ve süreli.
- **doğrulanmadı**: zorunlu kanıtın önemli kısmı platformda ya da tatbikatta ve henüz yapılmadı.

Kanıt kaynakları:

- Otomatik testler: `backend/tests/security_e2e/` (736 test), `backend/tests/test_route_guards.py` (3), `backend/tests/test_security_fetch.py` (41). Bugünkü sonuç: **780 geçti, 0 kaldı**. Quality Gate'te ayrıca `test_security_hardening.py` (30), `test_activity_audit.py` (4), `test_dependency_audit_gate.py` (12), `test_google_auth_flow.py` (3), `test_sns_security.py` (13) ve `test_stock_audit.py` (5) çalışıyor; hepsi geçiyor.
- Test kayıt kartı: [`guvenlik-test-kayitlari.json`](guvenlik-test-kayitlari.json). Her test kimliğinin durumu ve `dosya::test` kanıtı burada. Özet: 119 geçti, 9 kısmen, 9 kod dışı, 14 uygulanamaz (CHAIN08, CHAIN09 `280b0c20`; ADM-07, ADM-08, IAM-01 `3589333a` ile geçti; `security_e2e` ~3600 test).
- Canlı öz-test: `backend/security/selftest.py`. Açılıştan 5 dk sonra ve her gün 05:13 UTC'de çalışır; 16 kontrol yapar (uyarı e-postası teslimi dahil). Sonuç Güvenlik Paneli'ndeki "Güvenlik öz-testi" kartında görünür ve e-postayla gelir. Elle çalıştırmak için: `POST /api/admin/security/selftest/run` (yalnız süper admin).
- CI durumu (`security.yml`): pip-audit **yeşil** (0 açık). gitleaks **kırmızı**: geçmişteki 5 gerçek sırrın rotasyonu bekleniyor. yarn audit **kırmızı**: frontend `yarn.lock` güncellemesi onay bekliyor.

## Karar tablosu

| Kapı | Zorunlu kanıt → durum | Karar | Kanıt bağlantısı |
|---|---|---|---|
| G01 müşteri/admin sınırı | AUTH01 ✔, AUTH02 ✔, AUTH03 ✔, AUTH18 ✔; route envanteri ✔ | **geçti** | `test_route_guards.py`, `test_identity_sessions.py::test_customer_type_token_rejected_by_admin_api`, `test_identity2_reset_otp.py::test_profile_update_ignores_privilege_and_identity_fields` |
| G02 tenant ve nesne | T01 ✔, T02 ✔, T22 ✔, PAY-01 ✔, IAM-01 ✔; özel dosya ✔ | **geçti** | `test_commerce2_order_access.py`, `test_identity2_perm_matrix.py`, `test_identity_rbac.py`, `test_infra_private_archive.py` |
| G03 OTP ve recovery | AUTH06–11 ✔, AUTH13 ✔, AUTH15 ✔, AUTH16 ✔, AUTH19 ✔, AUTH20 ✔, AUTH23 ✔, AUTH24 ✔ | **geçti** | `test_identity2_reset_otp.py`, `test_identity2_staff_factors.py`, `test_identity2_sms_budget.py`, `test_identity_mfa.py` |
| G04 personel ve onay | ADM-01 ✔, ADM-02 ✔, ADM-03 ✔, T23 ✔; rol matrisi ✔ (IAM-01); tek süper admin istisnası | **koşullu** (İ-04) | `test_identity_approvals.py`, `test_identity2_admin_ops.py`, `test_identity2_staff_factors.py`, `test_infra2_scheduler_jobs.py` |
| G05 ödeme doğrulaması | T03–T07 ✔; PAY-01…13 ✔ | **geçti** | `test_payment_3ds_e2e.py`, `test_payment_webhook_sweep_e2e.py`, `test_payment_edges_e2e.py`, `test_payment_url_parser_confusion.py` |
| G06 para ve sipariş | T08 ✔, COM06 ✔, COM10 ✔, COM11 ✔, COM16 ✔, COM12 ◐ | **koşullu** (İ-05) | `test_commerce2_money_chain.py`, `test_commerce2_chargeback_refunds.py`, `test_commerce2_state_races.py`, `test_infra2_iyzico_reconciliation.py` |
| G07 kart ve sır | T14 ✔, PRIV01 ✔, AUTH23 ✔ | **geçti** | `test_commerce2_privacy.py::test_canary_card_never_persists_or_logs`, `test_infra_log_redaction.py`, `test_identity2_sms_budget.py::test_otp_values_never_logged` |
| G08 XML, dosya, egress | T09 ✔, T10 ✔, T11 ✔, ADM-07 ✔ | **geçti** | `test_infra2_xml_feed_integrity.py`, `test_infra2_image_isolation_load.py`, `test_infra_ssrf.py`, `test_security_fetch.py` |
| G09 stok ve dış belgeler | COM07 ✔, COM09 ✔, COM14 ✔, COM15 ✔ | **geçti** | `test_commerce2_state_races.py`, `test_infra2_xml_feed_integrity.py`, `test_orders_receipts_invoices_e2e.py` |
| G10 veri ve tercih | PRIV02 ✔, PRIV03 ✔, PRIV04 ✔, PRIV05 ✔, ADM-13 ✔ | **geçti** | `test_kvkk_self_service.py`, `test_infra2_marketing_suppression_restore.py`, `test_data_email_marketing.py` |
| G11 yedek ve restore | DR-01 ☐, DR-02 ☐, COM16 ✔, CHAIN12 ✔, CHAIN08 ✔ | **doğrulanmadı** (İ-10) | `test_identity2_admin_ops.py::test_restore_finalize_reapplies_revocations_epoch_and_tombstones`, `test_chain08_restore_refund_hold.py`, `test_infra2_selftest_archive_refs.py`, `test_infra_kill_switches.py` |
| G12 deploy ve audit | Audit: ADM-12 ✔, SOC-02 ✔, SOC-01 ◐. Deploy: SDLC-01 ☐, ADM-09 ☐, SDLC-02 ◐ | **doğrulanmadı** (İ-11, İ-13) | `test_infra2_audit_outbox.py`, `test_identity2_admin_ops.py::test_pipeline_check_ok_then_detects_export_lag_delivery_failure_and_gap`, `.github/workflows/*.yml` |
| G13 kapasite ve kesinti | CHAIN13 ✔, T18 ◐, SHOP02 ◐; CHAIN14 uygulanamaz | **doğrulanmadı** (İ-12) | `test_infra2_image_isolation_load.py` (CI, cömert bütçe), `backend/tests/load/checkout_latency.py` (hazırlık ortamı aracı, henüz çalıştırılmadı) |
| G14 operasyon sahipliği | Runbook ✔ ([el kitabı](olay-mudahale-el-kitabi.md)); acil anahtarlar testli ✔; uyarı teslimi canlıda ✔; güvenlik hattı izleme ✔; tatbikat ☐; irtibat listesi ☐ | **koşullu** (İ-13) | `test_infra_kill_switches.py`, `test_selftest_e2e.py`, öz-test "Uyarı e-postası teslimi" |

✔ geçti · ◐ kısmen · ☐ kod dışı veya yapılmadı

Özet: **6 geçti** (G01, G03, G05, G07, G09, G10), **5 koşullu** (G02, G04, G06, G08, G14), **3 doğrulanmadı** (G11, G12, G13).

## İstisna kaydı

Her istisnada telafi kontrolü mevcut. Bitiş tarihinde yeniden test şartı karşılanmamışsa istisna yenilenmez. İstisna, KVKK veya iyzico sözleşme yükümlülüğünü ortadan kaldırmaz. Risk sahibi işletme sahibidir (süper admin). "Kodlama" sahipli maddeler depoda yapılır.

### Açık istisnalar

| No | Kapı | Risk | Etkilenen işlev/veri | Telafi kontrolü | Sahip | Bitiş | Yeniden test şartı |
|---|---|---|---|---|---|---|---|
| İ-02 | G02, G04 | Kritik olmayan admin uçlarında "yanlış izinle 403" dinamik olarak doğrulanmıyor; alan düzeyinde tam rol × alan matrisi yok (IAM-01) | Panel rolleri | 29 kritik uç için üretilmiş matris (izinsiz 403 + yan etki yok, izinli + denetim, fail-closed); her /admin ve yazma rotasında yetki bağımlılığı statik testli; çapraz müşteri erişimi tüm sipariş uçlarında testli | Kodlama | 2026-12-31 | Rota envanterinden üretilen, tüm admin uçlarını kapsayan dinamik matris testi geçer |
| İ-04 | G04 | Tek aktif süper admin varken başka personelin şifre/MFA telefonu değişimi ve IBAN değişimi ikinci onaysız uygulanır (iki kişi kuralı uygulanamaz) | Panel hesapları, IBAN | Kritik uyarı tüm alıcılara gider; `single_admin_exception` denetim kaydı; hedefin oturumları ve bekleyen kodları düşer; kurtarma girişinden sonra 24 sa yasak | Kullanıcı-Platform | 2026-10-31 | En az iki aktif, MFA'lı süper admin tanımlı; öz-test "Personel / süper admin" ≥2 gösterir; onay akışı canlıda bir kez denenmiş |
| İ-05 | G06 | Komisyon/settlement defteri yok; iyzico hesap özetiyle mutabakat elle (COM12) | Muhasebe, iade, itiraz | Günlük salt-okunur iyzico mutabakatı (tahsilat, tutar, paymentId, iade toplamı) uyuşmazlıkta kritik uyarı; iade tahsilatla sınırlı; açık itirazda iade kilidi; `freeze_refunds`; aylık elle mutabakat (el kitabı §5) | Kullanıcı-Platform (+ istenirse Kodlama: settlement raporu içe aktarma) | 2026-12-31 | Aylık mutabakat kaydı 2 ay boyunca tutulmuş; ya da settlement içe aktarma testi geçer |
| İ-07 | G08 | Görsel dönüştürme aynı süreçte; Pillow'da yerel çökme tüm backend'i etkileyebilir (ADM-07) | Upload, anlık görsel boyutlandırma | Başlıktan piksel ön kontrolü (bomba çözülmez); sınırlı havuz + görev zaman aşımı; içerik koklama; yalnız personel yükler; yük altında checkout testli; çöken süreç platformun yeniden başlatma politikasıyla ayağa kalkar | Kodlama | 2027-01-31 | Dönüştürme ayrı süreç/işçide (ADR-05) ve çökme testi geçer |
| İ-10 | G11 | Restore tatbikatı yapılmadı; RPO/RTO bilinmiyor | Tüm veri | `restore_mode` kapanışında oturum dönemi, KVKK silmeleri, pazarlama retleri ve personel iptali otomatik (testli); `restore_mode` açıkken bütün iade uçları kapalı, kapanınca restore penceresi iyzico mutabakatı temiz bitene kadar iade kilidi (CHAIN08 geçti, `280b0c20`); öz-test arşiv referanslarını denetler | Kullanıcı-Platform | 2026-11-15 | T16/DR-01 tatbikatı yapılmış, süreler kaydedilmiş; tatbikatta iade kilidinin kalkışı (`refund_hold_cleared`) ya da gerekçeli elle kaldırma kaydedilmiş |
| İ-11 | G12 | Railway CI beklemeden deploy edebilir; artefakt imzası yok. gitleaks kırmızı (geçmişte 5 gerçek sır, rotasyon bekliyor); yarn audit kırmızı (frontend `yarn.lock` güncellemesi onay bekliyor) | Backend ve frontend dağıtımı, sağlayıcı anahtarları | CI her push'ta e2e, gitleaks, pip-audit (0 açık) ve CodeQL çalıştırıyor; action'lar SHA'ya pinli; deploy yalnız `main`'den; sırlar HEAD'de yok, yalnız geçmişte | Kullanıcı-Platform | 2026-10-18 | Railway "Wait for CI" açık, branch protection var; 5 sır döndürülmüş ve gitleaks yeşil; yarn.lock güncellenmiş ve yarn audit yeşil |
| İ-12 | G13 | Gerçek altyapıda kapasite ölçülmedi; pazaryeri senkron yükünün checkout'a etkisi bilinmiyor (T18, SHOP02) | Checkout | CI'da 20 eşzamanlı pahalı istek altında checkout testi; sınırlı görsel havuzu; IP ve genel hız sınırları; zamanlayıcı lider kilidi; `pause_marketplace_sync` ve `close_checkout` anahtarları | Kullanıcı-Platform (hazırlık ortamı) + Kodlama (ölçüm) | 2027-01-31 | `checkout_latency.py` hazırlık ortamında çalıştırılmış; p95 checkout süresi raporu hazır |
| İ-13 | G12, G14 | Tatbikat yapılmadı; 15 dk insan teyidi ölçülmedi (SOC-01); PSP, hukuk ve muhasebe irtibat listesi yok | Olay müdahalesi | El kitabı ve ilk 15 dk listesi; acil anahtarlar testli; uyarı teslimi günlük öz-testle ve 5 dk'lık güvenlik hattı kontrolüyle doğrulanıyor | Kullanıcı-Platform | 2026-11-15 | Bir masa başı tatbikat yapılmış ve tespit → insan teyidi süresi kaydedilmiş; irtibat listesi doldurulmuş (el kitabı §0) |

### Kapı dışı kabul edilen kalıntılar

Bu kayıtlar hiçbir kapının zorunlu kanıtı değil ama test kartında "kısmen" duruyor. Risk kabulü ya da iş sahibi burada izlenir.

| No | Test | Kalıntı | Telafi kontrolü | Sahip | Bitiş | Kapanış şartı |
|---|---|---|---|---|---|---|
| İ-14 | AUTH14 | Kayıt ucu "bu e-posta / telefon zaten kayıtlı" diyerek hesap varlığını açıklıyor | IP başına 5/dk sınırı; giriş ve şifre sıfırlama yanıtları eşit (testli); hesap kilidi ve IP engeli | Kullanıcı-Platform (risk kabulü) | 2026-12-31 | Risk kabulü yazılı; ya da kayıt genel yanıt + e-posta doğrulama linkine çevrilmiş ve testli (Kodlama) |
| İ-15 | T19, ADM-08 | CSP Report-Only ve `unsafe-inline`; zararlı sürüm karantinası yok; yayınlanan JS paketinin özeti izlenmiyor | Yalnız süper admin yazar; her değişiklik ve geri dönüş uyarı; script host manifesti ve yeni host uyarısı; öz-test DB farkını yakalar; CSP ihlal raporları toplanıyor | Kodlama + Kullanıcı-Platform (zorlama kararı) | 2026-12-31 | CSP zorlayıcı modda (nonce/hash) ve testli; sürüm karantinası testli |
| İ-16 | CHAIN09 | **Kapandı (`280b0c20`).** Süren dışa aktarım yanıtı personel pasifleştirilince / oturum iptal edilince / `freeze_exports` açılınca ~5 sn içinde kesiliyor | Akış parçalar arasında personel, oturum ve anahtar durumunu yeniden denetliyor; kesme kritik uyarı üretiyor (`test_chain09_export_stream_revocation.py`) | Kodlama | — | Kapandı |
| İ-17 | SHOP12 | Dar ekran/klavye ve ağ kesintisi davranışı için tarayıcı tabanlı arayüz testi yok | Sunucu tarafı idempotency testli (çift sipariş yok); checkout değişikliklerinde ekran görüntüsüyle elle kontrol | Kodlama (+ test paketi kurulumu için Kullanıcı onayı) | 2027-01-31 | Checkout için arayüz testi CI'da |
| İ-18 | ADM-15 | Sunucuya bir daha bağlanmayan (çevrimdışı/kayıp) personel cihazındaki veri kodla silinemez; WebView'in işletim sistemi önbelleği ve gerçek cihazda uçtan uca test yok; bu sürümden önceki X-App-* göndermeyen derlemeler sürüm kapısına takılmaz | Oturum iptalinde 401 + `X-Auth-Code: session_revoked` ve uygulama `wipeLocalStaffData()` ile token/localStorage/sessionStorage/Cache Storage/IndexedDB/Capacitor Preferences'ı siler (testli); asgari sürüm İşletme Kuralı → 426 + güncelleme ekranı (testli); push'larda kişisel veri yok (testli); personel pasifleştirilince token anında geçersiz | Kullanıcı-Platform (cihaz politikası / MDM) | 2027-01-31 | Yazılı cihaz politikası (MDM uzaktan silme, ayrılışta cihaz iadesi) ve bir iOS + bir Android cihazda pasifleştirme → silme testi kaydı |

### Kapatılan istisnalar (build `8c34c16c`)

| No | Kapı | Kapanış kanıtı |
|---|---|---|
| İ-01 | G01 | AUTH02 geçti: `test_identity2_reset_otp.py::test_profile_update_ignores_privilege_and_identity_fields` |
| İ-03 | G03 | AUTH06/10/11/13/15/19 geçti (`test_identity2_reset_otp.py`); küresel SMS tavanı (`test_identity2_sms_budget.py`). SMS sağlayıcı panel limiti artık ikinci savunma hattı olarak README §4'te öneri |
| İ-06 | G07 | PRIV01 kanarya kart testi geçti: `test_commerce2_privacy.py::test_canary_card_never_persists_or_logs`; OTP log testi (AUTH23). Barındırılan form kararı BL-015'te mimari seçenek olarak duruyor |
| İ-08 | G09 | COM09 (`test_commerce2_state_races.py`) ve COM15 (`test_infra2_xml_feed_integrity.py`) geçti |
| İ-09 | G10 | ADM-13 ve PRIV04 geçti: `test_infra2_marketing_suppression_restore.py` |

İ-02 (IAM-01 tüm-uygulama matrisi) ve İ-07 (ADM-07 ayrı süreç) `3589333a` ile kapandı; tablodaki satırlar tarihçe için duruyor. İ-04 daraltıldı: iki kişi kuralı kodda var ve testli; yalnız tek süper admin durumu kaldı. İ-02, İ-05, İ-07, İ-10, İ-11, İ-12 ve İ-13 güncel kalan kısma göre yeniden yazıldı.

## Açılış sonrası izleme

Her deploy sonrası şunlar kontrol edilir:

- **Smoke:** `scratchpad/smoke.sh` çalıştırılır (CLAUDE.md).
- **Öz-test:** Güvenlik Paneli öz-test kartında "0 hata" görülür. Deploy'dan 5 dk sonra kendiliğinden çalışır.
- **Ödeme:** Kart siparişi `awaiting_payment` başlıyor mu? Ödeme Kaydı Bulunmayan ekranında yeni birikme var mı? Günlük iyzico mutabakatı uyarı verdi mi?
- **Uyarılar:** Gelen kutusunda `break_glass_active`, `audit_chain_broken`, `single_admin_exception`, `security_pipeline_degraded`, `sms_budget_exceeded`, `pii_bulk_reveal`, `storefront_script_changed` / `script_host_new` ya da entegrasyon tazelik uyarısı var mı?

Fark görülürse ilgili acil anahtar açılır (el kitabı). DB'yi geri almak iyzico, Doğan, kargo ve pazaryeri tarafındaki dış etkileri geri almaz.
