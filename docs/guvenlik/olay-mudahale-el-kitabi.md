# Olay müdahale el kitabı — Facette'e uyarlanmış

4 Ekim 2026 · Build `8c34c16c` · Kaynak: güvenlik paketi `olay-mudahale-el-kitabi.md` (RB-01…08, RUN-F01…F07)

Bu el kitabı paketteki genel prosedürleri bu sistemin gerçek parçalarıyla eşler:

- **Altyapı:** Railway backend, Cloudflare Pages vitrin/panel, MongoDB Atlas, Cloudflare R2.
- **Dış servisler:** iyzico (tek ödeme kuruluşu), Doğan e-Arşiv/e-Fatura, MNG ve DHL kargo, Trendyol, Hepsiburada, Amazon, İYS. E-posta için Resend ve mağaza işlem maili.

VDS, PayTR, çoklu mağaza (tenant) ve satıcı ödemesi (payout) bu sistemde yok; ilgili adımlar çıkarıldı.

> Altın kurallar
>
> 1. **DB'yi geri almak dış etkileri geri almaz.** iyzico tahsilatı ve iadesi, Doğan faturası, kargo etiketi, pazaryeri gönderimi ve gönderilmiş e-posta veritabanı geri alınınca geri dönmez.
> 2. **Bir siparişi elle "ödendi" yapma.** Kart siparişinde tek yetkili yol iyzico doğrulamasıdır (CLAUDE.md değişmez 1). Havale/EFT onayı yalnız banka ekstresinde görülen para için verilir.
> 3. **`JWT_SECRET`'i değiştirme.** Bkz. §4.10. Normal koşulda asla değişmez; sızıntıda önce kodlama desteği gerekir.
> 4. **Kanıtta sır ve gereksiz kişisel veri tutma.** Token, şifre, kart numarası, CVC, tam TCKN ya da IBAN kanıt notuna yazılmaz. Sipariş numarası, kayıt kimliği ve zaman yeterlidir.

---

## 0. Roller ve irtibat (doldurulacak)

Küçük ekipte bir kişi birden çok rolü üstlenebilir. Ancak para çıkışını yapan kişi, onu onaylayan ikinci kişinin yerine geçemez.

| Rol | Kim | Yedek | Kanal (panelden bağımsız) |
|---|---|---|---|
| Olay komutanı (karar, kayıt) | İşletme sahibi (süper admin) | İkinci süper admin | Telefon |
| Teknik müdahale | Kodlama (Claude Code oturumu) | — | — |
| Finans / mutabakat | Muhasebe | — | — |
| Hukuk / KVKK irtibat kişisi | — | — | — |
| iyzico destek / risk | iyzico Merchant Panel → Destek | — | — |
| Doğan e-Fatura destek | Doğan portal | — | — |
| MNG / DHL kurumsal destek | — | — | — |
| Trendyol / HB / Amazon satıcı destek | — | — | — |
| Railway / Atlas / Cloudflare | Konsol destek talebi | — | — |

Bildirim süreleri hukuk irtibatıyla teyit edilir:

- **KVK Kurulu:** ihlali öğrenmeden itibaren 72 saat (Kurul kararı 2019/10).
- **Amazon:** bilgilerini ilgilendiren olaylarda 24 saat (`docs/INCIDENT_RESPONSE_PLAN.md`).

---

## 1. İLK 15 DAKİKA — tek sayfa

Olay türü ne olursa olsun bu sırayla ilerle. Her adımı saatiyle not et (§6 kayıt formu).

**0–3 dk · Gör ve kaydet**

- [ ] Olay kimliği ver (ör. `OLAY-2026-10-04-01`), başlangıç saatini yaz, olay komutanını belirle.
- [ ] İlk sinyali kaydet: uyarı e-postasının konusu, ekran ya da müşteri bildirimi. Ekran görüntüsünde kişisel veri varsa maskele.
- [ ] Panelden bağımsız bir iletişim kanalı aç (telefon). Ele geçirilmiş olabilecek panel ve posta kutusuna güvenme.

**3–8 dk · Durdur (en küçük yeterli anahtar)**

Panel → Entegrasyonlar → **Acil Anahtarlar** (`/admin/acil-anahtarlar`, yalnız süper admin, gerekçe zorunlu). Anahtar 10 sn içinde tüm sunucularda etkin olur.

| Durum | Aç |
|---|---|
| Şüpheli iade / para çıkışı | `freeze_refunds` |
| Veri sızıntısı veya şüpheli toplu indirme | `freeze_exports` |
| Pazaryeri stok/fiyat/sipariş bozulması | `pause_marketplace_sync` |
| Ödeme sayfası veya script güvenilmez; sahte sipariş dalgası | `close_checkout` (başlamış ödemelerin sonuçlanması sürer) |
| Yedekten dönüş yapılacak ya da yapıldı | `restore_mode` (e-fatura, pazaryeri ve kargo dış yazmaları **ve bütün iade uçları** durur; kapatılınca iyzico mutabakatı temiz bitene kadar iadeler otomatik kilitli kalır) |
| Kart ödemesi tek başına durmalı | Panel → **Ödeme Tipleri** (`/admin/odeme-tipleri`) → Kredi Kartı pasif. Ya da iyzico ayarında aktif=false. Callback ve mutabakat sürer |

**8–12 dk · Kimlikleri kes**

- [ ] Tek personel şüpheliyse: Ayarlar → Kullanıcılar & Roller → personeli **pasifleştir** (`PUT /api/admin/users/{id}` `{"is_active": false}`). Tokenları bir sonraki istekte reddedilir.
- [ ] Kapsam belirsizse veya bir süper admin şüpheliyse: Güvenlik Paneli → **Tüm Oturumları Sonlandır** (`POST /api/admin/security/revoke-all-sessions`). Herkes yeniden giriş yapar; size yeni token döner.
- [ ] Saldırgan IP'si belliyse: Güvenlik Paneli → IP engelle (`POST /api/admin/security/ip-blocklist` `{ip, hours, reason}`). Yoğun saldırıda Cloudflare WAF kuralı da ekle.

**12–15 dk · Kanıtı koru ve haber ver**

- [ ] Güvenlik Paneli → Denetim zinciri → **Doğrula**, sonra **Arşive aktar** (`POST /api/admin/security/audit-chain/verify` ve `/export`). Kayıtlar R2 gizli arşivine kopyalanır.
- [ ] Kullanıcı İşlem Geçmişi (`/admin/islem-gecmisi`) ve Entegrasyon Logları'ndan ilgili zaman aralığını not et. Tam dökümü kanala yapıştırma.
- [ ] Uyarı kanalını doğrula: Güvenlik Paneli → **Test uyarısı** (`POST /api/admin/security/alerts/test`). Alıcı başına teslim sonucu döner.
- [ ] Senaryoya git (§4). Kişisel veri olasılığı varsa hukuk irtibatına saat yazarak haber ver; 72 saat sayacı başlar.

**Yapma:** sunucuyu rastgele yeniden başlatma, logları ya da kayıtları silme, `ADMIN_MFA_ENFORCE=off` gibi break-glass değişkenlerini "işi hızlandırmak" için açma, siparişi elle "ödendi" yapma.

---

## 2. Araç kutusu

### 2.1 Acil anahtarlar (`backend/security_switches.py`)

Yönetim uçları: `GET /api/admin/security-switches` ve `POST /api/admin/security-switches/{ad}` (body `{"on": true|false, "reason": "…"}`). Yalnız süper admin kullanabilir. Açmak için en az 5 karakter gerekçe zorunludur. Her değişiklik önce denetim kaydına yazılır; kayıt yazılamazsa anahtar değişmez (503). Değişiklik kritik uyarı e-postası üretir. Geçmiş, ekranda son 30 kayıtla görünür.

| Anahtar | Durdurur | Durdurmaz |
|---|---|---|
| `freeze_refunds` | İade ödemesi işaretleme, iyzico kart iadesi, toplu iade, siparişi/iadeyi elle "iade ödendi / kısmi iade / ödemeli iptal" yapma (tekli, toplu, sessiz düzeltme), Temu iade komutu (423) | İade talebi alma, sipariş, belirsiz iadenin elle sonuçlandırılması (`/iyzico/refund/resolve`) |
| `freeze_exports` | Tüm toplu Excel/CSV dışa aktarımları (423) | Tekil ekran görüntüleme |
| `pause_marketplace_sync` | Pazaryeri stok/fiyat/sipariş senkronları, fatura linki gönderimi | Vitrin satışı |
| `restore_mode` | Doğan e-fatura, pazaryeri fatura linki, kargo etiketi ve tüm dış yazmalar; **`freeze_refunds`'ın durdurduğu bütün iade uçları** (aynı kapı, 423: "Veritabanı geri yükleme sonrası mutabakat tamamlanana kadar iade yapılamaz") | Vitrin okuma, ödeme sonuçlandırma |
| `close_checkout` | Yeni sipariş (503, müşteriye bakım mesajı) | 3DS callback, mutabakat cron'u, 3 saatlik ödenmemiş sipariş süpürmesi |

**`restore_mode` kapatılınca (açık → kapalı) otomatik geri yükleme kapanışı çalışır** (`security_switches.finalize_restore`):

1. Oturum dönemi artar; herkes yeniden giriş yapar. Anahtarı kapatan süper admine taze token döner, paneli açık kalır.
2. KVKK mezar taşları gizli arşivden yeniden uygulanır.
3. Pazarlama retleri (abonelik iptali, bounce, şikâyet, İYS RET) gece arşivinden yeniden uygulanır. Müşteri retten sonra yeniden onay verdiyse son seçimi korunur.
4. Personel iptalleri yeniden uygulanır: R2 denetim arşivinde, geri yüklenen DB'nin son denetim kaydından sonra tamamlanmış pasifleştirme/silme işlemleri bulunur. DB'de hâlâ aktif olan bu personel pasifleştirilir. Sonradan yeniden aktifleştirilmiş kişiye dokunulmaz.
5. **Geri yükleme sonrası iade kilidi (CHAIN08):** `restore_mode`'u kapatan yazımla AYNI anda kilit açılır; iadeler 423 vermeye devam eder. Restore penceresini kapsayan iyzico mutabakatı yanıt döndükten sonra arka planda otomatik başlar. Pencere: geri yüklenen DB'nin son denetim kaydı (yedek anı) ile `restore_mode`'un açıldığı anın erkeninden 2 gün pay + 30 gün iade geri bakışı (en az 7 gün). Mutabakat tam ve temizse (hata, uyuşmazlık, doğrulanamayan sipariş yok; iyzico her siparişte iade bilgisini döndürdü; sonuç kesilmedi) kilit kalkar ve `refund_hold_cleared` uyarısı gider. Aksi halde kilit kalır, kritik `post_restore_reconciliation_failed` uyarısı gider. Durum: `GET /api/admin/security-switches` → `refund_hold` (`status`, `last_problems`).

Her adım denetime yazılır ve kritik `restore_finalized` uyarısı gider. Ekrandaki bildirimde yeniden pasifleştirilen personel sayısı görünür. Bir adım hata verirse diğerleri yine çalışır; hata adı uyarıda yazar. `restore_mode`'u açmak ya da başka anahtarları değiştirmek kapanışı tetiklemez.

**Kurtarma koduyla giriş kısıtı:** Personel MFA kurtarma koduyla girdiyse 24 saat boyunca acil anahtarları değiştiremez, toplu dışa aktarım yapamaz, onay veremez, IBAN, rol ve yetki değiştiremez, kasa açamaz ve MFA kapatamaz (403, `recovery_restricted_action` kaydı). Kurtarma girişinden sonra yeni bir faktör (Authenticator ya da SMS telefonu) bağlarsa kısıt kalkar. Olay sırasında anahtarı kurtarma koduyla girmiş bir yönetici açamıyorsa önce yeni faktör bağlamalı ya da başka bir süper admin açmalıdır.

### 2.2 Diğer kollar

| Kol | Nerede | Not |
|---|---|---|
| Kart ödemesini kapat | Ödeme Tipleri → Kredi Kartı pasif / iyzico aktif=false | Yeni 3DS başlatma reddedilir; başlamış ödeme sonuçlanır (testli) |
| Bakım modu | Genel Ayarlar → bakım | Müşteriye 503; panel çalışır |
| Küresel oturum iptali | Güvenlik Paneli → Tüm Oturumları Sonlandır | Denetim yazılamazsa çalışmaz (fail-closed) |
| Kendi oturumlarını kapat | `POST /api/auth/logout-all` | Müşteri ve personel |
| Personel pasifleştir / rol değiştir | Kullanıcılar & Roller | Süper admini pasifleştirmek ikinci süper admin onayı ister |
| Onay kuyruğu | Kullanıcılar & Roller → Onay Bekleyen İşlemler (`GET /api/admin/approvals`) | Şüpheli talebi **reddet** (`POST /api/admin/approvals/{id}/reject`) |
| Hesap kilidi aç / kilitleri temizle | Güvenlik Paneli (`/unlock-user`, `/locks/clear`) | Yalnız kimliği doğrulanmış meşru kullanıcı için |
| Sır kasası | Secrets Vault (`/admin/secrets-vault`) | Açık değer görüntüleme kritik uyarı üretir |
| Öz-test | Güvenlik Paneli → öz-test kartı → Çalıştır (`POST /api/admin/security/selftest/run`) | 19 kontrol: arşiv, denetim zinciri, şifreli sırlar, ortam değişkenleri, anahtarlar, oturum dönemi, KVKK arşivi, veri koruma, personel, uyarı teslimi, güvenlik hattı, arşiv referansları, script host manifesti, denetim outbox'ı, pazarlama ret arşivi, entegrasyon tazeliği, vitrin bütünlüğü, görsel dönüştürme izolasyonu, iyzico mutabakat defteri (son başarılı çalışma < 36 sa) |
| Güvenlik hattı kontrolü | Otomatik, 5 dk'da bir | Denetim arşivi 30 dk'dan fazla gecikirse, son 60 dk'da teslim edilemeyen uyarı varsa ya da zincirde yeni boşluk oluşursa kritik `security_pipeline_degraded` |
| KVKK mezar taşları | `POST /api/admin/kvkk/reapply-tombstones` | Her gece 03:47 UTC ve `restore_mode` kapanışında otomatik; elle de çalışır |
| Belirsiz iade çözümü | `POST /api/integrations/iyzico/refund/resolve` `{order_id, claim_id, outcome: "done"\|"failed"}` | Yalnız iyzico panelinde gerçek sonuç görüldükten sonra |
| Açık itirazda iade istisnası | İade isteğine `chargeback_override_reason` (en az 10 karakter) | Açık itirazlı siparişte bütün iade yolları 409 döner. Yalnız süper admin gerekçe yazarak geçebilir; istisna önce denetim zincirine yazılır (yazılamazsa iade yapılmaz) |
| Günlük iyzico mutabakatı | Otomatik, 04:23 UTC (07:23 TR), son 3 gün | Salt-okunur. Uyuşmazlık Entegrasyon Logları'nda (`service=iyzico`, `event=reconciliation_mismatch`) ve kritik uyarıda. Günlük iş geri yükleme sonrası iade kilidini KALDIRMAZ; restore penceresi mutabakatı `restore_mode` kapanışında otomatik çalışır |
| iyzico mutabakat defteri (komisyon / hesaba geçiş) | Otomatik, 05:41 UTC (08:41 TR), önceki 2 TR günü. Süper admin: `GET /api/admin/payment-ledger/summary[?month=YYYY-MM]`, `GET /api/admin/payment-ledger/discrepancies`, `GET /api/admin/payment-ledger/export.csv?month=YYYY-MM`, `POST /api/admin/payment-ledger/rerun` `{date_from, date_to}` (en çok 31 gün, denetimli) | Salt-okunur; siparişe/ödeme durumuna yazmaz. iyzico raporundan brüt, komisyon, net hesaba geçen, beklenen ödeme tarihi; aylık özet `payment_ledger_months`. Uyuşmazlık (`payment_ledger_discrepancies`) başına tek uyarı (`payment_ledger_discrepancy`). Komisyon üst sınırı ve hesaba geçiş toleransı: İşletme Kuralları → Ödeme & İndirim |
| Geri yükleme sonrası iade kilidi — yeniden dene | `POST /api/admin/security-switches/refund-hold/reconcile` (yalnız süper admin; istenirse `{"days": N}` ile pencere büyütülür) | Mutabakatı arka planda yeniden başlatır; temiz biterse kilit kalkar. Para hareketi yok |
| Geri yükleme sonrası iade kilidi — elle kaldır | `POST /api/admin/security-switches/refund-hold/release` `{"reason": "…"}` (yalnız süper admin, en az 10 karakter) | Yalnız iyzico panelinde restore penceresindeki iadeler tek tek kontrol edildikten sonra. Önce denetim zincirine yazılır (yazılamazsa 503, kilit kalkmaz); kritik `refund_hold_released` uyarısı. `restore_mode` açıkken kaldırılamaz (409) |
| Entegrasyon tazelik alarmı | Otomatik, 15 dk'da bir | Pazaryeri sipariş çekme / stok gönderimi beklenen aralığın 5 katı (en az 30 dk) başarısızsa "Entegrasyon durdu" uyarısı; düzelince "yeniden çalışıyor". Dış senkron duraklatılmışsa sessiz. Ayar: `settings` belgesi `integration_freshness` (çarpan, asgari dakika, kapalı kaynaklar, XML aralığı; panel ekranı yok, Kodlama) |
| Toplu kişisel veri açma engeli | Otomatik | Personel bir saatte eşikten fazla (varsayılan 50) tam IBAN / TCKN / VKN / kasa değeri açarsa kritik `pii_bulk_reveal` uyarısı ve 1 saat açık görüntüleme engeli (429). Sayaç deposu yoksa açık değer verilmez (503) |
| Kupon hakkı sayaçları | `coupon_usage_counters` (kupon başına `reserved`/`used`/`total`), `coupon_usage_holds` (sipariş başına) | Kullanım limitli kuponda hak sipariş oluşturulurken atomik ayrılır. İptal, 3 saatlik süpürme ve sipariş açılamaması hakkı geri verir. Redemption kartta yine ödeme onayından sonra yazılır. Sayaçları elle düzeltme; kullanıcının kupon ayarları korunur |
| Denetim outbox'ı | Otomatik aktarma 5 dk'da bir | Kritik olmayan denetim kaydı yazılamazsa `audit_outbox` koleksiyonuna, Mongo da yoksa yerel dosyaya (20 MB tavan) yazılır ve sonra zincire aktarılır. Kritik işlemler yine fail-closed |
| CSP ihlal raporları | `GET /api/admin/custom-theme/csp-reports` (yalnız süper admin) | Vitrinin zorlayıcı ve sıkı Report-Only CSP raporları `POST /api/custom-theme/csp-report` ucuna toplu sayaç olarak gelir (`disposition`: `enforce` = tarayıcı ENGELLEDİ, `report` = yalnız ölçüm) (IP, tarayıcı ve sorgu dizesi tutulmaz). Manifest dışı script host'u ilk görüldüğünde uyarı |
| Ödeme izi | Siparişler → Ödeme İzi (Numaradan) / Ödeme Kaydı Bulunmayan | Mutabakatın giriş noktası |

### 2.3 Break-glass ortam değişkenleri (Railway → Variables)

Yalnız kilitlenme durumunda ve kayıtlı gerekçeyle açılır. Açıkken her açılışta **kritik `break_glass_active` uyarısı** gider. İş biter bitmez silinir.

| Değişken | Etki |
|---|---|
| `ADMIN_MFA_ENFORCE=off` | Admin MFA zorunluluğu ve günlük MFA hata tavanı kalkar |
| `ADMIN_MFA_CHANNEL_POLICY=off` | MFA kanal ayrımı kalkar (telefonlu admine e-posta kodu vb.) |
| `TWO_PERSON_RULE=off` | İki kişi onayı atlanır (IBAN, süper admin daveti, başka personelin şifre/MFA telefonu değişimi); her işlem "single_admin_exception" uyarısı üretir |
| `ALLOW_DIRECT_STAFF_CREATE=1` | Davet yerine doğrudan personel oluşturma |
| `STAFF_IDLE_TIMEOUT=off` | Personel boşta zaman aşımı kapanır |
| `IBAN_COOLING_OFF_MINUTES` | IBAN soğuma süresi (varsayılan 60 dk). Olay sırasında **kısaltılmaz** |

---

## 3. Ortak kurallar

- **Kanıt:** olay zamanı ile toplama zamanını ayrı yaz; toplayanı, kaynağı ve dosya özetini (sha256) kaydet. Denetim zinciri R2'ye aktarıldıktan sonra kanıt değiştirilemez kopyadır.
- **Callback'i kabul etmek ile iş etkisi farklıdır.** iyzico callback ve webhook'u `close_checkout` açıkken de işlenir ve doğrulanmış ödeme kaydedilir. Bu doğrudur: para çekilmişse sipariş kaybolmamalı. Webhook kalıcı yazım yapamazsa 500 döner ve iyzico tekrar dener.
- **Restore sonrası** önce `restore_mode` ve `close_checkout` açık olur (`restore_mode` iadeleri de kapatır); mutabakat (§5) bitmeden kapatılmaz. `restore_mode` kapatılınca otomatik kapanış çalışır ve iyzico mutabakatı temiz bitene kadar iadeler kilitli kalır (§2.1).
- **Rotasyon** temiz bir cihazdan ve ilgili sağlayıcının kendi panelinden yapılır. Yeni sır yalnız Railway Variables'a girilir; sohbet, e-posta ya da koda yazılmaz.
- **Müşteri iletişimi:** "işleminiz kontrol ediliyor" denir. Tahsilatın kesin olup olmadığı iyzico'dan doğrulanmadan "ödemeniz alınmadı" denmez.

---

## 4. Senaryolar

### 4.1 Admin/personel hesabının ele geçirilmesi (paket RB-02)

**Tetikleyici uyarılar:**

- `staff_new_device`: yönetici girişi yeni IP veya cihazdan.
- `role_grant_star`: '*' rolü verildi.
- `mfa_reset_by_admin`: bir yönetici başka bir personelin MFA ayarını değiştirdi. İkinci süper admin varsa bu değişiklik önce onay kuyruğuna düşer (`approval_requested`).
- `mfa_recovery_used`: kurtarma kodu kullanıldı. Hesap 24 saat yüksek riskli işlem yapamaz; denemeler `recovery_restricted_action` olarak güvenlik günlüğüne yazılır.
- `vault_reveal`: kasa (vault) gizli değeri görüntülendi.
- `pii_bulk_reveal`: personel kısa sürede çok sayıda tam IBAN/TCKN/VKN/kasa değeri açtı; açık görüntülemesi 1 saat engellendi.
- `bank_account_change` / `approval_requested`: IBAN değişikliği.
- `single_admin_exception`, `break_glass_active`.
- Beklenmeyen toplu dışa aktarım uyarıları (`member_export`, `orders_export` …).

**Adımlar:**

1. Şüpheli hesabı **pasifleştir**. Hedef bir süper adminse ve ikinci süper admin yoksa **Tüm Oturumları Sonlandır**'ı kullan.
2. **Onay kuyruğunu** aç ve şüpheli hesabın açtığı bekleyen talepleri reddet (IBAN, rol, davet, MFA kapatma, başka personelin şifre ya da MFA telefonu değişimi).
3. Ayarlar → Kullanıcılar & Roller: son 7 günde açılan davetleri (`/admin/users/invites`) iptal et. '*' içeren rolleri ve yeni personeli gözden geçir. Meşru personeli körlemesine silme; önce pasifleştir.
4. Banka hesapları: soğuma kuyruğunda bekleyen IBAN varsa (`bank_accounts_pending`) iptal et. Herkese açık havale sayfasındaki IBAN'ı kontrol et (`/api/settings/public/bank-default`).
5. `freeze_refunds` ve `freeze_exports` aç. İşlem Geçmişi'nden hesabın yaptığı iade, export, tema/piksel değişikliği, kargo/fatura ve ayar değişikliklerini çıkar.
6. **Tema/piksel:** Ayarlar → Özel Tema (CSS/JS) ve Pazarlama Pixelleri sürüm geçmişinde hesabın değişikliği varsa önceki sürüme dön (§4.5).
7. **Vault:** `vault_reveal` olduysa görüntülenen sağlayıcı anahtarını döndür (§4.10).
8. Gerçek kişiyle telefonda doğrula. Hesabı geri açarken şifreyi sıfırlat, MFA'yı yeniden bağlat ve kurtarma kodlarını yeniden ürettir (eskiler geçersiz olur). Başka bir personelin şifresini ya da MFA telefonunu değiştirmek ikinci süper admin onayı ister; tek süper admin varsa işlem hemen uygulanır ve `single_admin_exception` uyarısı gider.
9. Denetim zincirini doğrula ve arşive aktar. Zincir kırıksa (`audit_chain_broken`) kayıtların silindiği varsayılır; R2 arşivindeki kopya esas alınır.

**Çıkış kriterleri:**

- Eski token ile `GET /api/admin/me/permissions` 401 dönüyor.
- Onay kuyruğu temiz.
- IBAN doğru.
- Öz-test 0 hata.
- Break-glass değişkeni yok.

### 4.2 Müşteri hesabının ele geçirilmesi (RB-01)

1. Üye Listesi → üyenin şifresini sıfırla (`customers.credentials` yetkisi). Bu işlem tüm oturumlarını düşürür. Müşterinin kendi SMS ya da e-posta sıfırlaması da eski oturumları ve bekleyen kodları geçersiz kılar. Gerekirse üyeyi pasifleştir veya Bloklu Müşteriler'e al (Bloklu Müşteriler'i yalnız süper admin değiştirir).
2. Hesaptan son siparişler: kargoya verilmemişse beklet. Hediye çeki ya da puan harcaması varsa finansla incele.
3. Saldırganın girdiği telefonu ya da e-postayı kurtarma kanalı kabul etme. Profilden değiştirilen telefon `phone_verified=False` olur ve SMS sıfırlamada kullanılamaz. Telefon değişince o numaraya gönderilmiş bekleyen kodlar da geçersizleşir.
4. Toplu deneme varsa Güvenlik Paneli → başarısız giriş grafikleri ve IP engeli. Kimlik doldurma (credential stuffing) dalgasında Cloudflare'de rate-limit kuralı ekle.

### 4.3 Müşteri verisi sızıntısı / yetkisiz dışa aktarım (RB-04)

**Tetikleyici:** `member_export`, `consent_export`, `orders_export`, `customers_export`, `returns_export`, `trendyol_claims_export` ya da influencer export uyarısı beklenmedik saatte geldi; `pii_bulk_reveal` uyarısı geldi (personel kısa sürede çok sayıda tam IBAN/TCKN açtı); dışarıda veri görüldü; KVKK talebi şüpheli.

1. `freeze_exports` aç.
2. Aktörün hesabını pasifleştir (§4.1).
3. Kapsamı **kayıtlarla** çıkar. Uyarı meta verisinde satır sayısı ve biçim var; İşlem Geçmişi'nde aktör, zaman ve uç var. `pii_bulk_reveal` uyarısında personel, saatlik sayı ve veri türleri yazar. Dışa aktarımlar sunucuda dosya bırakmaz; indirilen kopya geri alınamaz, bunu kayda yaz. Pasifleştirme, oturum iptali ya da `freeze_exports` anında sürmekte olan dışa aktarım en geç ~5 sn içinde kesilir (dosya eksik kalır, sonuna hata satırı eklenir) ve kritik `export_aborted_access_revoked` uyarısı gider; uyarıdaki `bytes_sent` kesilene kadar gönderilen bayt sayısıdır (CHAIN09).
4. Hassas alanlar:
   - TCKN/VKN, IBAN ve dekontlar DB'de şifreli. Ham DB dökümü sızdıysa bu alanlar açık değildir; ancak `SECRETS_MASTER_KEY` de sızdıysa açıktır (§4.10).
   - Ad, adres, telefon ve e-posta düz metindir.
5. Bağımsız kanıt için denetim zincirini arşive aktar.
6. **Hukuk:** etkilenen kişi sayısı, veri kategorileri, zaman aralığı ve alınan önlemlerle KVK Kurulu bildirim taslağını hazırla (72 saat). Amazon verisi varsa 24 saat bildirimi yap.
7. Kök neden ve düzeltme, CI testiyle birlikte yapılır (Kodlama). `freeze_exports` düzeltme deploy edilip test edildikten sonra kapatılır.

### 4.4 SMS maliyet saldırısı (SMS pumping) (RB-03)

**Tetikleyici:** kritik `sms_budget_exceeded` uyarısı (küresel SMS tavanı aşıldı); SMS sağlayıcı (Bildirim Sağlayıcıları'nda tanımlı Netgsm, İleti Merkezi ya da Verimor) bakiye ya da harcama uyarısı; olağan dışı `forgot-password/request-otp` hacmi; yabancı numaralar.

**Mevcut sınırlar (kod, testli):**

- **Küresel tavan:** bütün SMS'ler (bildirim, müşteri OTP'si, admin MFA, İYS kodu, influencer, test SMS'i) tek kapıdan geçer. Saatlik `sms.global_hourly_cap` (varsayılan 300) ve günlük (UTC) `sms.global_daily_cap` (varsayılan 2000) İşletme Kuralları → Güvenlik'te; yalnız süper admin değiştirir. Tavan dolunca rutin SMS'ler gönderilmez ve pencere başına bir uyarı gider.
- Admin MFA SMS'i tavan dolsa da küçük bir yedek payla (tavanın %5'i, en az 10) gitmeye devam eder; personel kilitlenmez.
- Sayaç deposu (MongoDB) okunamazsa SMS gönderilmez (fail-closed).
- Yalnız geçerli TR cep numarasına (`905…`) ve kayıtlı, doğrulanmış numaraya kod gider; kayıtsız numaraya SMS gönderilmez. Yanıt kayıtlı ve kayıtsız numara için aynıdır.
- Numara ve hesap başına 24 saatte en çok 5 sıfırlama kodu; yeniden istemek bütçeyi sıfırlamaz.
- Aynı numaraya 60 sn'de bir kod; IP başına 3/dk.
- Hesap başına 24 saatte 10 hatadan sonra yeni kod gönderilmez.
- Limit deposu çalışmazsa kod üretilmez (503).
- İYS doğrulama kodu numara başına günde 5.
- Admin MFA SMS'i yalnız şifre doğrulandıktan sonra gider.

**Adımlar:**

1. `sms_budget_exceeded` geldiyse önce gerçek bir kampanya ya da toplu bildirim mi, saldırı mı ayır. Entegrasyon/Bildirim loglarında son saatin SMS türlerine bak.
2. Saldırıysa tavanı **yükseltme**. Gerekirse İşletme Kuralları'ndan geçici olarak düşür; admin MFA yedek payla çalışmaya devam eder.
3. SMS sağlayıcı panelinde günlük harcama limitini düşür, uluslararası gönderimi kapat ve yeni API anahtarı oluştur.
4. Panelde SMS sağlayıcı kimlik bilgisini güncelle; yeni anahtar bir sonraki gönderimde kullanılır. Uygulama dışından gönderim varsa (anahtar sızıntısı) sağlayıcı raporundaki kaynak IP'lere bak.
5. Güvenlik Paneli'nde saldırgan IP'lerini engelle. Dağıtık saldırıda Cloudflare'de `/api/auth/forgot-password/*` için rate-limit kuralı ekle.
6. Müşteri kurtarması e-posta yoluyla açık kalır (`/forgot-password/email`). Admin MFA'yı SMS'ten e-postaya **indirme**; TOTP kullanan yöneticiler etkilenmez.
7. Olay bitince tavanı eski değerine döndür; değişiklik denetim kaydında görünür.

### 4.5 Zararlı script / piksel / tema kodu (RB-05)

**Tetikleyici:**

- `storefront_script_changed`: özel tema JS/CSS'i ya da bir pikselin kodu/aktifliği değişti. Her değişiklikte ve geri dönüşte gelir.
- `script_host_new`: vitrine daha önce görülmemiş bir script host'u eklendi.
- `csp_script_violation`: tarayıcılar manifest dışı bir script host'undan yükleme denemesini CSP raporuyla bildirdi.
- Öz-test "Vitrin script host manifesti" HATA: panel dışından (doğrudan DB'ye) yazılmış host.
- Vitrinde beklenmeyen alan adına istek; müşteriden kart sorunu bildirimi.

Özel tema ve piksel değişikliğini yalnız süper admin yapabilir; her kayıt sürümlenir ve denetlenir. CSP **zorlayıcı** (host izin listesi): izin listesi dışı host'tan script/bağlantı engellenir ve raporlanır. `unsafe-inline` kalır: satır içi enjeksiyonu engellemez ama sıkı Report-Only politikası onu raporlar. Vitrine doğrulanmamış build (backend commit'iyle eşleşmeyen/kimliksiz) sunulursa `storefront_build_unverified` kritik uyarısı gelir; meşru yalnız-frontend deploy'u `POST /api/admin/security/storefront-integrity/approve-build` ile gerekçeli onaylanır. Toplu ihlal sayaçları: `GET /api/admin/custom-theme/csp-reports`.

1. Kart ödeme formu etkilenmiş olabilir: kart numarası vitrinden backend'e gelir (iyzico API 3DS). Script kart alanını okuyabilir. **Hemen** Ödeme Tipleri → Kredi Kartı pasif ya da `close_checkout`.
2. Ayarlar → Özel Tema (CSS/JS) ve Pazarlama Pixelleri → geçmişten son temiz sürüme dön ya da kodu boşalt. Değişikliği yapan hesabı §4.1 ile ele al. Dikkat: sistem zararlı sürümü işaretleyip karantinaya almaz. Geri dönerken sürümün içeriğine bak, uyarıdaki host'u kontrol et; geri dönüş de `storefront_script_changed` uyarısı üretir.
3. Kaynak kodda değişiklik varsa (frontend/public, `functions/`): GitHub'da son commit'leri incele. Cloudflare Pages → Deployments → son temiz deploy'a **Rollback**.
4. Cloudflare → Caching → Purge Everything. Temiz sürümün gerçekten sunulduğunu gizli pencerede ve tarayıcı ağ sekmesinde doğrula.
5. Etkilenen zaman aralığındaki kart siparişlerini çıkar (Siparişler, ödeme yöntemi kart, tarih). iyzico risk ekibine bildir. Kart verisi sızmış olabilir: hukuk ve müşteri bilgilendirme planı yap.
6. Kart ödemesini, temiz sürüm doğrulandıktan ve olay komutanı onayladıktan sonra aç.

### 4.6 Ödeme sahtekârlığı, çift tahsilat, iade anomalisi (RUN-F01…F03)

**Sistem davranışı (testli):**

- Sipariş yalnız iyzico ödeme-detay sorgusu tutar, sipariş, sepet ve para birimiyle eşleşirse `paid/confirmed` olur.
- Uyuşmazlık, zaman aşımı ya da ödeme kimliği dönmesi durumunda `needs_reconciliation=True` işaretlenir; 15 dk'lık mutabakat cron'u iyzico'dan doğrulayıp tamamlar.
- Günlük iyzico mutabakatı (04:23 UTC, son 3 gün) ödenmiş kart siparişlerini iyzico'yla karşılaştırır: sağlayıcı durumu, tutar, paymentId ve (yanıt taşıyorsa) iade toplamı. Uyuşmazlık kritik uyarı üretir; siparişe yazmaz.
- Açık ödeme itirazı olan siparişte bütün iade yolları 409 döner (süper admin gerekçeli istisna hariç).
- İkinci başarılı ödeme `duplicate_payments` alanına yazılır, `needs_duplicate_refund=True` işaretlenir ve "duplicate_payment" push'u gider.
- 3 saat ödenmeyen kart siparişi iptal edilir ve stoğu iade edilir. Geç gelen gerçek ödeme siparişi kurtarır.

**a) Çift tahsilat**

1. Siparişler → ilgili sipariş: `duplicate_payments` listesi.
2. iyzico Merchant Panel → İşlemler: aynı sipariş (conversationId = sipariş kimliği/numarası) için iki başarılı işlemi doğrula.
3. Fazla tahsilatın iadesi panelden, `returns.iyzico_refund` yetkili personelce yapılır. Tutar tavanı ilk tahsilatla sınırlıdır; ikinci işlem için iadeyi iyzico panelinden yapıp sipariş notuna işlem kimliğini yaz.
4. Kör iade yapma: hangi tahsilatın siparişi karşıladığı iyzico kaydıyla belirlenir.

**b) Belirsiz tahsilat / "ödeme kaydı bulunmayan"**

1. Siparişler → Ödeme Kaydı Bulunmayan ve Ödeme İzi (Numaradan).
2. iyzico panelinde işlem var ve başarılıysa mutabakat cron'unun (15 dk) işlemesini bekle ya da Kodlama ile tetikle. Elle "ödendi" yapma.
3. Günlük mutabakat uyarısı (`reconciliation_mismatch`) geldiyse: Entegrasyon Logları → iyzico'da sipariş ve fark türü yazar (durum, tutar, paymentId, iade). iyzico panelindeki kayıt esastır; düzeltmeyi bu el kitabındaki ilgili adımla yap, siparişi elle "ödendi" yapma.
4. iyzico'da işlem yoksa sipariş `awaiting_payment` kalır ve 3 saatte otomatik iptal edilir. Siparişin kupon hakkı bu iptalde geri verilir.

**c) İade anomalisi** (beklenmeyen iade, `refund_needs_verification`, toplu iade)

1. `freeze_refunds` aç.
2. Sonucu "unknown" olan iadeler yeni iadeyi zaten kilitler. iyzico panelinde gerçek sonucu gör, sonra `refund/resolve` ile `done` ya da `failed` işaretle.
3. Yetkisiz iade varsa yapan hesabı §4.1 ile ele al. İade tamamlanmışsa geri çekilemez; finans ile müşteri iletişimi.

**d) Sahte sipariş / kart dolandırıcılığı dalgası**

1. iyzico risk ekibine bildir.
2. Gerekirse `close_checkout` ya da yalnız kart ödemesini kapat.
3. Kargoya verilmemiş şüpheli siparişleri beklet.
4. **Chargeback:** Siparişler → Ödeme İtirazları (`/admin/odeme-itirazlari`) kaydı açılır. Kayıt sipariş ve ödeme durumunu değiştirmez; son tarihten 3 gün önce günlük uyarı gider. Açık itirazı olan siparişte iyzico iadesi, iade ödemesi işaretleme, toplu "iade ödendi" ve durumu "iade edildi" yapma **409 ile engellenir**; para banka süreciyle zaten dönebilir. İstisna gerekiyorsa (ör. banka itirazı geri çekildi ama kayıt henüz kapanmadı) önce Ödeme İtirazları'nda kaydın durumunu güncelle (`PATCH /api/admin/chargebacks/{id}`; kapanan itiraz iadeyi kilitlemez). Bu mümkün değilse yalnız süper admin, isteğe `chargeback_override_reason` (en az 10 karakter) yazarak iade yapabilir; istisna denetim zincirine yazılır.

**e) Kupon kötüye kullanımı**

1. Kullanım limitli kuponlarda hak sipariş oluşturulurken atomik ayrılır; son hak eşzamanlı siparişlerde bir kez verilir. Ödenmemiş siparişin hakkı 3 saatlik süpürmede geri döner.
2. Bir kupon beklenenden hızlı doluyorsa Kampanyalar/Kuponlar ekranından kuponu pasifleştir. Sayaç belgelerini (`coupon_usage_counters`, `coupon_usage_holds`) elle düzeltme; yanlışsa Kodlama ile incele.
3. Otomatik uygulanan kampanyalar bilinçli olarak sayım tabanlı kalır (terk edilmiş sepetle dolmasın diye); kişi başı limit yalnız ödenmiş kullanımı sayar (işletme kararı). Bunlarda rezervasyon yoktur.

**f) Callback imza hatası artışı**

1. Entegrasyon Logları → iyzico: hata nedeni (imza, ortam anahtarı, gövde).
2. Son deploy'u ve iyzico ayarındaki ortamı (sandbox/canlı) karşılaştır.
3. Doğrulamayı **gevşetme**. Doğrulanamayan ödemeler mutabakat cron'uyla ve iyzico paneliyle eşlenir.

### 4.7 Fatura mükerrerliği (Doğan e-Arşiv/e-Fatura)

**Sistem davranışı (testli):**

- Zaman aşımından sonra Doğan'da mevcut belge aranır ve bulunursa bağlanır.
- Tekrar denemede aynı UUID ve numara kullanılır.
- Belirsiz sorgu 409 "inceleme gerekli" döner; kesin redde sayaç geri alınır.
- Ödenmemiş kart siparişine ve onaysız havaleye fatura kesilmez.

**Adımlar:**

1. `restore_mode` aç. Bu, Doğan'a yeni fatura gönderimini ve pazaryeri fatura linkini durdurur.
2. Doğan portalında sipariş numarasıyla ara. Aynı sipariş için iki belge varsa hangisinin muhasebeye işlendiğini muhasebeyle belirle. Fazla belgenin iptalini Doğan portalı ve mevzuat süreleri içinde yap; e-Arşiv iptali GİB kurallarına tabidir.
3. Paneldeki sipariş kaydının doğru UUID ve numarayı gösterdiğini kontrol et. DB'de belge kaydını silerek "düzeltme" yapma; muhasebe izi korunur.
4. Pazaryerine fatura linki yanlış gittiyse ilgili pazaryeri panelinden düzelt.
5. Kök neden Entegrasyon Logları → Doğan. Kalıcı düzeltme testle yapılır (Kodlama). Sonra `restore_mode` kapatılır. Not: `restore_mode` açıkken iadeler de kapalıdır; kapatınca geri yükleme kapanışı çalışır (herkes yeniden giriş yapar) ve iadeler iyzico mutabakatı temiz bitene kadar kilitli kalır (§2.1, madde 5).

### 4.8 Pazaryeri senkron bozulması (RUN-F04)

**Tetikleyici:** "Entegrasyon durdu: …" tazelik uyarısı (sipariş çekme ya da stok gönderimi beklenen aralığın 5 katı süredir başarısız); Trendyol, Hepsiburada ya da Amazon'da toplu sıfır stok, yanlış fiyat, ürün kapanması; Aktarılamayanlar listesinde ani artış; XML feed'den toplu fiyat sıçraması ya da reddedilen içe aktarma.

Yalnız tazelik uyarısı geldiyse ve stok/fiyat bozulması yoksa önce nedene bak: Otomasyon Durumu'nda iş çalışıyor mu, Entegrasyon Logları'nda kimlik bilgisi ya da sağlayıcı hatası var mı? Kaynak düzelince "yeniden çalışıyor" bildirimi kendiliğinden gelir. `pause_marketplace_sync` açıkken tazelik uyarısı gelmez. Veri bozulması varsa aşağıdaki adımlara geç.

1. `pause_marketplace_sync` aç. Zamanlayıcı dış işleri atlar; fatura linki gönderimi durur.
2. Entegrasyon Logları ve Otomasyon Durumu'ndan son başarılı ve ilk hatalı çalışmayı belirle.
3. Hatalı değerleri pazaryeri satıcı panelinden düzelt ya da ilgili ürünleri satışa kapat. Toplu "sıfırla" yapma.
4. **XML feed:** büyük fiyat sıçraması olan ürünler zaten incelemeye alınır; elle düzenlenmiş fiyat ezilmez; stok kaynağı olmayan feed stok yazmaz. Her içe aktarma bir manifest yazar. Ürün sayısı son başarılı içe aktarmaya göre %40'tan fazla düşerse ya da feed daha eski üretimse içe aktarma reddedilir ve etkilenen ürün raporu yazılır. Uygulanan içe aktarmada fiyatı değişen ürünleri içeren açık siparişler raporda listelenir. Reddi bilerek aşmak (`force_integrity`) yalnız süper admin içindir ve kayıtlıdır; feed kaynağı (adresi) değiştiyse ilk içe aktarma da bu zorlamayı ister (aynı kaynakta token yenilemek istemez). Feed'in eksik olmadığını doğrulamadan zorlama. Son içe aktarımın etkilediği ürünleri XML Feed'ler ekranından kontrol et.
5. Etkilenen aralıkta pazaryerinden gelen siparişleri çıkar. Hatalı fiyatla gelen siparişlerin kaderine işletme karar verir; otomatik iptal yok.
6. Kimlik bilgisi sızıntısı şüphesinde ilgili pazaryeri API anahtarını döndür (§4.10). Yeni anahtar bir sonraki çağrıda kullanılır. Anahtar kapatılınca tekrar aç.
7. **Kullanıcı kararlarını koru** (CLAUDE.md): senkron düzeltmesi panelde elle girilmiş değerleri ezmez.

### 4.9 Veritabanı geri yükleme (RUN-F06, RUN-F07)

Geri yükleme, ancak veri bozulması ya da kaybı, düzeltme betiğiyle onarılamıyorsa yapılır.

**Hazırlık (canlıya dönmeden önce):**

1. `close_checkout` ve `restore_mode` **aç**. `restore_mode` bütün iade uçlarını da kapatır (423); `freeze_refunds` ayrıca gerekmez (açmak zararsızdır). Anahtarlar DB'de tutulur; geri yüklenen snapshot'ta kapalı olabilir. Restore'dan hemen sonra yeniden aç.
2. Kesim zamanını kaydet: snapshot saati (Atlas) ve olay saati. Aradaki pencere "kayıp pencere"dir.
3. Mümkünse **önce ayrı bir cluster'a** restore et ve kontrol et. Canlıya restore ayrı bir karardır (Atlas → Backup → Restore).

**Restore sonrası, sırasıyla:**

1. Anahtarları yeniden aç: `close_checkout` ve `restore_mode` açık.
2. **Oturumlar:** Güvenlik Paneli → Tüm Oturumları Sonlandır. Snapshot eski oturum dönemini geri getirmiş olabilir; eski tokenlar yeniden geçerli olmasın. (`restore_mode` kapanışı dönemi bir kez daha artırır.)
3. **Personel (ara dönem):** Snapshot'tan sonra pasifleştirilen ya da silinen personel, `restore_mode` kapanana kadar yeniden aktif görünebilir. Kapanış bunu otomatik düzeltir (aşağıda, 8.5). Ara dönemde Kullanıcılar & Roller'i kontrol et; ayrıldığını bildiğin kişiyi elle pasifleştir. Snapshot'tan sonraki **rol, davet ve IBAN** değişiklikleri otomatik yeniden uygulanmaz; R2 denetim arşiviyle karşılaştırıp elle uygula.
4. **KVKK ve pazarlama retleri:** `restore_mode` kapanışında otomatik yeniden uygulanır. Ara dönemde kampanya gönderme. İstersen KVKK adımını erkenden elle de çalıştırabilirsin: `POST /api/admin/kvkk/reapply-tombstones`.
5. **İYS:** kesim sonrası verilen RET'ler İYS'de kayıtlıdır. İYS ekranından son senkronu çalıştır.
6. **Mutabakat (§5):** kayıp penceredeki iyzico, Doğan, MNG/DHL ve pazaryeri işlemleri. iyzico için kesim penceresini kapsayan mutabakat `restore_mode` kapanışında otomatik çalışır (§2.1, madde 5); günlük iş yalnız son 3 günü kapsar ve kilidi kaldırmaz. Snapshot'tan sonra iyzico'da tamamlanmış iadeler yerel kayıtta yoktur; mutabakat bunları uyuşmazlık olarak bulur ve kilit açık kalır. Uyuşmazlıkları iyzico panelinden çöz, sonra "yeniden dene" ya da gerekçeyle "elle kaldır" (§2.2). Eksik siparişler dış kayıtlardan yeniden oluşturulmaz, elle vaka açılır. Kart tahsilatı yeniden yapılmaz.
7. **Öz-test:** 0 hata olmalı. Özellikle "Arşiv referansları (restore mutabakatı)": kayıtların işaret ettiği dekont, fatura PDF'i ve mezar taşı nesneleri R2'de olmalı. Eksik listesi Entegrasyon Logları'nda (yalnız anahtar yolu).
8. **Aşamalı açılış**, her adımda smoke ile:
   1. Katalog (zaten açık).
   2. Giriş.
   3. `close_checkout` kapat → havale siparişi.
   4. Kart ödemesi.
   5. `restore_mode` kapat (fatura, kargo, pazaryeri). **Bu, otomatik kapanışı çalıştırır:** oturum dönemi artar (herkes yeniden giriş yapar, kapatan süper adminin paneli açık kalır), KVKK mezar taşları ve pazarlama retleri yeniden uygulanır, R2 denetim arşivindeki personel pasifleştirme/silmeleri yeniden uygulanır, geri yükleme sonrası iade kilidi açılır ve restore penceresi iyzico mutabakatı arka planda başlar. Ekrandaki bildirimi ve `restore_finalized` uyarısını kaydet: yeniden pasifleştirilen personel sayısı ve hata varsa adı.
   6. İade kilidinin durumunu izle (`refund_hold`): `refund_hold_cleared` uyarısı geldiyse iadeler açıktır. `post_restore_reconciliation_failed` geldiyse `last_problems`'a bak, iyzico panelinden kontrol et, sonra yeniden dene ya da gerekçeyle elle kaldır. `freeze_refunds`'ı ayrıca açtıysan şimdi kapat.
   7. `pause_marketplace_sync` kapat.
9. RPO ve RTO'yu kayda yaz.

### 4.10 Sır sızıntısı (RUN-F05)

Genel sıra:

1. Sırrın kapsamını belirle.
2. Sağlayıcı panelinde iptal et ve yenisini üret.
3. Railway Variables'ı ya da panel ayarını güncelle.
4. Öz-testi çalıştır.
5. İhlal penceresindeki işlemleri sağlayıcı raporundan bağımsız kontrol et.
6. gitleaks ya da GitHub bulgusu ise geçmişi temizlemek iptalin yerine geçmez. Not: git geçmişinde döndürülmeyi bekleyen 5 gerçek sır var; liste `.gitleaksignore`'daki `PENDING-ROTATION` satırlarında (değer yok). Rotasyon README §4'e göre yapılır; o zamana kadar gitleaks kırmızı kalır.

| Sır | Etki | Müdahale |
|---|---|---|
| **`JWT_SECRET`** | Saldırgan herhangi bir kullanıcı adına (süper admin dahil) token üretebilir; MFA atlanır | **Kritik.** Hemen `close_checkout`, kart ödemesini kapat, panel API'sini kısıtla (Cloudflare WAF ile `/api/admin/*` yollarını yalnız yönetici IP'lerine aç). `JWT_SECRET` doğrudan **değiştirilmez**: eski (SECRETS_MASTER_KEY öncesi) şifreli kayıtlar, KVKK mezar taşı, PII arama özeti ve eski fatura linkleri bu değerden türetilmiş anahtarlara bağlı olabilir. Kodlama önce şunu yapar: `SECRETS_MASTER_KEY`, `PII_HASH_SECRET`, `KVKK_TOMBSTONE_SECRET` ve `INVOICE_LINK_SECRET` tanımlı mı kontrol eder; tüm `v1:` değerleri SMK ile yeniden şifreler (`security.crypto.rotate_secret`) ve öz-test "Şifreli sırlar" ile doğrular. Ancak bundan sonra JWT_SECRET değişir ve herkes yeniden giriş yapar |
| `SECRETS_MASTER_KEY` | DB dökümüyle birlikte TCKN/VKN, IBAN, dekont, MFA ve sağlayıcı sırları açılabilir | Doğrudan değiştirme (çözülemez olur). Kodlama: yeni anahtarla yeniden şifreleme betiği, sonra değişim. Bu sürede DB erişimini kısıtla (Atlas kullanıcı şifresi ve IP erişim listesi) |
| iyzico API/secret | Ödeme/iade API çağrısı | iyzico panelinde anahtar yenile; Entegrasyonlar → iyzico ayarı; `freeze_refunds`; iyzico işlem raporunda ihlal penceresi |
| `MONGO_URL` (Atlas kullanıcı) | Tüm DB | Atlas → Database Access → şifre değiştir ya da yeni kullanıcı; Railway Variables; Atlas Network Access listesini daralt |
| R2 erişim anahtarları | Görseller ve gizli arşiv | Cloudflare → R2 → API Tokens → iptal ve yeniden üret; arşiv bucket'ında silinen nesne var mı bak |
| Doğan, MNG/DHL, Trendyol/HB/Amazon, İYS, SMS, Resend | İlgili dış işlem | Sağlayıcı panelinde döndür → panel ayarı ya da vault → `pause_marketplace_sync` / `restore_mode` gerektiği kadar |
| Google OAuth client secret | Sosyal giriş | Google Cloud Console → yeni secret → Ayarlar → Sosyal Giriş |
| `INVOICE_LINK_SECRET` | Fatura linki sahteciliği | Yeni değer; eski linkler yeniden imzalanmalı (Kodlama) |
| `CF_EDGE_SECRET` | Hız sınırı atlatma | Railway ve Cloudflare Transform Rule'da birlikte değiştir |

### 4.11 Fidye yazılımı benzeri veri kaybı veya silme (RB-07)

**Tetikleyici:** Atlas'ta koleksiyonlar silinmiş ya da şifrelenmiş, toplu kayıt kaybı, fidye notu; R2'de toplu silme; Railway projesinde yetkisiz değişiklik.

1. **Yetkileri kes:** Atlas'ta uygulama kullanıcısının şifresini değiştir, şüpheli kullanıcıları ve API anahtarlarını sil, Network Access'i daralt. Railway, Cloudflare ve GitHub hesaplarında şifre ve MFA kontrolü yap; oturumları sonlandır.
2. `close_checkout`, `restore_mode` ve `freeze_refunds` (DB erişilebiliyorsa).
3. Atlas → Backup: saldırı öncesi en son temiz snapshot. Yedek de silinmişse Atlas desteği ve Backup Compliance Policy. R2 arşivinde denetim zinciri, dekontlar ve mezar taşları ayrıca durur.
4. Ayrı cluster'a restore et, doğrula, sonra §4.9.
5. Veri **çalınmış** olabilir; şifrelemenin durması sızıntı olmadığı anlamına gelmez. §4.3 adımlarıyla hukuk değerlendirmesi yap.
6. Fidye ödeme kararı bu el kitabının konusu değildir; hukuk ve olay komutanı.

### 4.12 Resend / uyarı e-postası kesintisi

**Belirti:** Öz-test kartında "Uyarı e-postası teslimi" ya da "Güvenlik kayıt/uyarı hattı" kontrolü HATA; `security_pipeline_degraded` uyarısı (panel içinde görünür); günlük öz-test e-postası gelmiyor; Resend panelinde 403 (alan adı doğrulanmamış) ya da limit; öz-test "Denetim outbox'ı" UYARI ya da HATA.

**Sistem davranışı:**

- Kanal sırası: SMTP (`ALERT_SMTP_*` tanımlıysa) → Resend → mağaza işlem maili yedeği.
- Hiçbiri ulaşmazsa teslim hatası `alerts` koleksiyonuna yazılır; panel içi uyarı her zaman görünür.
- Başarısız teslim bir sonraki öz-testte yeniden denenir.
- Güvenlik hattı kontrolü 5 dakikada bir teslim edilemeyen uyarıyı, denetim arşivi gecikmesini (30 dk) ve zincir boşluğunu arar.
- Denetim kaydı yazılamazsa kritik olmayan kayıtlar `audit_outbox`'a, Mongo da yoksa yerel dosyaya (20 MB tavan) gider ve 5 dakikada bir zincire aktarılır. Kritik işlemler (iade, IBAN, export, rol, anahtar…) denetim yazılamazsa yapılmaz.

**Adımlar:**

1. Güvenlik Paneli → Test uyarısı: alıcı başına sonuç ve kanal (`resend` / `smtp` / `magaza_maili`) ile Resend uyarı nedeni görünür.
2. Resend → Domains: gönderen alan adı "Verified" mi? DNS kayıtlarını (SPF, DKIM, MX) Cloudflare DNS'te kontrol et.
3. Alıcılar: İşletme Kuralları → `security.alert_emails`. Boşsa aktif süper adminlere gider. Gönderen: `RESEND_FROM` ya da `security.alert_from`.
4. Kesinti sürerken Güvenlik Paneli'ni günde iki kez elle kontrol et: öz-test kartı ve son olaylar.
5. Çözülünce öz-testi elle çalıştır; teslim "ok" olmalı.
6. Öz-test "Denetim outbox'ı" HATA (yerel tavan doldu, kayıt düştü) verdiyse düşen kayıt sayısını olay kaydına yaz. Bu kayıtlar geri getirilemez; kesinti penceresini İşlem Geçmişi'nde "bilinen boşluk" olarak not et. Mongo kesintisi sürerken deploy yapma: Railway'de yerel dosya kalıcı değildir.

---

## 5. Mutabakat prosedürleri

Kesim zamanını (saat dilimi: TR) ve dış raporun tam olduğunu kaydet. Farkları şu sınıflara ayır:

- yerelde eksik,
- dışta eksik,
- tutar farkı,
- mükerrer,
- belirsiz,
- yalnız zamanlama farkı.

Düzeltme önerisi önce listelenir; dış para, etiket ya da fatura doğuracak düzeltme ayrı onayla yapılır. Kayıt silinerek denkleştirme yapılmaz.

| Sistem | Dış kaynak | Panel karşılığı | Kontrol |
|---|---|---|---|
| iyzico tahsilat | Merchant Panel → İşlemler / Raporlar (tarih aralığı) | Siparişler (kart, paid), Ödeme İzi, Ödeme Kaydı Bulunmayan, Entegrasyon Logları (`reconciliation_mismatch`) | Her başarılı iyzico işlemi tek siparişe bağlı mı? `duplicate_payments` ve `needs_reconciliation` listeleri boş mu? Günlük otomatik mutabakat son 3 günü kapsar; komisyon ve hesaba geçen tutar iyzico mutabakat defterinde (aylık özet + CSV, `GET /api/admin/payment-ledger/summary`); ay sonunda defter toplamı iyzico hesap özetiyle karşılaştırılır, açık uyuşmazlık listesi boş olmalı |
| iyzico iade | Merchant Panel → İadeler | Sipariş `refunds`, İadeler ekranı | `unknown` iade kalmadı mı? Toplam iade ≤ tahsilat mı? |
| iyzico itiraz | Merchant Panel → Chargeback | Ödeme İtirazları | Her itiraz kayıtlı ve son tarihli mi? Kapanan itirazın kaydı kapatıldı mı? (Açık kayıt iadeyi kilitler) |
| Doğan | Doğan portalı → giden belgeler | Sipariş fatura no/UUID, E-Arşiv/E-Fatura ayarları | Her ödenmiş siparişe tek belge; iptaller işlenmiş |
| MNG / DHL | Kurumsal panel → gönderiler | Sipariş kargo takip no, etiketler | Etiketi olup panelde takip numarası olmayan gönderi yok; tek etiket |
| Trendyol / HB / Amazon | Satıcı paneli → siparişler, stok, faturalar | Pazaryerleri Hub, Aktarılamayanlar, sipariş listesi | Sipariş sayıları, stok ve fiyat farkları, fatura linkleri |
| İYS | İYS portalı | İYS ekranı, Pazarlama İzinleri, öz-test "Pazarlama ret listesi arşivi" | RET'ler yerelde de ret mi? Ret arşivi güncel mi (gece 03:37 UTC)? |
| Havale/EFT | Banka ekstresi | Havale/EFT Bildirimleri | Onaylanan her havale ekstrede var mı? |

---

## 6. Kayıt ve kapanış formu

```
Olay kimliği:            Başlangıç (TR saati):          Komutan:
İlk sinyal:              Tespit eden:                   Öğrenme zamanı (KVKK sayacı):
Açılan anahtarlar (saat / gerekçe):
İptal edilen kimlikler / döndürülen sırlar (sağlayıcı + saat; değer YAZILMAZ):
Etkilenen siparişler / kişi sayısı / veri kategorileri:
Dış sistem kimlikleri (iyzico işlem no, Doğan UUID, kargo takip no):
Mutabakat sonucu (fark sınıfları ve sahipleri):
Bildirimler (KVK Kurulu / Amazon / iyzico / müşteri) — saat:
Kök neden:                Kalıcı düzeltme (commit + test):
Anahtarların kapatılma saati ve onaylayan:
Kalan risk / istisna (yayina-cikis-kapisi.md'ye işle):
```

Tatbikat önerisi 3 ayda birdir; her seferinde bir senaryo seçilir (§4.1, §4.6, §4.9 öncelikli). Ölçülecekler: tespitten insan teyidine kadar geçen süre, anahtar açma süresi, oturum iptali ve restore süresi. Sonuç `yayina-cikis-kapisi.md` İ-13 satırına işlenir.

**Alarm tatbikatı (SOC-01, otomatik):** her ayın ilk Pazartesi 10:07'de (İstanbul) ve istenince Güvenlik Paneli → "Tatbikat Başlat" ile gerçek uyarı hattından `[TATBİKAT]` etiketli kritik uyarı gider. Alıcılardan biri e-postadaki "Gördüm / İnceliyorum" bağlantısını açıp butona basar ya da panelde "Onayla"ya tıklar. 15 dk içinde onay → geçti; yoksa kaldı ve tüm alıcılara "YANIT YOK" (+ SMS) gider. Sonuç ve onay süresi panelde "Tatbikat geçmişi"nde; öz-test son 35 günde başarılı tatbikat yoksa uyarır. Gerçek her kritik uyarı da aynı şekilde onaylanır; onaylanmayan kritik uyarı İşletme Kuralı `security.ack_escalation_minutes` (varsayılan 15) sonunda bir kez yükseltilir.
