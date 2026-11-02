# Kişisel Veri Envanteri — Telefon ve Adres (Müşteri + Influencer)

> Sahip talebi (2026-10): **"Müşteri ve influencer adres ve telefonlarının güvenliği son derece önemli."**
> Bu belge, telefon / adres (ve influencer IBAN / TC) verisinin **nerede saklandığını, kimin okuyabildiğini,
> nereye gittiğini ve nasıl korunduğunu** listeler. Kod tarafındaki tek merkez:
> `backend/security/contact_pii.py` (TCKN/VKN için `backend/security/pii.py`).

## Koruma katmanları (özet)

| Katman | Ne yapar | Nerede |
|---|---|---|
| **Maskeleme** | Personel listelerinde telefon `+90 *** *** ** 33`, açık adres `••••••` (il/ilçe açık) | `contact_pii.mask_order_contact`, `mask_doc` — sipariş, iade, silinen sipariş, üye, influencer, PR, sepet, stok-haber, İYS, RFM listeleri |
| **"Göster" (tek kayıt)** | Tam değer yalnız tek kaydın açma ucunda, doğru izinle | `GET /api/orders/{id}/contact` (orders.view), `GET /api/admin/members/{id}/contact` (customers.view), `GET /api/influencers/{id}/contact` (campaigns.influencers) |
| **Açma sayacı + alarm** | Her açma `note_pii_reveal` (PRIV07) sayacına işlenir; saatte >50 (env `PII_REVEAL_HOURLY_MAX`) → kritik uyarı + 1 saat engel; `auth_audit_logs` → `pii_contact_view` (değer yazılmaz) | `contact_pii.note_contact_reveal` |
| **At-rest şifreleme** | `<alan>_enc` (Fernet, `security/crypto.py`), telefon için `<alan>_hash` (alan-ayrımlı HMAC; arama) + `<alan>_last2`; IBAN `_last4`; adres defterinde `address_hash` (tekilleştirme) | influencer, influencer_pr, addresses, users (müşteri telefonu) |
| **Backfill + gece ağı** | Tek seferlik (bayrak `pii_contact_backfill_v1`, doğrula-sonra-sil, kaldığı yerden sürer) + her gece 01:53 UTC kaçak düz değer taraması (uyarı loglar) | `scheduler.encrypt_existing_contact_pii[_nightly]` |
| **Saklama süresi (minimizasyon)** | Kapanmış siparişte süre dolunca açık adres silinir, telefon son 2 hane; ilk çalışma KURU + süper admin raporu | `scheduler.minimize_old_order_contact_pii` (03:23 UTC), kural `privacy.order_pii_retention_days` (730, taban 365), `privacy.order_pii_minimize_enabled` |
| **Maske koruması** | Maskeli değer formdan geri gelirse gerçek değer korunur (maske asla gerçek değerin üstüne yazılmaz) | `carry_over_masked_order_contact`, `sealed_set_update` |
| **Sızıntı süzgeçleri** | Log filtresi (`[TEL]`/`[ADRES]`), push, güvenlik uyarı e-postası, entegrasyon/olay logu, LLM istemleri, hata yanıtları | `server._SecretRedactFilter`, `routes/push.py`, `security/alerts.send_alert`, `integrations_common.log_integration_event`, `ai_chatbot.llm_chat`, `report_assistant._temizle` |

## 1. Müşteri verisi

| Veri | Nerede saklanıyor | Kim okuyabilir | Nereye gidiyor | Koruma |
|---|---|---|---|---|
| Sipariş teslimat adresi + telefonu | `orders.shipping_address.{address, address_line*, postal_code, neighborhood, phone}`, `orders.phone/customer_phone/user_phone` (düz — kargo/fatura için okunur) | Personel (orders.view): liste + detay **maskeli**; tam değer yalnız "Göster". Sipariş sahibi müşteri: kendi siparişinde tam. | MNG/DHL (kargo kaydı + etiket, sunucuda), Doğan e-Arşiv/e-Fatura (fatura), iyzico (ödeme isteği buyer/shippingAddress), Netgsm (müşteriye SMS), pazaryeri senkronu | Maskeleme, "Göster"+sayaç+denetim, saklama süresi minimizasyonu (730 gün), log/olay/entegrasyon maskeleme, düzenleme formunda maske koruması |
| Sipariş fatura adresi + telefonu | `orders.billing_address.*`, `orders.invoice_address.*` (VKN/TCKN ayrıca `pii.py` ile şifreli) | Aynı | Doğan (fatura UBL) | Aynı; VKN/TCKN şifreli |
| Pazaryeri siparişi adres/telefon | `orders.shipping_address`, `orders.shipmentAddress/invoiceAddress` (Trendyol/HB/Amazon/Temu aktarımı) | Aynı | Kaynak pazaryerinin kendisi; kargo | Aynı (minimizasyon pazaryeri siparişlerini de kapsar; Amazon için ayrıca 30 günlük DPP anonimleştirmesi korunur) |
| Silinen (arşiv) sipariş | `orders_deleted.*` | orders.view: **maskeli** (`/api/orders/deleted`, `/api/admin/orders/deleted`) | — | Maskeleme, minimizasyon |
| İade kaydı telefon/adres | `customer_returns.customer_phone`, `shipping_address`, `orders.return_request.*` | returns.view: liste **maskeli** | MNG iade kargosu (sunucuda) | Maskeleme, minimizasyon (bağlı sipariş minimize edilince) |
| Üye profil telefonu | `users.phone_enc / phone_hash / phone_last2` (müşteri; personelde düz `phone` + MFA'da `mfa_phone_enc`) | Sahibi (`/auth/me`, profil); customers.view: liste/detay **maskeli**, "Göster" ile tam; customers.export: Excel'de tam (satır sayılı denetim + freeze_exports) | Netgsm (SMS / şifre sıfırlama OTP / MFA), İYS (izin kaydı) | **At-rest şifreli**; mükerrer kontrol, SMS ile şifre sıfırlama, İYS doğrulama, WhatsApp eşleşmesi **hash eşitliği** ile (regex/parça tarama yok); backfill + gece ağı |
| Adres defteri | `addresses.{phone_enc, phone_hash, phone_last2, address_enc, address_hash}` (ad/il/ilçe düz; vergi no `pii.py` ile şifreli) | Sahibi (`/my-addresses`: kendi verisi açık); customers.view: üye kartında **maskeli**, "Göster" ile tam | Ödeme sayfası ön-doldurma (yalnız sahibine) | **At-rest şifreli**; tekilleştirme `address_hash` ile |
| İYS izin kaydı telefonu | `iys_consents.phone` (yasal izin kanıtı — düz) | customers.view: liste **maskeli**; customers.export: Excel tam | Netgsm İYS API | Maskeleme; ihracat izin+guard+formül-güvenli |
| Terk edilmiş sepet telefonu | `cart_sessions.phone` | campaigns.view: liste **maskeli** | Hatırlatma SMS'i (sunucuda) | Maskeleme |
| "Stoğa gelince haber ver" telefonu | `stock_alerts.phone` | products.view: liste **maskeli** | Bildirim SMS'i (sunucuda) | Maskeleme |
| Şifre sıfırlama OTP kaydı | `password_reset_otps.phone` (5 dk geçerli) | Kimse (yalnız sunucu) | — | Gece işi 7 günden eski kayıtları siler |
| İYS OTP doğrulama kaydı | `otp_verifications.phone` | Kimse (yalnız sunucu) | — | Yasal izin kanıtı — silinmez |
| SMS kara listesi | `sms_suppressions.phone` | Kimse (yalnız sunucu) | — | Gönderim engeli için gerekli |
| WhatsApp konuşmaları | `whatsapp_conversations/messages/handoffs` (gönderen numara + mesaj) | questions.view (teşhis ucu PII'siz) | Meta WhatsApp; LLM'e giden metin **süzülür** (`[telefon]/[adres]/[e-posta]`) | LLM süzgeci; push şablonu PII'siz |
| Destek talebi | `tickets.{email, message}` (serbest metin) | customers.view | — | — |
| KVKK mezar taşı | `deletion_tombstones.phone_hmacs` (yalnız HMAC) | — | Özel arşiv | Düz değer yok |
| Teşhis logları | `integration_logs.details` (ham pazaryeri yükü), `order_events.meta` | Personel log ekranları | — | Telefon son 2 hane, adres `••••••`, TCKN maskeli; hata metni süzülür |
| Personel e-postaları | Yeni sipariş maili (personele) | E-posta kutusu | SMTP/Resend | Telefon son 2 hane, teslimat yalnız ilçe/il |
| Güvenlik uyarıları | `alerts` + uyarı e-postası | Süper adminler | SMTP/Resend | Başlık/gövde/meta tek noktada süzülür |
| Push bildirimleri | — | Personel cihazı | FCM/APNs | Şablon + telefon/adres süzgeci; `adres/gsm/...` data anahtarları atılır |
| Reklam platformları | — | — | Meta/TikTok/Google/Pinterest/Snapchat CAPI | Yalnız SHA-256 e-posta/telefon (+ il/ilçe hash); **açık adres ve posta kodu gönderilmez**; consent gate (`marketing.capi_consent_gate`) satın alma + iade/iptal olaylarında |
| Herkese açık sipariş-no ucu | `GET /api/orders/by-number/{n}` | Herkes (sipariş no ile) | — | Ad/soyad maskeli, telefon son 2 hane, **açık adres dönmez** (yalnız il/ilçe) |
| Tarayıcı | `localStorage.facette_last_address` | — | — | Hiç yazılmaz; ödeme sayfası açılışında ve çıkışta silinir |

## 2. Influencer verisi

| Veri | Nerede saklanıyor | Kim okuyabilir | Nereye gidiyor | Koruma |
|---|---|---|---|---|
| Telefon | `influencers.phone_enc/_hash/_last2`, `influencers.shipping_address.phone_*` | campaigns.view: liste/detay/geçmiş **maskeli**; campaigns.influencers: "Göster" (sayaç + denetim) | MNG (gönderi kaydı), Netgsm (kargo SMS'i) — sunucuda çözülür | **At-rest şifreli**, arama yalnız tam numara (hash) |
| Adres | `influencers.adres_enc`, `influencers.shipping_address.adres_enc` (il/ilçe düz) | Aynı | MNG, kargo etiketi, sevk irsaliyesi (campaigns.influencers) | **At-rest şifreli** |
| IBAN | `influencers.iban_enc/_last4` (varsa; `bank_info/payment_info` altında da) | Aynı | — | **At-rest şifreli**, listede `TR** … 12 34` |
| TC kimlik / vergi no | `influencers.tc_enc/_hash/_last2` (`tc`, `tckn`, `tc_no`, `identity_number` …) | Aynı | — | **At-rest şifreli** (pii.py hash'i), listede `*********12` |
| PR / gönderi kaydı telefon + adres | `influencer_pr.phone_*`, `influencer_pr.adres_enc` | campaigns.view: **maskeli** | Kargo etiketi / irsaliye (campaigns.influencers) | **At-rest şifreli** |
| Influencer Excel'leri | `/api/influencer-registry/export`, `/api/influencer-pr/export`, `/api/influencer-pr/calendar-export` | campaigns.view (maskeli sütunlar); **tam telefon/adres yalnız `campaigns.influencers_export`** | Dosya | freeze_exports + akış koruması, satır sayılı güvenlik olayı (`contact: full/masked`), formül enjeksiyonu güvenli |

## 3. Personel iş akışı değişiklikleri

* **Siparişler / İptaller:** listede telefon `+90 *** *** ** 33`, adres `••••••` (il/ilçe görünür). Detayda
  "Teslimat Adresi" başlığındaki **Göster** tam bilgiyi açar (kayıt altına alınır). **Düzenle** butonu formu
  gerçek değerle açar (arka planda "Göster" çağrılır). Kargo etiketi, fatura, MNG kaydı, pazaryeri senkronu
  ETKİLENMEZ (sunucuda okunur).
* **Üyeler:** listede telefon maskeli; telefonla arama yalnız **tam numarayla** (kısmi rakam araması yok);
  üye kartında **Göster** telefonu ve adres defterini açar.
* **Influencerlar:** tablo/PR listesi maskeli; düzenleme formunda **Göster** gerçek değeri getirir; maskeli
  form değişmeden kaydedilirse değer korunur. Kargo etiketi / irsaliye artık `campaigns.influencers` ister.
  Excel'de tam telefon/adres için süper admin rol ekranından **"Influencer Excel'i — tam telefon/adres"**
  (`campaigns.influencers_export`) yetkisini vermelidir; vermeden dışa aktarım maskeli sütunlarla çalışır.
* **Personel yeni sipariş e-postası:** telefon son 2 hane, teslimat yalnız ilçe/il.
* **Toplu açma alarmı:** bir personel saatte 50'den fazla tam kişisel veri (IBAN/TCKN/VKN/telefon/adres)
  açarsa güvenlik uyarısı gider ve açık görüntüleme 1 saat kapanır.

## 4. Bilinçli olarak düz kalanlar (gerekçe)

* `orders.*` teslimat/fatura adresi: kargo, e-fatura, iyzico, pazaryeri akışları düz değer okur; şifreleme
  bu akışların tamamında çözme gerektirirdi (kesinti riski). Yerine: maskeleme + açma denetimi + saklama
  süresi minimizasyonu + log süzgeçleri.
* `iys_consents.phone`, `otp_verifications.phone`: İYS izin kanıtı (yasal).
* `sms_suppressions.phone`: gönderim engeli için tam numara gerekir.
* WhatsApp konuşmaları: müşteri hizmetleri kanalının anahtarı telefon numarasıdır (Meta tarafında da tutulur).
* Personel telefonu (`users.phone`, is_admin): MFA akışında `mfa_phone_enc` ile ayrıca şifreli; personel
  kayıtları müşteri telefon şifrelemesinin kapsamı dışında tutuldu (MFA/rol ekranları).
