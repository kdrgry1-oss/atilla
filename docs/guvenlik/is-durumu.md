# Güvenlik iş listesi — durum (BL-001 … BL-042)

4 Ekim 2026 · Build `8c34c16c` · Kaynak: güvenlik paketi `guvenlik-uygulama-isleri.json` (42 iş)

Bu sürüm, aynı gün build `c1f556b` ile yapılan ilk değerlendirmenin güncellemesidir. Arada iki güvenlik commit'i geldi:

- `d3a85c6`: bağımlılık açıkları kapatıldı (pip-audit 0 açık), CodeQL ve gitleaks iş akışı düzeltildi, test sırları için gitleaks istisnası.
- `8c34c16`: önceki 75 "kısmen" testin kod ve test işi. 736 uçtan uca güvenlik testi var, hepsi geçiyor.

Durum anlamları:

- **tamamlandı**: işin başarı ölçütü kodda karşılanıyor ve otomatik testle doğrulanıyor. Testi eksik kalan alt senaryolar ya da platformda yapılacak adımlar "Kalan iş" sütununda yazılı.
- **kısmen**: kontrolün bir bölümü var. Eksik kısım ve sahibi yazılı.
- **kod dışı**: kontrol platform konsolunda veya bir süreçte yaşıyor (Atlas, Railway, Cloudflare, tatbikat).
- **uygulanamaz**: özellik bu mimaride yok. Gerekçesi yazılı.

Sahip kısaltmaları: **Kodlama** (depoda kod veya test işi), **Kullanıcı-Platform** (işletme sahibinin konsolda ya da süreçte yapacağı iş).

Test kanıtlarının tam listesi `guvenlik-test-kayitlari.json` dosyasında. Bu tabloda test kimlikleri (T/AUTH/COM…) ve o kayıttaki durum yazılı.

## Özet

| Durum | Adet | İlk değerlendirme (`c1f556b`) |
|---|---|---|
| tamamlandı | 29 | 16 |
| kısmen | 9 | 22 |
| kod dışı | 3 | 3 |
| uygulanamaz | 1 | 1 |
| **Toplam** | **42** | **42** |

Bugünkü güvenlik commit'leri: `f3c8c45` `c5bcf80` `388b2e6` `b1936a9` `f3806ea` `33ac66d` `1870c1a` `5779a74` `cff2253` `cc40510` `0a3ce4f` `69ff6d7` `25a134b` `e7cedef` `00b1cb1` `0b23859` `3654ea8` `1b3a50c` (CI'da e2e seti) `ecb7e42` `d3a85c6` `8c34c16`. Önceki sertleştirme commit'leri: `50cdf7a` (29 Eylül), `1a2628b`, `5617bd3`, `f839afc`.

Kalan 9 "kısmen" işin 4'ünde (BL-018, BL-019, BL-026, BL-031) kalan iş yalnız işletme sahibinde: platform ayarı, sır rotasyonu ya da süreç kaydı. BL-021 ve BL-036'da önce sahibin adımı ya da onayı gerekiyor; ardından küçük bir Kodlama işi var. Ağırlıklı olarak Kodlama tarafında kalanlar BL-029 (görsel dönüştürmenin ayrı süreçte çalışması), BL-030 (CSP'nin zorlanması ve sürüm karantinası) ve BL-039 (arayüz testi).

## İşler

| İş | Ö. | Durum | Commit | Kanıt (test kaydı → durum) | Kalan iş → sahip |
|---|---|---|---|---|---|
| BL-001 Kimlik alanlarını ayır | P0 | tamamlandı | 1870c1a, 5779a74, e7cedef, 8c34c16 | AUTH01, AUTH02, AUTH03 geçti. Token `typ` (staff/customer), personel 12 sa, müşteri 7 gün; admin kapısı rolü DB'den yeniden okur; profil ucu ayrıcalık alanlarını yok sayar (testli) | Yok |
| BL-002 Admin route envanteri ve default deny | P0 | tamamlandı | 25a134b, 5779a74 | AUTH01 ve AUTH18 geçti. `test_route_guards.py` her /admin ve yazma rotasında yetki bağımlılığı arar; istisnalar gerekçeli allowlist'te | Yok (yeni herkese açık rota eklenirse test kırmızı olur) |
| BL-003 Panel personel MFA ve recovery | P0 | tamamlandı | 1870c1a, 5779a74, e7cedef, 8c34c16 | T23, AUTH16, AUTH24, IAM-03 geçti. Başka personelin şifre/MFA telefonu değişimi iki kişi onayına bağlı; kurtarma girişinden sonra 24 sa yüksek riskli işlem kapalı | Yalnız tek aktif süper admin varsa işlem hemen uygulanır (uyarı + `single_admin_exception`). MFA'lı ikinci süper admin tanımla → Kullanıcı-Platform (İ-04) |
| BL-004 OTP amaç, hesap ve hedef bağlama | P0 | tamamlandı | 1870c1a, 00b1cb1, 8c34c16 | AUTH06, AUTH07, AUTH08, AUTH09 geçti. Müşteri `/auth/forgot-password/*` akışı uçtan uca testli: eşzamanlı tek tüketim, amaç ve hesap bağlama, hedef değişince kod ölür | Yok |
| BL-005 OTP gönderim ve tahmin limitleri | P0 | tamamlandı | 1870c1a, 50cdf7a, 0b23859, 8c34c16 | AUTH10, AUTH11, AUTH19 geçti. Numara/hesap başına 24 sa'te 5 gönderim, hesap başına 10 hata, 60 sn aralık, süre dolumu; limit deposu yoksa fail-closed | Yok |
| BL-006 SMS maliyet bütçesi ve alarm | P0 | tamamlandı | 8c34c16 | AUTH19 ve CHAIN13 geçti. Küresel saatlik/günlük SMS tavanı (İşletme Kuralları `sms.global_hourly_cap` 300, `sms.global_daily_cap` 2000; yalnız süper admin), aşımda `sms_budget_exceeded` uyarısı, admin MFA için küçük yedek pay, sayaç deposu yoksa SMS yok | SMS sağlayıcı panelinde günlük harcama limiti ve bakiye uyarısı; uluslararası gönderim kapalı → Kullanıcı-Platform |
| BL-007 Reset token ve session iptali | P0 | tamamlandı | 1870c1a, 5617bd3, 8c34c16 | AUTH13 ve AUTH15 geçti. Link başlıklardan değil `SITE_URL`'den kurulur; sıfırlama eski oturumları ve bekleyen MFA biletini düşürür | Yok |
| BL-008 E-posta ve telefon faktör değişimi | P0 | tamamlandı | 1870c1a, 5779a74, 8c34c16 | AUTH09, AUTH20, AUTH24 geçti. Doğrulama yeni e-postaya taşınmaz; kurtarma sonrası 24 sa kısıt | Yok |
| BL-009 Personel offboarding ve revocation | P0 | tamamlandı | e7cedef, 00b1cb1, 8c34c16 | T12, AUTH05, IAM-02, ADM-02 geçti. Pasifleştirme ve rol silme bir sonraki istekte etkili; zamanlanmış işler denetimde `system:<iş>` kimliğiyle | Otomasyon kimliklerini 3 ayda bir gözden geçir → Kullanıcı-Platform |
| BL-010 Nesne, tenant ve alan yetki matrisi | P0 | tamamlandı | cff2253, c5bcf80, 5779a74, 8c34c16 | T01, T02, PAY-01, IAM-01 geçti (3589333a: tüm-uygulama yetki matrisi, 4 açık kapatıldı). Çapraz müşteri sipariş erişimi bütün uçlarda testli; 29 kritik uç için üretilmiş izin matrisi (izinsiz 403 + yan etki yok, izinli + denetim, fail-closed) | 735 uç 'herhangi bir personel' politikasında (uç başına izin ürün kararı). Tenant uygulanamaz |
| BL-011 Provider özel callback adapter | P0 | tamamlandı | f3806ea, cff2253 | T04 ve T05 geçti. iyzico sonucu her zaman ödeme-detay sorgusu, tutar ve sipariş eşleşmesiyle doğrulanır; dönüş ve callback adresinde ayrıştırıcı hileleri reddedilir | PayTR yok (uygulanamaz) |
| BL-012 Inbox ve iş etkisi dedup | P0 | tamamlandı | f3806ea, 8c34c16 | T03, T06, T07, CHAIN07 geçti. Atomik koşullu "paid" geçişi, replay kalkanı, süpürme işi; kritik olmayan denetim kaydı için outbox | Ayrı inbox tablosu yok; tekillik durum geçişiyle sağlanıyor (ADR-04) |
| BL-013 Ödeme belirsizliği mutabakat | P0 | tamamlandı | f3806ea, 8c34c16 | COM06, PAY-05 geçti; COM12 kısmen. Zaman aşımı "belirsiz" sayılır; 15 dk'lık mutabakat cron'u; günlük salt-okunur iyzico mutabakatı (04:23 UTC, son 3 gün) | Komisyon/settlement defteri yok; aylık iyzico hesap özeti mutabakatı elle (el kitabı §5) → Kullanıcı-Platform |
| BL-014 İade rezervasyonu ve onay | P0 | tamamlandı | f3806ea, cff2253, 8c34c16 | T08 ve COM10 geçti. Eşzamanlı iadede tek iyzico çağrısı; belirsiz iade yeni iadeyi kilitler; aynı kalem için eşzamanlı iade talebi tek kayıt | Yok |
| BL-015 Kart veri akışı ve log redaksiyonu | P0 | tamamlandı | c5bcf80, 25a134b, 8c34c16 | T14, PRIV01, AUTH23 geçti. Kanarya kart numarası ve CVC yalnız iyzico isteğine ulaşır; hiçbir koleksiyonda, ham logda ve yanıtta yok | Kart verisi hâlâ backend'den geçerek iyzico 3DS'e iletiliyor. iyzico Checkout Form'a (barındırılan form) geçiş, PCI kapsamını daraltır → Kullanıcı-Platform kararı |
| BL-016 XML parser ve egress izolasyonu | P0 | tamamlandı | 25a134b, b1936a9, c5bcf80, 8c34c16 | T09, T10, T11 geçti. XXE reddi, IP'ye sabitlenmiş indirme, bayt tavanı; ayrıştırma ayrı iş parçacığında derinlik, öğe ve süre bütçesiyle | Ayrı süreç izolasyonu (ADR-05) ertelendi → Kodlama, düşük öncelik |
| BL-017 VDS, provider, DNS ve SSH erişimleri | P0 | kod dışı | — | T15 kod dışı. VDS yok; Railway, Atlas, Cloudflare ve GitHub hesapları var | Tüm konsollarda MFA; ayrı Atlas DB kullanıcısı; Cloudflare DNS kilidi → Kullanıcı-Platform (README §4) |
| BL-018 DB ve migration runtime ayrımı | P0 | kısmen | f3806ea, 00b1cb1, 8c34c16 | COM03, COM07 geçti. Kod tarafı tamam: atomik stok düşümü, atomik kupon hakkı rezervasyonu, hediye çeki; idempotency, kupon ve redemption tekil indeksleri | Uygulama DB kullanıcısı yalnız `readWrite` olmalı; DDL/admin yetkisi olmamalı → Kullanıcı-Platform |
| BL-019 Sır envanteri, vault ve iptal | P0 | kısmen | e7cedef, 0a3ce4f, 25a134b, 8c34c16 | T14, ADM-10, PRIV11 geçti. Sırlar şifreli; açık değer kritik uyarı ve toplu açma alarmı üretir; döndürülen anahtar bir sonraki çağrıda geçerli | Ortam değişkenlerini tanımla (README §4); rotasyon takvimi ve sahibini kaydet; **git geçmişindeki 5 gerçek sırrı sağlayıcı panelinde döndür** (gitleaks bunlar yüzünden kırmızı, bkz. BL-036) → Kullanıcı-Platform |
| BL-020 Denetim kalıcı kayıt ve alarm | P0 | tamamlandı | e7cedef, 3654ea8, 8c34c16 | ADM-12, SOC-02 geçti; SOC-01 kısmen. Hash zinciri, R2 arşivi, fail-closed kritik işlemler, denetim outbox'ı, 5 dk'lık güvenlik hattı kontrolü, yedek mail kanalı, günlük öz-test | 15 dk insan teyidi tatbikatı → Kullanıcı-Platform |
| BL-021 Yedek ve temiz restore | P0 | kısmen | e7cedef, 00b1cb1, 8c34c16 | CHAIN12, ADM-11, CHAIN08 geçti; DR-01, DR-02 kod dışı. `restore_mode` kapanınca oturum dönemi, KVKK silmeleri, pazarlama retleri ve ayrılmış personel iptali otomatik yeniden uygulanır; öz-test arşiv referanslarını denetler | Atlas restore tatbikatı ve RPO/RTO ölçümü → Kullanıcı-Platform. (CHAIN08 `280b0c20`: `restore_mode` bütün iade uçlarını kapatır; kapanınca restore penceresi iyzico mutabakatı otomatik çalışır, temiz bitene kadar iade kilidi kalır) |
| BL-022 Fiyat, quote, vergi ve kur politikası | P1 | tamamlandı | cff2253, cc40510, 8c34c16 | COM02, COM06 geçti. Fiyat her siparişte sunucuda; aşırı ondalık kuruşa yuvarlanır, toplam = iyzico = fatura denkliği ve KDV ayrımı testli | Yok |
| BL-023 Kupon, bakiye ve sadakat ledger | P1 | tamamlandı | f3806ea, 00b1cb1, 8c34c16 | COM03, COM04, COM16 geçti. Kupon başına sayaçla son hak tek kez; iptal ve süpürme hakkı bir kez geri verir; başarısız ödeme kupon yakmaz | Yok |
| BL-024 Stok feed snapshot, delta ve fencing | P1 | tamamlandı | b1936a9, 8c34c16 | COM15, CHAIN05, FIN-E04 geçti. İçe aktarma manifesti, %40 düşüş ve bayat/yeniden oynatılmış feed reddi, etkilenen ürün raporu, kilit fencing belirteci | Yok |
| BL-025 Sevk adresi ve etiket sürümü | P1 | tamamlandı | 25a134b, e7cedef, 8c34c16 | COM08, COM09, FIN-E06 geçti. Durum geçişleri karşılaştır-ve-ayarla; etiket kilidi; etiketten sonra adres değişmez; kayıp etiket MNG'den bağlanır | Yok |
| BL-026 Chargeback ve settlement ledger | P1 | kısmen | 25a134b, 00b1cb1, 8c34c16 | COM11, FIN-E03 geçti; COM12 kısmen. Açık itirazda iade 409 (süper admin gerekçeli istisna, denetimli) | Settlement/komisyon defteri yok; aylık iyzico hesap özeti mutabakatı elle → Kullanıcı-Platform (istenirse settlement raporu içe aktarma → Kodlama) |
| BL-027 Personel daveti ve iki kişi onayı | P1 | tamamlandı | e7cedef, 8c34c16 | ADM-01, ADM-03 geçti. Özetli davet, 48 sa, süper admin daveti onaylı, eşzamanlı kabul tek hesap | Yok |
| BL-028 Export, bulk ve impersonation | P1 | tamamlandı | b1936a9, 00b1cb1 | ADM-06 geçti; ADM-05 uygulanamaz. Her export yetki, denetim, güvenlik olayı ve formül kaçışı ile; `freeze_exports` hepsini kapatır; kurtarma girişinden sonra 24 sa export yok | — (CHAIN09 `280b0c20` ile geçti: süren export yanıtı erişim iptalinde parçalar arasında kesiliyor; İ-16 kapandı) |
| BL-029 Medya upload ve converter izolasyonu | P1 | tamamlandı | b1936a9, c5bcf80, 8c34c16, 3a30838 | ADM-07 geçti (ayrı süreç, çökme testi); CHAIN10 geçti. İçerik koklama, SVG reddi, WebP; başlıktan piksel ön kontrolü (sıkıştırma bombası çözülmez); sınırlı havuz + görev zaman aşımı; yük altında checkout testli | — |
| BL-030 CMS, tema ve JS yayın kontrolü | P1 | kısmen | b1936a9, 5617bd3, 8c34c16, 3a30838 | ADM-08 geçti (karantina); T19 kısmen. Vitrin bütünlük izleyicisi (15 dk, sunulan paket sha256 + CSP + host); Yalnız süper admin; her değişiklik uyarı; script host manifesti ve yeni host uyarısı; DB'den doğrudan değişiklik öz-testte; CSP ihlal raporu ucu | CSP'yi nonce/hash ile zorlayıcı moda alma ve `unsafe-inline`'ı kaldırma → Kodlama. Report-Only raporlarını 2–4 hafta izleyip zorlama kararı → Kullanıcı-Platform (İ-15) |
| BL-031 API key scope, rotasyon ve uninstall | P1 | kısmen | e7cedef, 8c34c16 | ADM-10, PRIV11 geçti. Sistem dışarıya anahtar vermiyor; döndürülen/silinen sağlayıcı anahtarı bir sonraki çağrıda etkili | Sağlayıcı anahtarlarının rotasyon kaydı ve takvimi → Kullanıcı-Platform |
| BL-032 Cache ve tarayıcı offline ayrımı | P1 | tamamlandı | c5bcf80 | T22 geçti; SHOP11 uygulanamaz. Kişisel veri dönen uçlarda `no-store` | Yok |
| BL-033 İYS ret ve kampanya kuyruğu | P1 | tamamlandı | b1936a9, 25a134b, 8c34c16 | PRIV05, ADM-13 geçti. Ret listesi her gece gizli arşive (yalnız HMAC'li e-posta); geri yükleme kapanışında yeniden uygulanır; sonraki yeni onay korunur | Yok |
| BL-034 Veri talepleri, imha ve restore | P1 | tamamlandı | e7cedef, 00b1cb1, 8c34c16 | PRIV02, PRIV03, PRIV04 geçti. KVKK self-servis; mezar taşları R2'de, gece ve restore kapanışında yeniden uygulanır | Yok |
| BL-035 Kargo ve fatura partner mutabakatı | P1 | tamamlandı | 33ac66d, 69ff6d7, 8c34c16 | COM09, COM14, FIN-E06, PRIV09 geçti. Tek etiket, kayıp etiket bağlama, Doğan'da tek belge; kargo yükü minimum | Aylık Doğan/MNG/DHL mutabakatı elle (el kitabı §5) → Kullanıcı-Platform |
| BL-036 CI, artifact ve bağımlılık güveni | P1 | kısmen | 25a134b, 1b3a50c, d3a85c6 | SDLC-02 kısmen; SDLC-01 ve ADM-09 kod dışı. SHA'ya pinli action'lar; pip-audit **0 açık** (fastapi/starlette, cryptography, pypdf, pymongo vb. yamalandı; kullanılmayan litellm kaldırıldı); e2e seti CI'da | (1) **gitleaks kırmızı**: git geçmişindeki 5 gerçek sır döndürülüp `.gitleaksignore`'da PENDING-ROTATION öneki kaldırılmalı. (2) **yarn audit kırmızı**: frontend `yarn.lock`'ta axios, react-router ve form-data güncellemesi sahibin onayını bekliyor (CLAUDE.md: paket kurulumu onaylı yapılır). (3) Railway "Wait for CI" ve GitHub branch protection → Kullanıcı-Platform |
| BL-037 Tenant kota ve kaynak tüketimi | P1 | uygulanamaz | d635c4e, 8c34c16 | T18, SHOP02 kısmen. Tek mağaza. CI'da 20 eşzamanlı pahalı istek altında checkout testi; ölçüm aracı `backend/tests/load/checkout_latency.py` (CI dışı) | Hazırlık ortamında p95 ölçümü (G13) → Kullanıcı-Platform (ortam ve onay) + Kodlama (ölçüm) |
| BL-038 Sosyal giriş bağlama | P1 | tamamlandı | 1870c1a, 1a2628b | ADM-14 geçti. Google `sub` bağlama; personel Google'la giremez | Yok |
| BL-039 Müşteri ekranları erişim testleri | P1 | kısmen | 5779a74, ecb7e42, 8c34c16 | SHOP01, SHOP04, SHOP06, SHOP07, SHOP10 geçti; SHOP12 kısmen. Arama, paylaşılan sepet, adres ID ve anlık görüntü testli | Dar ekran/klavye ve ağ kesintisi için tarayıcı tabanlı arayüz testi; yeni test paketi için onay gerekli → Kodlama (+ Kullanıcı onayı) (İ-17) |
| BL-040 Alan şifreleme ve anahtar ayrımı | P1 | tamamlandı | 00b1cb1, e7cedef, 0a3ce4f, 8c34c16 | PRIV06, PRIV07 geçti. TCKN/VKN, IBAN, dekont, MFA ve sırlar Fernet ile; arama HMAC ile; toplu çözme alarmı (saatte 50, aşımda 1 sa engel) | `PII_HASH_SECRET` ve `KVKK_TOMBSTONE_SECRET` env → Kullanıcı-Platform |
| BL-041 Bağımsız güvenlik değerlendirmesi | P2 | kod dışı | — | IAM-01 geçti; COM16 geçti | Bağımsız sızma testi (ödeme, panel, KVKK) → Kullanıcı-Platform |
| BL-042 Olay tatbikatı ve risk kabul döngüsü | P2 | kod dışı | — | SOC-01 kısmen; DR-01 kod dışı | El kitabıyla 3 ayda bir tatbikat; istisna kaydını süresinde yenile → Kullanıcı-Platform |

## Öncelikli kalan işler

1. **BL-019 / BL-036: geçmişteki 5 gerçek sırrın rotasyonu.** gitleaks bu yüzden kırmızı. Her sağlayıcı panelinde anahtarı döndür ve eskisinin geçersiz olduğunu doğrula. Sonra `.gitleaksignore`'daki ilgili satırın `PENDING-ROTATION` önekini kaldırıp rotasyon tarihini yaz. Sahip: Kullanıcı-Platform.
2. **BL-036: frontend bağımlılık güncellemesi onayı.** yarn audit, `yarn.lock`'taki axios, react-router ve form-data sürümleri yüzünden kırmızı. Onay verilirse güncelleme yapılır ve `CI=true` build ile doğrulanır. Sahip: Kullanıcı (onay) + Kodlama.
3. **BL-017 / BL-018 / BL-021 / BL-036: platform adımları** (README §4): Atlas ayrı kullanıcı, yedek ve MFA; Railway "Wait for CI"; branch protection; restore tatbikatı. Sahip: Kullanıcı-Platform.
4. **BL-003: ikinci süper admin.** Tek aktif süper admin varken iki kişi kuralı uygulanamaz. Sahip: Kullanıcı-Platform.
5. **BL-030: CSP'yi zorlayıcı moda alma.** Önce Report-Only raporları izlenir (Güvenlik → CSP raporları). Sahip: Kodlama + Kullanıcı kararı.
6. **BL-006: SMS sağlayıcı panelinde harcama limiti.** Uygulama içi küresel tavan artık var; sağlayıcı limiti ikinci savunma hattıdır. Sahip: Kullanıcı-Platform.
