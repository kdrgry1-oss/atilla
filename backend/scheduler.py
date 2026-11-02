"""
Application-level background scheduler (APScheduler).
Runs auto-cancellation of unpaid orders, etc.
"""
import os
import asyncio
import time
import logging
from datetime import datetime, timezone, timedelta
import uuid
import contextvars

from apscheduler.schedulers.asyncio import AsyncIOScheduler


# Periyodik stok/fiyat senkronları için HAFİF ürün projeksiyonu: fiyat/stok/SKU mantığının
# kullanmadığı büyük alanlar hariç (görseller, açıklamalar, Ticimax ham alanları, SEO metinleri).
_LIGHT_PRODUCT_PROJ = {"_id": 0, "images": 0, "description": 0, "short_description": 0,
                       "long_description": 0, "ticimax_fields": 0, "seo_description": 0,
                       "meta_description": 0, "size_table_html": 0, "video_url": 0, "videos": 0}

# Stok/fiyat PUSH senkronları için: hafif projeksiyon + pazaryeri ÖZELLİK haritaları da hariç
# (ürün belgesinin ~%60'ı; stok/fiyat/SKU üretimi bunları kullanmaz). Atlas veri transferi maliyeti:
# bu işler 1–3 dk'da bir TÜM aktif ürünleri okuyordu.
_STOCK_SYNC_PROJ = {**_LIGHT_PRODUCT_PROJ, "trendyol_attributes": 0, "hepsiburada_attributes": 0,
                    "temu_attributes": 0, "trendyol_category_attributes": 0}

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


HAVALE_METHOD_RX = {"$regex": r"^(transfer|havale|bank_transfer|eft|havale_eft|banka_havale|bank|banka|havale/eft|havale_or_eft)$",
                    "$options": "i"}


# Geçmişe dönük alt sınır (kullanıcı kararı): 1 Haziran 2026'dan itibaren tüm ödenmemiş havale
# siparişleri kural kapsamında; daha eskilere dokunulmaz.
HAVALE_SWEEP_SINCE = "2026-06-01"
# Kapsam (kullanıcı kararı): 'Ödeme Bildirimi' (payment_notified) durumundakiler de — personel
# 72 saat içinde ödemeyi ONAYLAMADIYSA — otomatik iptal edilir.
HAVALE_UNPAID_STATUSES = ["pending", "awaiting_payment", "payment_notified"]


def havale_unpaid_query(cutoff_dt):
    """Süresi dolmuş ÖDENMEMİŞ/ONAYLANMAMIŞ havale/EFT siparişleri (geçmişe dönük dahil).
    created_at hem ISO string hem datetime olarak saklanmış olabilir → ikisini de yakala;
    payment_method varyantları büyük/küçük harften bağımsız."""
    from datetime import datetime as _dt
    cutoff_iso = cutoff_dt.isoformat()
    since_dt = _dt.fromisoformat(HAVALE_SWEEP_SINCE).replace(tzinfo=timezone.utc)
    return {
        "payment_status": {"$nin": ["paid", "expired", "refunded"]},
        "status": {"$in": HAVALE_UNPAID_STATUSES},
        "payment_method": HAVALE_METHOD_RX,
        "platform": {"$nin": ["trendyol", "hepsiburada", "amazon", "n11", "etsy"]},
        "$or": [{"created_at": {"$lt": cutoff_iso, "$gte": HAVALE_SWEEP_SINCE}},
                {"created_at": {"$lt": cutoff_dt, "$gte": since_dt}}],
    }


async def auto_cancel_unpaid_havale_orders(limit: int = 0):
    """Cancel havale/transfer orders that remain unpaid after 72 hours and restock.
    Geçmişe dönük çalışır: kuralı aşan TÜM bekleyen havale siparişleri (kaç günlük olursa olsun)."""
    from routes.deps import db  # lazy import
    from routes.orders import _restock_order_once
    import business_rules as _BR

    cancelled = 0
    _started = datetime.now(timezone.utc)
    _matched = 0
    _err = ""
    try:
        # AYAR: süre admin panelinden (İşletme Kuralları) yönetilir; varsayılan 72 saat.
        _hrs = int(await _BR.get_rule(db, "order.havale_cancel_hours", 72) or 72)
        cutoff_dt = datetime.now(timezone.utc) - timedelta(hours=_hrs)
        query = havale_unpaid_query(cutoff_dt)
        try:
            _matched = await db.orders.count_documents(query)
        except Exception:
            _matched = -1
        cur = db.orders.find(query, {"_id": 0})
        if limit:
            cur = cur.limit(limit)
        async for order in cur:
            if not await _still_leader():  # fencing: liderlik kaybedildiyse yazma yapma
                break
            try:
                # O16: Önce durumu güncelle, SONRA idempotent iade yap.
                # TOCTOU koruması (D1 fix): sorgu ile update arasında müşteri ödemiş/dekont
                # bildirmiş olabilir. Filtreye payment_status != paid + status re-check ekle;
                # matched_count 0 ise (arada ödendi/durum değişti) İPTAL DE STOK GERİ DE YAPMA
                # → "72. saatte ödeyen müşterinin siparişi iptal edilip stoğu geri eklenmesi" biter.
                res = await db.orders.update_one(
                    {"id": order["id"],
                     "payment_status": {"$nin": ["paid", "expired", "refunded"]},
                     "status": {"$in": HAVALE_UNPAID_STATUSES}},
                    {"$set": {
                        "status": "cancelled",
                        "payment_status": "expired",
                        "cancel_reason": (f"{_hrs} saat içinde havale ödemesi onaylanmadı (otomatik iptal)"
                                          if order.get("status") == "payment_notified"
                                          else f"{_hrs} saat içinde havale ödemesi yapılmadı (otomatik iptal)"),
                        "auto_cancelled": True,
                        "cancelled_at": datetime.now(timezone.utc).isoformat(),
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }}
                )
                if res.matched_count == 0:
                    continue  # arada ödendi / dekont bildirildi / durum değişti — dokunma
                await _restock_order_once(order, "havale_auto_cancel")
                cancelled += 1
                # Müşteriye bildirim: "Siparişiniz ödeme yapılmadığı için iptal edildi" (SMS+e-posta).
                # Standart 'order_cancelled' event'i (Ayarlar → Bildirimler → 'Sipariş İptal Edildi').
                try:
                    from notification_service import send_notification
                    from routes.orders import _order_notify_vars
                    _addr = order.get("shipping_address") or {}
                    _phone = _addr.get("phone") or order.get("phone")
                    _email = _addr.get("email") or order.get("email")
                    _ch = None
                    _label = "İptal Edildi"
                    try:
                        from order_statuses import get_status_config, customer_label_for
                        _cfg = await get_status_config(db)
                        _nz = (_cfg.get("notify") or {}).get("cancelled") or {}
                        _ch = [c for c in ("sms", "email") if _nz.get(c)] or None
                        _label = customer_label_for("cancelled")
                    except Exception:
                        pass
                    if _ch is None:
                        _ch = (["email"] if _email else []) + (["sms"] if _phone else [])
                    _vars = await _order_notify_vars(
                        order, status_label=_label,
                        cancel_reason="72 saat içinde havale ödemesi yapılmadı",
                    )
                    if _ch:
                        await send_notification(
                            db, "order_cancelled",
                            to_phone=_phone, to_email=_email,
                            variables=_vars, channels=_ch,
                        )
                except Exception as _e_notif:
                    logger.warning(f"[scheduler] havale-cancel notif failed for {order.get('order_number')}: {_e_notif}")
            except Exception as e_item:
                logger.error(f"Failed to cancel order {order.get('order_number')}: {e_item}")
        if cancelled:
            logger.info(f"[scheduler] Auto-cancelled {cancelled} unpaid havale orders (>{_hrs}h)")
    except Exception as e:
        _err = str(e)[:300]
        logger.exception(f"[scheduler] auto_cancel_unpaid_havale_orders failed: {e}")
    # Sağlık kaydı (teşhis): son çalışma, eşleşen aday, iptal edilen, hata → önizleme ucunda görünür.
    try:
        await db.settings.update_one(
            {"id": "havale_sweep_health"},
            {"$set": {"id": "havale_sweep_health", "last_run_at": _started.isoformat(),
                      "last_finish_at": datetime.now(timezone.utc).isoformat(),
                      "matched": _matched, "cancelled": cancelled, "error": _err}},
            upsert=True)
    except Exception:
        pass
    return cancelled


async def expire_stale_site_return_requests():
    """Süresi dolan SİTE iade taleplerini kapatır ve siparişi NORMALE döndürür.

    Sorun: müşteri siteden iade talebi açınca sipariş "return_requested"a geçiyor ve
    iade sayfasında duruyor. Müşteri ürünü hiç kargoya vermezse kayıt orada ASILI
    kalıyordu — iade hakkı süresi dolduğu hâlde satış "iade" gibi görünüyor, ciroya
    girmiyor, sipariş listesinde çıkmıyordu.
    Mevcut mekanizma yetersizdi: scheduler'daki kargo yoklaması iade KAYDINI "expired"
    yapıyor ama SİPARİŞE HİÇ DOKUNMUYOR (order.status "return_requested" kalıyor);
    üstelik yalnız MNG entegrasyonu AÇIKSA ve kayıt 45 günden yeniyse çalışıyor.

    Bu iş: kargoya HİÇ VERİLMEMİŞ ve süresi dolmuş talepleri kapatır, siparişi iade
    öncesi durumuna (prev_status, yoksa "delivered") döndürür. Böylece satış tamamlanmış
    normal sipariş olarak değerlendirilir — panelde sipariş listesinde görünür, ciroya
    girer, iade sayfasından düşer.

    PARAYA DOKUNMAZ: tahsilat, iade tutarı, stok ve fatura alanları DEĞİŞMEZ. Kargoya
    verilmiş (in_transit/received/...), onaylanmış ya da parası iade edilmiş hiçbir kayıt
    bu işten ETKİLENMEZ. Durum güncellemesi koşulludur (TOCTOU: yalnız hâlâ
    return_requested ise) ve idempotenttir."""
    from routes.deps import db  # lazy import
    import business_rules as _BR
    try:
        _days = int(await _BR.get_rule(db, "return.stale_request_days", 14) or 14)
        if _days <= 0:
            return
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(days=_days)).isoformat()
        now_iso = now.isoformat()
        # Kargoya verilmemiş sayılan iade kayıt durumları
        _PRE_SHIP = ["created", "expired"]
        # İlerlemiş (asla dokunulmayacak) durumlar — aynı siparişte biri varsa atlanır
        _PROGRESSED = ["in_transit", "received", "returned", "approved",
                       "refunded", "partial_refunded", "rejected"]
        # Siparişin geri döndürülebileceği durumlar. DİKKAT: "completed" çekirdek durum
        # kataloğunda YOK (order_statuses.py) — oraya döndürmek panelde tanınmayan durum
        # bırakırdı. Katalogda bulunan tamamlanmış-satış durumları kullanılır; başka her
        # şeyde iade-iptal akışının kullandığı güvenli varsayılana ("delivered") düşülür.
        _RESTORE_OK = ("delivered", "shipped", "undelivered")
        kapanan, atlanan = 0, 0
        cur = db.customer_returns.find(
            {"status": {"$in": _PRE_SHIP}, "created_at": {"$lt": cutoff}},
            {"_id": 0, "id": 1, "order_id": 1, "order_number": 1, "status": 1,
             "created_at": 1, "approval": 1, "refund_amount": 1})
        async for rec in cur:
            if not await _still_leader():  # fencing: liderlik kaybedildiyse yazma yapma
                break
            try:
                o = await db.orders.find_one(
                    {"id": rec.get("order_id")},
                    {"_id": 0, "id": 1, "order_number": 1, "status": 1, "payment_status": 1,
                     "return_request": 1, "refund_paid_at": 1})
                if not o or o.get("status") != "return_requested":
                    continue                      # sipariş başka duruma geçmiş → DOKUNMA
                # Para hareketi olmuş mu? (onay / iade tutarı / iade ödemesi)
                if (rec.get("approval") or rec.get("refund_amount")
                        or o.get("refund_paid_at")
                        or str(o.get("payment_status") or "") in ("refunded", "partial_refunded")):
                    atlanan += 1
                    continue
                # Aynı siparişte İLERLEMİŞ başka bir iade kaydı var mı?
                if await db.customer_returns.find_one(
                        {"order_id": rec.get("order_id"), "status": {"$in": _PROGRESSED}},
                        {"_id": 0, "id": 1}):
                    atlanan += 1
                    continue
                _prev = str((o.get("return_request") or {}).get("prev_status") or "").strip()
                if _prev not in _RESTORE_OK:
                    _prev = "delivered"
                await db.customer_returns.update_one(
                    {"id": rec["id"]},
                    {"$set": {"status": "expired", "expired_at": now_iso,
                              "expire_reason": f"İade süresi doldu ({_days} gün) — ürün kargoya verilmedi",
                              "updated_at": now_iso}})
                # TOCTOU: yalnız HÂLÂ return_requested ise geri al (arada personel dokunduysa dokunma)
                r = await db.orders.update_one(
                    {"id": o["id"], "status": "return_requested"},
                    {"$set": {"status": _prev, "return_request.status": "expired",
                              "return_request.expired_at": now_iso, "updated_at": now_iso}})
                if r.modified_count:
                    kapanan += 1
                    try:
                        from routes.orders import _log_order_event
                        await _log_order_event(
                            o["id"], "return",
                            f"İade süresi doldu ({_days} gün) — ürün kargoya verilmedi; "
                            f"sipariş '{_prev}' durumuna döndürüldü",
                            {"email": "sistem"}, {"return_id": rec.get("id")},
                            order_number=o.get("order_number", ""))
                    except Exception:
                        pass
            except Exception as _ie:
                logger.warning(f"[iade-süresi] kayıt atlandı ({rec.get('id')}): {_ie}")
        if kapanan or atlanan:
            logger.warning(f"[iade-süresi] normale döndürülen sipariş={kapanan} atlanan={atlanan} "
                           f"(sınır {_days} gün)")
    except Exception as e:
        logger.exception(f"[scheduler] expire_stale_site_return_requests failed: {e}")


async def auto_cancel_unpaid_card_orders():
    """Başarısız/ödenmemiş KART siparişlerini 24 saat sonra iptal edip stoğu geri ekler.

    KRİTİK: Bu iş zamanlanmamıştı → başarısız kart ödemeleri (iyzico 3DS reddi, yarıda kalan
    ödeme) create_order'da düşürülen stoğu KALICI sızdırıyordu (ürünler yanlışlıkla tükeniyordu).
    COD/havale HARİÇ (meşru şekilde bekler + kendi akışları var). Restock idempotenttir
    (_restock_order_once — 'auto_cancel_expired' hareketi bir kez eklenir)."""
    from routes.deps import db  # lazy import
    from routes.orders import _restock_order_once
    import business_rules as _BR
    try:
        # AYAR (varsayılan 3 saat): 3DS başlatılıp ödenmeyen kart siparişleri fazla beklemesin.
        # Güvenli: webhook (saniyeler) + reconcile (her 15dk) gerçekten çekilen ödemeyi bu süreden
        # ÇOK önce 'paid' yapar. Süre admin panelinden (İşletme Kuralları) yönetilir.
        # SÜRE (kullanıcı isteği): yarıda kalan kart siparişi stoğu UZUN SÜRE TUTMASIN.
        # Dakika bazlı kural birincil; 0/boş ise eski saat bazlı ayar kullanılır.
        _ucm = int(await _BR.get_rule(db, "order.unpaid_card_cancel_minutes", 45) or 0)
        if _ucm <= 0:
            _ucm = int(await _BR.get_rule(db, "order.unpaid_card_cancel_hours", 3) or 3) * 60
        # Hiç ödeme denemesi OLMAYAN (iyzico kaydı oluşmamış) siparişler daha kısa sürede
        # kapanır: doğrulanacak bir tahsilat yok, stoğu boşuna tutmanın anlamı yok.
        _abm = int(await _BR.get_rule(db, "order.abandoned_card_cancel_minutes", 15) or 0)
        if _abm <= 0 or _abm > _ucm:
            _abm = _ucm
        _now_dt = datetime.now(timezone.utc)
        cutoff = (_now_dt - timedelta(minutes=_ucm)).isoformat()
        cutoff_abandoned = (_now_dt - timedelta(minutes=_abm)).isoformat()
        _cod_bank = ["cash_on_delivery", "kapida", "kapida_odeme", "cod",
                     "bank_transfer", "havale", "eft", "havale_eft", "banka_havale", "transfer"]
        # İki grup: (a) ödeme denemesi VAR (iyzico kaydı oluşmuş) → tam süre beklenir,
        # (b) hiç deneme YOK → kısa süre. (b)'de doğrulanacak tahsilat olmadığından stok
        # erken serbest bırakılır (kullanıcı isteği: yarıda kalan sipariş stoğu tutmasın).
        _no_attempt = {
            "$and": [
                {"$or": [{"iyzico_payment_id": {"$in": [None, ""]}}, {"iyzico_payment_id": {"$exists": False}}]},
                {"$or": [{"payment_id": {"$in": [None, ""]}}, {"payment_id": {"$exists": False}}]},
                {"$or": [{"reconcile_payment_id": {"$in": [None, ""]}}, {"reconcile_payment_id": {"$exists": False}}]},
                {"needs_reconciliation": {"$ne": True}},
            ]
        }
        query = {
            "payment_status": {"$in": ["pending", "failed"]},
            # cancel_requested: ödemesi tamamlanmamış kart siparişinde müşteri hesabından iptal
            # isteyince durum 'awaiting_payment'tan çıkıyor ve bu süpürgeden KAÇIYORDU → stok süresiz
            # rezerve, panelde ödenmiş gibi duran mor satır (W12767). Para hiç alınmadığından aynı
            # 'Ödeme Alınamadı' kapanışı uygulanır (ödenmiş iptal talepleri payment_status=paid → hariç).
            "status": {"$in": ["pending", "awaiting_payment", "cancel_requested"]},
            # DENETİM (2026-09-29): pazaryeri siparişi (Amazon 'Pending', HB bilinmeyen statü →
            # pending/pending) iyzico kaydı olmadığından bu süpürgeye takılıp iptal + stok iadesi
            # alıyordu (oversell). Yalnız SİTE kart siparişleri süpürülür.
            "payment_method": {"$nin": _cod_bank + ["marketplace"]},
            "platform": {"$nin": ["trendyol", "hepsiburada", "amazon", "temu", "n11"]},
            "$or": [
                {"created_at": {"$lt": cutoff}},
                {"$and": [{"created_at": {"$lt": cutoff_abandoned}}, _no_attempt]},
            ],
        }
        cancelled = 0
        async for order in db.orders.find(query, {"_id": 0}):
            if not await _still_leader():  # fencing: liderlik kaybedildiyse yazma yapma
                break
            try:
                # TOCTOU koruması: sorgu ile update arasında ödeme onaylanmış olabilir.
                # Filtreye payment_status != paid ekle → ödenmiş sipariş iptal/geri stok
                # EDİLMESİN. matched_count 0 ise (ödenmiş) restock da yapılmaz.
                # DENETİM (2026-09-29, Y3): iyzico'da ödeme kaydı belirsiz (needs_reconciliation)
                # olan sipariş kapatılır ve stok serbest kalır; ANCAK mutabakat bayrağı SİLİNMEZ ve
                # "para alınmadı" denmez — para çekilmiş olabilir, reconcile cron'u 7 gün boyunca
                # doğrulamaya devam eder (çekildiyse sipariş paid olur, stok yeniden düşer).
                _unsure = bool(order.get("needs_reconciliation"))
                _upd_extra = {"$unset": {"needs_reconciliation": ""}} if not _unsure else {}
                res = await db.orders.update_one(
                    {"id": order["id"], "payment_status": {"$nin": ["paid", "refunded"]},
                     "status": {"$in": ["pending", "awaiting_payment", "cancel_requested"]}},
                    {**_upd_extra, "$set": {
                        # status="cancelled" DEĞİL "payment_failed": bu siparişin parası HİÇ alınmadı.
                        # "İptal Edildi" yazılırsa ekip "ödeme alınmıştı" sanıp yanlışlıkla PARA İADESİ
                        # yapıyordu. "payment_failed" = Ödeme Alınamadı → iade GEREKMEZ, ana listede
                        # görünmez. (Gerçekten para çekilmiş olsaydı reconcile job 24s dolmadan
                        # 'paid' yapardı; bu guard paid'i zaten atlıyor.)
                        "status": "payment_failed",
                        "payment_status": "expired",
                        # Kapatılan siparişi mutabakat havuzundan çıkar: ödeme gerçekten
                        # çekilmediği (birkaç mutabakat turu boyunca SUCCESS gelmediği) için
                        # 7 gün boyunca 15 dakikada bir iyzico'ya sormanın anlamı yok.
                        # Geç ortaya çıkan gerçek tahsilat webhook ile yine yakalanır.
                        "reconcile_stopped_at": datetime.now(timezone.utc).isoformat(),
                        "cancel_reason": ("Ödeme doğrulanamadı — iyzico kaydı belirsiz, sistem 7 gün kontrol "
                                          "etmeye devam ediyor. İade yapmadan önce iyzico panelinden kontrol edin."
                                          if _unsure else
                                          "Ödeme süresinde tamamlanmadı — para HİÇ alınmadı (iade gerekmez)"),
                        "auto_cancelled": True,
                        # Denetim #2: bu sipariş restock edildi. Geç reconcile (iyzico'dan para
                        # çekildiği ortaya çıkarsa) tekrar paid+confirmed olursa stok TEKRAR
                        # düşülmeli (oversell önle) — payment._mark_order_from_payment bu işareti okur.
                        "_restocked_by_autocancel": True,
                        "cancelled_at": datetime.now(timezone.utc).isoformat(),
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }}
                )
                if res.matched_count == 0:
                    continue  # arada ödendi/durumu değişti — dokunma
                await _restock_order_once(order, "auto_cancel_expired")
                cancelled += 1
            except Exception as e_item:
                logger.error(f"Failed to cancel card order {order.get('order_number')}: {e_item}")
        if cancelled:
            logger.info(f"[scheduler] Auto-cancelled {cancelled} unpaid/failed card orders (>3h)")
    except Exception as e:
        logger.exception(f"[scheduler] auto_cancel_unpaid_card_orders failed: {e}")


async def reconcile_charged_but_unrecorded_orders():
    """OTOMATİK KURTARMA: iyzico'dan para ÇEKİLMİŞ ama 'ödendi' işaretlenmemiş kart siparişlerini
    (needs_reconciliation bayraklı VEYA pending/failed) iyzico'dan paymentId ile doğrulayıp
    finalize eder — kimsenin elle 'recover-charged' çalıştırmasına gerek kalmaz.
    _finalize_by_payment_id belirsiz cevapta siparişe DOKUNMAZ (yanlış paid yazmaz).
    Kapsam: son 7 gün, kart (COD/havale hariç), tur başına en çok 50 sipariş."""
    from routes.deps import db  # lazy import
    try:
        from routes.payment import _finalize_by_payment_id
    except Exception as e:
        logger.warning(f"[scheduler] reconcile import atlandı: {e}")
        return
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        _cod_bank = ["cash_on_delivery", "kapida", "kapida_odeme", "cod",
                     "bank_transfer", "havale", "eft", "havale_eft", "banka_havale", "transfer"]
        # GÜVENLİK (denetim 2026-10 #2, pencere açlığı): aday listesi en yeni 50 siparişle
        # sınırlı. Eskiden paymentId'si OLMAYAN siparişler de (döngüde zaten atlanıyordu) bu 50'lik
        # pencereyi dolduruyordu → misafir checkout ile açılan ödemesiz siparişler, kartı GERÇEKTEN
        # çekilmiş daha eski siparişi mutabakattan düşürebiliyordu. Artık yalnız doğrulanabilir
        # (paymentId taşıyan) siparişler aday olur. Kapsam/karar mantığı DEĞİŞMEDİ (değişmez 1/4).
        _has_pid = [
            {"reconcile_payment_id": {"$nin": [None, ""]}},
            {"iyzico_payment_id": {"$nin": [None, ""]}},
            {"payment_id": {"$nin": [None, ""]}},
            {"iyzico_retrieve_response.paymentId": {"$nin": [None, ""]}},
        ]
        query = {
            "created_at": {"$gt": cutoff},
            "payment_method": {"$nin": _cod_bank},
            "payment_status": {"$ne": "paid"},
            "$and": [
                {"$or": [
                    {"needs_reconciliation": True},
                    {"payment_status": {"$in": ["pending", "failed"]}},
                ]},
                {"$or": _has_pid},
            ],
        }
        recovered = 0
        candidates = await db.orders.find(
            query, {"_id": 0, "id": 1, "reconcile_payment_id": 1,
                    "iyzico_payment_id": 1, "payment_id": 1, "order_number": 1,
                    "iyzico_retrieve_response": 1}
        ).sort("created_at", -1).to_list(50)
        for order in candidates:
            if not await _still_leader():  # fencing: liderlik kaybedildiyse yazma yapma
                break
            # KÖK NEDEN #1: eski 'failed' siparişlerde paymentId ÜST DÜZEYDE yok, yalnızca
            # iyzico_retrieve_response.paymentId içinde saklı. O yüzden nested alanı da OKU →
            # bayrağı olmayan mevcut takılı siparişler de otomatik kurtarılabilsin.
            _nested_pid = ""
            try:
                _nested_pid = str((order.get("iyzico_retrieve_response") or {}).get("paymentId") or "").strip()
            except Exception:
                _nested_pid = ""
            pid = str(order.get("reconcile_payment_id") or order.get("iyzico_payment_id")
                      or order.get("payment_id") or _nested_pid or "").strip()
            if not pid:
                continue  # paymentId yok → otomatik doğrulanamaz (webhook/callback bekler)
            try:
                paid = await _finalize_by_payment_id(order["id"], pid)
            except Exception as _e:
                logger.warning(f"[scheduler] reconcile finalize hata {order.get('order_number')}: {_e}")
                continue
            if paid:
                # Çift tahsilat işaretli siparişte bayrak KALIR (iade gerekiyor — payment.py).
                await db.orders.update_one(
                    {"id": order["id"], "needs_duplicate_refund": {"$ne": True}},
                    {"$unset": {"needs_reconciliation": "", "reconcile_payment_id": ""}})
                recovered += 1
        if recovered:
            logger.info(f"[scheduler] iyzico reconcile: {recovered} çekilmiş sipariş otomatik finalize edildi")
    except Exception as e:
        logger.exception(f"[scheduler] reconcile_charged_but_unrecorded_orders failed: {e}")


async def pull_iys_rets():
    """İYS RET TARAMASI (kullanıcı isteği 2026-10-06): müşteri SMS 'RET' / iys.org.tr / İYS uygulaması
    ile reddettiyse bize bildirim gelmez → bizde ONAY görünen alıcılar NetGSM /iys/search ile (her alıcı
    günde en çok bir kez) sorgulanır, İYS'de RET olanlar sitede de RET yapılır (routes/iys.sync_iys_rets)."""
    try:
        from routes.iys import sync_iys_rets
        res = await sync_iys_rets()
        if res.get("ret") or res.get("stopped") or res.get("error"):
            logger.info(f"[scheduler] İYS RET taraması: {res}")
    except Exception as e:
        logger.exception(f"[scheduler] pull_iys_rets failed: {e}")


async def retry_pending_iys_consents():
    """İYS GÜNLÜK TOPLU BİLDİRİM (Kadir isteği 2026-09-29: "günlük bildir tüm izinleri, izin
    kalkarsa da bildir"). Saatte bir tetiklenir; gün içinde (TR saati) bir kez TÜM bekleyen
    izin (ONAY) ve iptal (RET) kayıtlarını NetGSM'e toplu gönderir. 'Sinir asimi' veya hata
    olursa o günün turu tamamlanmış sayılmaz → bir saat sonra kaldığı yerden devam eder.

    ESKİ HATA: NetGSM'in başarılı yanıtı ({"code":"0","error":"false"}) başarısız sayılıyordu;
    her kayıt 30 dk'da bir tek tek yeniden gönderiliyor ve NetGSM 103 'Sinir asimi' veriyordu."""
    from routes.deps import db  # lazy import
    try:
        from routes.iys import report_pending_batch
    except Exception as e:
        logger.warning(f"[scheduler] iys batch import atlandı: {e}")
        return
    try:
        tr_today = (datetime.now(timezone.utc) + timedelta(hours=3)).date().isoformat()
        st = await db.settings.find_one({"id": "iys_daily_batch"}, {"_id": 0}) or {}
        if st.get("completed_date") == tr_today:
            return
        res = await report_pending_batch()
        upd = {"id": "iys_daily_batch", "last_run_at": datetime.now(timezone.utc).isoformat(), "last_result": res}
        if not res.get("stopped") and not res.get("error"):
            upd["completed_date"] = tr_today
        await db.settings.update_one({"id": "iys_daily_batch"}, {"$set": upd}, upsert=True)
        logger.info(f"[scheduler] İYS günlük toplu bildirim: {res}")
    except Exception as e:
        logger.exception(f"[scheduler] retry_pending_iys_consents failed: {e}")


async def send_havale_payment_reminders():
    """Havale/EFT siparişinde ödeme N saat içinde gelmediyse müşteriye 'Siparişiniz Alındı ·
    Ödeme Bekleniyor' (order_awaiting_payment) bildirimini OTOMATİK bir kez daha gönderir.
    Süre: İşletme Kuralları → order.havale_reminder_hours (varsayılan 24; 0 = kapalı).
    Kapsam: awaiting_payment + ödenmemiş + dekont bildirilmemiş (payment_notified değil) +
    daha önce hatırlatılmamış. Otomatik iptal süresine (havale_cancel_hours) girmiş siparişe
    hatırlatma GİTMEZ (iptal işi onu süpürür). Kanal: Sipariş Durumları ayarı; boşsa e-posta (+SMS).
    Yalnız en az bir kanal başarılıysa 'hatırlatıldı' damgalanır (SMTP hatasında tekrar denenir)."""
    from routes.deps import db  # lazy import
    import business_rules as _BR
    try:
        _hrs = int(await _BR.get_rule(db, "order.havale_reminder_hours", 24) or 0)
        if _hrs <= 0:
            return
        _cancel_hrs = int(await _BR.get_rule(db, "order.havale_cancel_hours", 72) or 72)
        now = datetime.now(timezone.utc)
        due_before = (now - timedelta(hours=_hrs)).isoformat()
        alive_after = (now - timedelta(hours=max(_cancel_hrs, _hrs + 1))).isoformat()
        query = {
            "payment_status": {"$nin": ["paid", "expired", "refunded"]},
            "status": "awaiting_payment",
            "payment_method": {"$in": ["transfer", "havale", "bank_transfer", "eft", "havale_eft", "banka_havale"]},
            "payment_notified": {"$ne": True},
            "havale_reminder_sent_at": {"$in": [None, ""]},
            "created_at": {"$lt": due_before, "$gt": alive_after},
        }
        try:
            from notification_service import send_notification
            from routes.orders import _order_notify_vars
            from order_statuses import get_status_config
        except Exception as e:
            logger.warning(f"[scheduler][havale-reminder] import skip: {e}")
            return
        try:
            _cfg = await get_status_config(db)
            _nz = (_cfg.get("notify") or {}).get("awaiting_payment") or {}
        except Exception:
            _nz = {}
        sent = failed = 0
        async for order in db.orders.find(query, {"_id": 0}).limit(200):
            ship = order.get("shipping_address") or {}
            phone = ship.get("phone") or order.get("phone")
            email = ship.get("email") or order.get("email") or order.get("customer_email")
            if not (phone or email):
                await db.orders.update_one({"id": order["id"]},
                                           {"$set": {"havale_reminder_sent_at": now.isoformat(),
                                                     "havale_reminder_result": "alıcı yok (telefon/e-posta boş)"}})
                continue
            channels = [c for c in ("sms", "email") if _nz.get(c)]
            if not channels:
                channels = ["email"] + (["sms"] if phone else [])
            try:
                variables = await _order_notify_vars(order)
                res = await send_notification(db, "order_awaiting_payment", to_phone=phone,
                                              to_email=email, variables=variables, channels=channels)
                results = (res or {}).get("results") or {}
                ok = any((v or {}).get("success") for v in results.values())
                if ok:
                    sent += 1
                    await db.orders.update_one(
                        {"id": order["id"]},
                        {"$set": {"havale_reminder_sent_at": now.isoformat(),
                                  "havale_reminder_result": ",".join(k for k, v in results.items() if (v or {}).get("success"))}})
                else:
                    failed += 1
                    logger.warning(f"[scheduler][havale-reminder] gönderilemedi order={order.get('order_number')} res={results}")
            except Exception as e:
                failed += 1
                logger.warning(f"[scheduler][havale-reminder] hata order={order.get('order_number')}: {e}")
            await asyncio.sleep(0.2)
        if sent or failed:
            logger.info(f"[scheduler][havale-reminder] {_hrs}s hatırlatma: gönderilen={sent} başarısız={failed}")
    except Exception as e:
        logger.exception(f"[scheduler] send_havale_payment_reminders failed: {e}")




# Trendyol mutabakatı (Haz–Eyl 2026, Kadir onaylı): pazaryeri sipariş tarihi olmayan,
# senkron gününe (haziran/temmuz) tarihlenmiş 21 ESKİ sipariş. Gerçek ayları, Trendyol'un
# kendi dosyalarındaki 6.048 gerçek tarihli siparişin numara→tarih eşleşmesinden çıkarıldı
# (Trendyol numaralaması düzenli: 6 Nis→1 Haz 2,97 M/gün, haziran içi 3,06 M/gün).
# (sipariş_no, tahmini_utc, ay_kesin_mi, mayısta_iptal_edilmiş_mi)
_TY_ESKI_SIPARIS_TARIHLERI = [
    ("11088076243", "2026-03-28T03:43:00+00:00", True, False),
    ("11088129955", "2026-03-28T04:09:00+00:00", True, False),
    ("11099088778", "2026-03-31T20:39:00+00:00", False, False),
    ("11106666354", "2026-04-03T09:50:00+00:00", False, False),
    ("11109019280", "2026-04-04T04:50:00+00:00", True, False),
    ("11111951447", "2026-04-05T04:31:00+00:00", True, False),
    ("11120437894", "2026-04-08T00:50:00+00:00", True, False),
    ("11124442410", "2026-04-09T08:59:00+00:00", True, False),
    ("11135972655", "2026-04-13T05:34:00+00:00", True, False),
    ("11142375927", "2026-04-15T08:59:00+00:00", True, False),
    ("11146078168", "2026-04-16T14:43:00+00:00", True, False),
    ("11147650091", "2026-04-17T03:20:00+00:00", True, False),
    ("11154304226", "2026-04-19T08:46:00+00:00", True, False),
    ("11156811581", "2026-04-20T04:54:00+00:00", True, False),
    ("11157562786", "2026-04-20T10:56:00+00:00", True, False),
    ("11276507340", "2026-05-30T13:46:00+00:00", True, True),
    ("11276601402", "2026-05-30T14:23:00+00:00", True, True),
    ("11276619755", "2026-05-30T14:30:00+00:00", True, True),
    ("11277471783", "2026-05-30T20:13:00+00:00", True, True),
    ("11278312994", "2026-05-31T09:47:00+00:00", True, True),
    ("11279553615", "2026-05-31T16:25:00+00:00", True, True),
]


# TEK SEFERLİK onarım listesi (Kadir onaylı, 2026-09-24): 2026-09-23 13:55 UTC'de
# Trendyol statü geçişinin ZATEN iptal olan siparişlere bastığı hatalı "şimdi" iptal
# tarihleri. (sipariş no, hatalı damga UTC, kanıttan GERÇEK iptal tarihi UTC, kanıt)
# Kanıtlar iptal_tarih_q=ty150@2026-09-23T13:5 teşhisinden (salt okuma) alındı.
_TY_IPTAL_TARIHI_ONARIM = [
    ("11486234663", "2026-09-23T13:55:48", "2026-08-09T19:40:10", "trendyol_packageHistories"),
    ("11491325523", "2026-09-23T13:55:49", "2026-08-09T21:10:29", "stock_movement"),
    ("11492875905", "2026-09-23T13:55:48", "2026-08-10T14:57:41", "stock_movement"),
    ("11493820741", "2026-09-23T13:55:48", "2026-08-11T02:42:56", "trendyol_packageHistories"),
    ("11495117478", "2026-09-23T13:55:48", "2026-08-11T08:56:44", "stock_movement"),
    ("11497767315", "2026-09-23T13:55:48", "2026-08-11T22:26:44", "stock_movement"),
    ("11499109610", "2026-09-23T13:55:47", "2026-08-12T12:40:20", "stock_movement"),
    ("11499696190", "2026-09-23T13:55:47", "2026-08-12T16:14:51", "stock_movement"),
    ("11501086965", "2026-09-23T13:55:47", "2026-08-13T05:15:09", "stock_movement"),
    ("11501270883", "2026-09-23T13:55:47", "2026-08-13T07:27:09", "stock_movement"),
    ("11502133745", "2026-09-23T13:55:47", "2026-08-13T14:17:53", "stock_movement"),
    ("11502290793", "2026-09-23T13:55:47", "2026-08-13T16:35:26", "trendyol_packageHistories"),
    ("11507083906", "2026-09-23T13:55:46", "2026-08-15T07:37:00", "stock_movement"),
    ("11507677220", "2026-09-23T13:55:46", "2026-08-16T07:20:41", "trendyol_packageHistories"),
    ("11508380860", "2026-09-23T13:55:46", "2026-08-16T11:29:48", "trendyol_packageHistories"),
    ("11509850503", "2026-09-23T13:55:46", "2026-08-17T03:07:09", "trendyol_packageHistories"),
    ("11510930646", "2026-09-23T13:55:46", "2026-08-17T07:36:13", "trendyol_packageHistories"),
    ("11516817762", "2026-09-23T13:55:45", "2026-08-19T04:43:27", "trendyol_packageHistories"),
    ("11517282400", "2026-09-23T13:55:46", "2026-08-18T14:13:19", "stock_movement"),
    ("11517744594", "2026-09-23T13:55:45", "2026-08-19T07:46:29", "trendyol_packageHistories"),
    ("11518377734", "2026-09-23T13:55:45", "2026-08-19T04:04:00", "trendyol_packageHistories"),
    ("11519616422", "2026-09-23T13:55:45", "2026-08-19T10:10:22", "stock_movement"),
    ("11519828012", "2026-09-23T13:55:45", "2026-08-19T12:12:19", "stock_movement"),
    ("11520628632", "2026-09-23T13:55:44", "2026-08-19T15:25:10", "stock_movement"),
    ("11520782188", "2026-09-23T13:55:44", "2026-08-20T08:23:31", "trendyol_packageHistories"),
    ("11521136517", "2026-09-23T13:55:44", "2026-08-19T18:58:26", "stock_movement"),
    ("11521473118", "2026-09-23T13:55:44", "2026-08-19T20:22:26", "stock_movement"),
    ("11522043326", "2026-09-23T13:55:44", "2026-08-20T06:15:10", "stock_movement"),
    ("11523309384", "2026-09-23T13:55:43", "2026-08-21T04:43:18", "trendyol_packageHistories"),
    ("11524458727", "2026-09-23T13:55:44", "2026-08-20T19:40:24", "stock_movement"),
    ("11525137804", "2026-09-23T13:55:43", "2026-08-21T07:21:57", "stock_movement"),
    ("11526393850", "2026-09-23T13:55:53", "2026-08-23T17:10:23", "trendyol_packageHistories"),
    ("11528437030", "2026-09-23T13:55:43", "2026-08-22T12:33:04", "stock_movement"),
    ("11529784164", "2026-09-23T13:55:43", "2026-08-22T19:24:46", "stock_movement"),
    ("11529889472", "2026-09-23T13:55:53", "2026-08-24T05:52:38", "trendyol_packageHistories"),
    ("11530520471", "2026-09-23T13:55:43", "2026-08-23T00:48:03", "stock_movement"),
    ("11531742164", "2026-09-23T13:55:53", "2026-08-24T00:05:19", "trendyol_packageHistories"),
    ("11534045820", "2026-09-23T13:55:53", "2026-08-24T13:49:06", "trendyol_packageHistories"),
    ("11534698898", "2026-09-23T13:55:52", "2026-08-24T14:45:04", "trendyol_packageHistories"),
    ("11535547865", "2026-09-23T13:55:52", "2026-08-24T19:50:40", "stock_movement"),
    ("11537965513", "2026-09-23T13:55:52", "2026-08-25T15:10:22", "trendyol_packageHistories"),
    ("11546505883", "2026-09-23T13:55:52", "2026-08-28T12:52:54", "trendyol_packageHistories"),
    ("11548155980", "2026-09-23T13:55:51", "2026-08-30T10:22:16", "trendyol_packageHistories"),
    ("11549792253", "2026-09-23T13:55:52", "2026-08-29T15:36:13", "stock_movement"),
    ("11550158329", "2026-09-23T13:55:52", "2026-08-30T09:21:37", "trendyol_packageHistories"),
    ("11551442651", "2026-09-23T13:55:52", "2026-08-30T10:30:26", "stock_movement"),
    ("11551970048", "2026-09-23T13:55:51", "2026-08-30T13:00:06", "stock_movement"),
    ("11552679449", "2026-09-23T13:55:51", "2026-08-31T01:26:26", "trendyol_packageHistories"),
    ("11557290651", "2026-09-23T13:55:51", "2026-09-01T09:04:06", "stock_movement"),
    ("11557881879", "2026-09-23T13:55:51", "2026-09-01T11:55:07", "stock_movement"),
    ("11558648949", "2026-09-23T13:55:50", "2026-09-02T04:54:49", "trendyol_packageHistories"),
    ("11560279855", "2026-09-23T13:55:50", "2026-09-02T07:58:12", "stock_movement"),
    ("11561213960", "2026-09-23T13:55:50", "2026-09-02T14:16:46", "stock_movement"),
    ("11562446909", "2026-09-23T13:55:50", "2026-09-02T21:26:46", "stock_movement"),
    ("11564821593", "2026-09-23T13:55:50", "2026-09-03T16:58:09", "stock_movement"),
    ("11565979118", "2026-09-23T13:55:50", "2026-09-03T21:25:25", "stock_movement"),
    ("11573610310", "2026-09-23T13:55:49", "2026-09-05T20:21:25", "stock_movement"),
    ("11573752697", "2026-09-23T13:55:49", "2026-09-06T02:47:47", "trendyol_packageHistories"),
    ("11573884884", "2026-09-23T13:55:49", "2026-09-05T23:36:24", "stock_movement"),
    ("11574240936", "2026-09-23T13:55:49", "2026-09-06T07:46:02", "stock_movement"),
    ("11584823325", "2026-09-23T13:55:52", "2026-09-09T13:10:26", "trendyol_packageHistories"),
    ("11585204566", "2026-09-23T13:55:52", "2026-09-09T18:35:32", "trendyol_packageHistories"),
    ("11588613183", "2026-09-23T13:55:51", "2026-09-11T01:51:05", "trendyol_packageHistories"),
    ("11590253490", "2026-09-23T13:55:51", "2026-09-11T06:53:35", "trendyol_packageHistories"),
    ("11590564397", "2026-09-23T13:55:51", "2026-09-10T21:47:46", "stock_movement"),
    ("11590567169", "2026-09-23T13:55:51", "2026-09-10T21:47:46", "stock_movement"),
    ("11590802613", "2026-09-23T13:55:51", "2026-09-11T05:42:46", "stock_movement"),
    ("11592952925", "2026-09-23T13:55:51", "2026-09-11T17:10:35", "stock_movement"),
    ("11593247553", "2026-09-23T13:55:50", "2026-09-11T23:20:18", "trendyol_packageHistories"),
    ("11595234073", "2026-09-23T13:55:50", "2026-09-12T12:39:52", "stock_movement"),
    ("11596597504", "2026-09-23T13:55:50", "2026-09-13T08:12:55", "trendyol_packageHistories"),
    ("11596668675", "2026-09-23T13:55:50", "2026-09-13T08:30:36", "trendyol_packageHistories"),
    ("11597079111", "2026-09-23T13:55:50", "2026-09-12T22:31:55", "stock_movement"),
    ("11598091538", "2026-09-23T13:55:50", "2026-09-13T11:29:12", "stock_movement"),
    ("11599086465", "2026-09-23T13:55:49", "2026-09-13T15:51:55", "stock_movement"),
    ("11601535232", "2026-09-23T13:55:49", "2026-09-14T11:21:12", "stock_movement"),
    ("11604237478", "2026-09-23T13:55:49", "2026-09-15T07:21:39", "trendyol_packageHistories"),
    ("11605799324", "2026-09-23T13:55:49", "2026-09-15T14:34:27", "stock_movement"),
    ("11605854532", "2026-09-23T13:55:50", "2026-09-16T04:05:50", "trendyol_lastModifiedDate"),
    ("11607536049", "2026-09-23T13:55:49", "2026-09-16T10:40:01", "trendyol_packageHistories"),
    ("11609189584", "2026-09-23T13:55:50", "2026-09-16T11:38:59", "stock_movement"),
    ("11609727015", "2026-09-23T13:55:48", "2026-09-17T04:48:33", "trendyol_packageHistories"),
    ("11610150755", "2026-09-23T13:55:49", "2026-09-16T14:58:19", "stock_movement"),
    ("11610577381", "2026-09-23T13:55:48", "2026-09-16T16:16:23", "stock_movement"),
    ("11612255923", "2026-09-23T13:55:48", "2026-09-17T04:34:49", "trendyol_packageHistories"),
    ("11612434277", "2026-09-23T13:55:48", "2026-09-17T07:09:29", "trendyol_packageHistories"),
    ("11612444123", "2026-09-23T13:55:48", "2026-09-17T07:25:44", "trendyol_packageHistories"),
    ("11613279611", "2026-09-23T13:55:47", "2026-09-17T10:36:39", "stock_movement"),
    ("11613333152", "2026-09-23T13:55:47", "2026-09-17T13:09:23", "stock_movement"),
    ("11613655534", "2026-09-23T13:55:47", "2026-09-17T14:03:42", "stock_movement"),
    ("11613743238", "2026-09-23T13:55:46", "2026-09-18T07:19:17", "trendyol_packageHistories"),
    ("11614885950", "2026-09-23T13:55:46", "2026-09-18T07:58:14", "trendyol_packageHistories"),
    ("11615111079", "2026-09-23T13:55:47", "2026-09-17T17:48:42", "stock_movement"),
    ("11615487775", "2026-09-23T13:55:47", "2026-09-17T21:02:50", "stock_movement"),
    ("11615621182", "2026-09-23T13:55:46", "2026-09-18T07:56:44", "trendyol_packageHistories"),
    ("11616356568", "2026-09-23T13:55:47", "2026-09-18T06:11:23", "stock_movement"),
    ("11616742778", "2026-09-23T13:55:46", "2026-09-18T10:24:34", "stock_movement"),
    ("11617817891", "2026-09-23T13:55:46", "2026-09-18T13:26:50", "stock_movement"),
    ("11619671809", "2026-09-23T13:55:46", "2026-09-18T23:17:24", "stock_movement"),
    ("11619782090", "2026-09-23T13:55:44", "2026-09-21T08:28:50", "trendyol_packageHistories"),
    ("11619969809", "2026-09-23T13:55:45", "2026-09-21T04:04:30", "trendyol_packageHistories"),
    ("11620742361", "2026-09-23T13:55:45", "2026-09-19T10:17:24", "stock_movement"),
    ("11622657015", "2026-09-23T13:55:45", "2026-09-20T14:26:56", "trendyol_packageHistories"),
    ("11623370289", "2026-09-23T13:55:45", "2026-09-19T21:56:17", "stock_movement"),
    ("11626582789", "2026-09-23T13:55:45", "2026-09-20T19:21:00", "stock_movement"),
    ("11627025873", "2026-09-23T13:55:45", "2026-09-20T21:27:01", "stock_movement"),
    ("11628377790", "2026-09-23T13:55:44", "2026-09-21T12:25:56", "stock_movement"),
    ("11628994173", "2026-09-23T13:55:44", "2026-09-21T16:00:56", "stock_movement"),
    ("11629807880", "2026-09-23T13:55:44", "2026-09-21T17:20:55", "stock_movement"),
    ("11630477889", "2026-09-23T13:55:44", "2026-09-21T21:20:30", "stock_movement"),
    ("11632732056", "2026-09-23T13:55:44", "2026-09-22T17:41:21", "stock_movement"),
    ("11632900495", "2026-09-23T13:55:43", "2026-09-22T18:54:04", "stock_movement"),
    ("11633288426", "2026-09-23T13:55:43", "2026-09-22T18:36:21", "stock_movement"),
    ("11633486389", "2026-09-23T13:55:43", "2026-09-22T18:36:34", "stock_movement"),
    ("11633617681", "2026-09-23T13:55:43", "2026-09-22T19:16:48", "stock_movement"),
    ("11635085180", "2026-09-23T13:55:43", "2026-09-23T10:39:27", "stock_movement"),
]


# Doğan'da İPTAL edildikten sonra doğru tutarla YENİDEN kesilecek faturalar.
# (sipariş no, iptal edilmesi beklenen ESKİ fatura no). Kullanıcı faturayı Doğan'da iptal
# ettiğinde iş bunu görür, siparişi sıfırlar, yeniden keser (HB/TY'ye otomatik gönderilir).
_FATURA_YENIDEN_KESIM = [
    ("HB4029270518", "FCT2026000020776"),   # Kadir isteği 2026-09-28 (HB ilk-kalem toplam hatası)
]


async def _members_only_canli_test(asama: str):
    """TEK SEFERLİK (Kadir onaylı 2026-09-29): üyelere özel özelliğinin CANLI testi.
    'ac': Kimono kategorisini (3381) geçici olarak üyelere özel işaretler (daha önce işaretli
    değilse). 'kapat': YALNIZ bu testin açtığı işareti geri alır. Önbellek anında tazelenir."""
    from routes.deps import db
    from routes.products import invalidate_members_only_cache
    _flag = "members_only_canli_test_v1"
    try:
        st = (await db.settings.find_one({"id": _flag}, {"_id": 0})) or {}
        now = datetime.now(timezone.utc).isoformat()
        if asama == "ac":
            if st.get("acildi_at"):
                return
            cat = await db.categories.find_one({"id": "3381"}, {"_id": 0, "members_only": 1, "name": 1})
            if not cat or cat.get("members_only"):
                await db.settings.update_one({"id": _flag}, {"$set": {"id": _flag, "acildi_at": now,
                                             "atlandi": "kategori yok ya da zaten işaretli"}}, upsert=True)
                return
            await db.categories.update_one({"id": "3381"}, {"$set": {"members_only": True}})
            invalidate_members_only_cache()
            await db.settings.update_one({"id": _flag}, {"$set": {"id": _flag, "acildi_at": now,
                                         "biz_actik": True}}, upsert=True)
            logger.info("[scheduler] üyelere özel canlı test: Kimono geçici işaretlendi")
        else:
            if st.get("biz_actik") and not st.get("kapandi_at"):
                await db.categories.update_one({"id": "3381"}, {"$set": {"members_only": False}})
                invalidate_members_only_cache()
                await db.settings.update_one({"id": _flag}, {"$set": {"kapandi_at": now}})
                logger.info("[scheduler] üyelere özel canlı test: Kimono işareti kaldırıldı")
    except Exception as e:
        logger.warning(f"[scheduler] üyelere özel canlı test ({asama}) hata: {e}")


async def _bekleyen_fatura_temizle_once():
    """TEK SEFERLİK (Kadir açık isteği 2026-10-15, çift-fatura riski kendisine sorulup onaylandı):
    Doğan'a ulaşılamadığı için 'bekleyen / inceleme gerekli / takılı kilit' durumunda kalan fatura
    istekleri temizlenir. Kapsam YALNIZ faturası KESİLMEMİŞ (invoice_issued≠True) siparişler.
    `orders.reset-invoice` ile aynı alanlar; bekleyen UUID/numara geçmişe + denetim kaydı."""
    from routes.deps import db
    from activity_audit import record_admin_audit
    _flag = "bekleyen_fatura_temizle_once_v1"
    try:
        if ((await db.settings.find_one({"id": _flag}, {"_id": 0, "done": 1})) or {}).get("done"):
            return
        q = {"invoice_issued": {"$ne": True}, "$or": [
            {"invoice_pending.number": {"$exists": True}},
            {"invoice_needs_review": True},
            {"invoice_in_progress": True},
        ]}
        now = datetime.now(timezone.utc).isoformat()
        temizlenen = []
        async for o in db.orders.find(q, {"_id": 0, "id": 1, "order_number": 1, "invoice_number": 1,
                                          "invoice_pending": 1}):
            _pend = o.get("invoice_pending") or {}
            upd = {"$set": {"invoice_issued": False},
                   "$unset": {"invoice_number": "", "invoice_uuid": "", "invoice_type": "",
                              "invoice_provider": "", "invoice_provider_response": "",
                              "invoice_intl_txn_id": "", "invoice_dogan_id": "", "invoice_pdf_url": "",
                              "invoice_issued_at": "", "invoice_issued_by": "",
                              "invoice_last_error": "", "invoice_last_error_at": "",
                              "invoice_in_progress": "", "invoice_in_progress_at": "",
                              "invoice_pending": "", "invoice_needs_review": ""}}
            if _pend:
                upd["$push"] = {"invoice_pending_history": {**_pend, "cleared_at": now,
                                                            "cleared_by": "system:bekleyen_fatura_temizle"}}
            r = await db.orders.update_one({"id": o["id"], "invoice_issued": {"$ne": True}}, upd)
            if r.modified_count:
                temizlenen.append({"order": o.get("order_number") or o["id"],
                                   "pending_number": str(_pend.get("number") or "")})
                await record_admin_audit(
                    db, action="orders.invoice_reset", entity_type="order", entity_id=o["id"],
                    before={"invoice_number": o.get("invoice_number") or "",
                            "invoice_pending_number": str(_pend.get("number") or "")},
                    after={"invoice_number": "", "invoice_issued": False, "invoice_pending_number": ""},
                    source="scheduler.bekleyen_fatura_temizle")
        await db.settings.update_one({"id": _flag}, {"$set": {"id": _flag, "done": True, "at": now,
                                     "temizlenen": temizlenen}}, upsert=True)
        logger.info(f"[scheduler] bekleyen fatura temizliği: {len(temizlenen)} sipariş — {temizlenen}")
    except Exception as e:
        logger.warning(f"[scheduler] bekleyen fatura temizliği hata: {e}")


async def _run_varyant_id_normalize():
    """Beden kimliği kuralını TÜM ürünlere uygular (6 saatte bir, idempotent):
    her beden id = kendi 4 haneli urun_id; urun_id ürünler arası tekil (önce oluşturulan ürün
    korur, kopyalayan ürüne yeni numara). Panelden/imalattan açılan 'var-…' bedenler ve
    kopyalanan ürünlerin çakışan ID'leri böylece Meta katalog/pikselde 4 haneli ve tekil olur.
    Barkodsuz bedenin id'sine dokunulmaz (eski siparişlerin iadesi id ile çözülür)."""
    from routes.deps import db, build_used_urun_id_set, normalize_variant_ids
    try:
        used = await build_used_urun_id_set()
        claimed = {}
        changed_products, changed_vars, barkodsuz = 0, 0, 0
        ornek = []
        async for p in db.products.find({}, {"_id": 0, "id": 1, "name": 1, "variants": 1}).sort("created_at", 1):
            vs = p.get("variants") or []
            if not vs:
                continue
            before = [(str(v.get("id") or ""), str(v.get("urun_id") or "")) for v in vs]
            n = normalize_variant_ids(vs, used, claimed)
            for v in vs:
                u = str(v.get("urun_id") or "").strip()
                if u:
                    claimed.setdefault(u, p["id"])
                if not str(v.get("barcode") or "").strip() and not str(v.get("id") or "").isdigit():
                    barkodsuz += 1
            if n:
                after = [(str(v.get("id") or ""), str(v.get("urun_id") or "")) for v in vs]
                if after != before:
                    await db.products.update_one({"id": p["id"]}, {"$set": {
                        "variants": vs, "updated_at": datetime.now(timezone.utc).isoformat()}})
                    # Bekleyen "stoğa gelince haber ver" kayıtları yeni beden id'sine taşınır.
                    for (oid, _ou), (nid, _nu) in zip(before, after):
                        if oid and nid and oid != nid:
                            try:
                                await db.stock_alerts.update_many(
                                    {"product_id": p["id"], "variant_id": oid, "notified": False},
                                    {"$set": {"variant_id": nid}})
                            except Exception:
                                pass
                    changed_products += 1
                    changed_vars += sum(1 for a, b in zip(before, after) if a != b)
                    if len(ornek) < 15:
                        ornek.append({"urun": (p.get("name") or "")[:40],
                                      "degisen": [f"{a[0]}→{b[0]}" for a, b in zip(before, after) if a != b][:4]})
        await db.settings.update_one({"id": "varyant_id_normalize"}, {"$set": {
            "id": "varyant_id_normalize", "at": datetime.now(timezone.utc).isoformat(),
            "degisen_urun": changed_products, "degisen_beden": changed_vars,
            "barkodsuz_4hanesiz": barkodsuz, "ornek": ornek}}, upsert=True)
        if changed_products:
            logger.info(f"[scheduler] varyant id normalize: {changed_products} ürün / {changed_vars} beden")
        # DOĞRULAMA + UYARI (kullanıcı görsün, söze güvenmek zorunda kalmasın):
        # düzeltme sonrası hâlâ 4 haneli olmayan / çakışan beden id'si var mı?
        import re as _re_v
        _seen, _bad, _dup = {}, [], []
        async for p in db.products.find({"is_deleted": {"$ne": True}},
                                        {"_id": 0, "id": 1, "name": 1, "variants.id": 1}):
            for v in (p.get("variants") or []):
                _vid = str(v.get("id") or "").strip()
                if not _re_v.fullmatch(r"\d{4,}", _vid):
                    _bad.append(f"{(p.get('name') or '')[:30]}: {_vid[:24]}")
                elif _vid in _seen and _seen[_vid] != p["id"]:
                    _dup.append(f"{_vid} ({(p.get('name') or '')[:30]})")
                _seen.setdefault(_vid, p["id"])
        try:
            from security.alerts import send_alert
            if _bad or _dup:
                await send_alert(
                    "variant_id_rule", "Beden ID kuralı BOZUK — Meta katalog eşleşmesi etkilenir",
                    f"4 haneli olmayan: {len(_bad)} · çakışan: {len(_dup)} · örnek: {(_bad + _dup)[:6]}",
                    level="critical", fingerprint="variant_id_rule_broken",
                    meta={"bad": _bad[:30], "dup": _dup[:30]})
            elif changed_products:
                await send_alert(
                    "variant_id_rule", f"Beden ID'si kurala uymayan {changed_products} ürün otomatik düzeltildi",
                    f"Bir yoldan 4 haneli olmayan/çakışan beden ID'si girdi ve düzeltildi. Örnek: {ornek[:5]}",
                    level="warning", fingerprint="variant_id_rule_fixed", meta={"ornek": ornek[:15]})
        except Exception as _ae:
            logger.warning(f"[scheduler] varyant id uyarısı gönderilemedi: {_ae}")
    except Exception as e:
        logger.warning(f"[scheduler] _run_varyant_id_normalize failed: {e}")


async def _ensure_hb4029_fatura_iptal():
    """TEK SEFERLİK (Kadir açık isteği 2026-09-28): HB4029270518'in eksik tutarlı e-Arşiv
    faturası FCT2026000020776'yı Doğan'da İPTAL et, ardından doğru tutarla yeniden kes.
    Önce SIMULATION_FLAG=Y ile Doğan'a doğrulatılır; hata dönerse GERÇEK iptal YAPILMAZ.
    Şema WSDL'den okundu: CancelEArchiveInvoice(REQUEST_HEADER, CancelEArsivInvoiceContent[])."""
    from routes.deps import db  # lazy import
    _no, _eski, _tutar = "HB4029270518", "FCT2026000020776", 6112.50
    _flag = f"fatura_yeniden_kesim:iptal2:{_no}"   # v2: ilk deneme SESSION_ID eksik (hiçbir şey gönderilmedi)
    try:
        if ((await db.settings.find_one({"id": _flag}, {"_id": 0, "done": 1})) or {}).get("done"):
            return
        res = {"at": datetime.now(timezone.utc).isoformat()}
        o = await db.orders.find_one({"order_number": _no}, {"_id": 0, "invoice_number": 1, "invoice_uuid": 1})
        if not o or (o.get("invoice_number") or "") != _eski or not o.get("invoice_uuid"):
            res["durum"] = f"siparişteki fatura {(o or {}).get('invoice_number')} — beklenen {_eski} değil, iptal edilmedi"
            await db.settings.update_one({"id": _flag}, {"$set": {"id": _flag, "done": True, "sonuc": res}}, upsert=True)
            return
        ds = await db.settings.find_one({"id": "dogan_edonusum"}, {"_id": 0}) or {}
        from dogan_client import DoganClient
        from fastapi.concurrency import run_in_threadpool
        from zeep.helpers import serialize_object
        from decimal import Decimal
        from datetime import date as _date
        cli = DoganClient(username=ds["username"], password=ds["password"], is_test=ds.get("is_test", True))
        content = [{
            "FATURA_UUID": o["invoice_uuid"], "FATURA_ID": _eski,
            "IPTAL_TARIHI": _date.today(), "TOPLAM_TUTAR": Decimal(f"{_tutar:.2f}"),
            "IPTAL_NOTU": "Hatali tutar - dogru tutarla yeniden duzenlenecek",
        }]

        def _call(sim: bool):
            _svc = cli._get_earsiv_client().service   # ÖNCE giriş (SESSION_ID), sonra başlık
            hdr = cli._make_header()
            if sim:
                hdr["SIMULATION_FLAG"] = "Y"
            r = _svc.CancelEArchiveInvoice(REQUEST_HEADER=hdr, CancelEArsivInvoiceContent=content)
            return serialize_object(r)

        def _err(ser):
            e = (ser or {}).get("ERROR_TYPE") if isinstance(ser, dict) else None
            return e if (e and e.get("ERROR_CODE")) else None

        try:
            sim = await run_in_threadpool(_call, True)
            res["simulasyon"] = str(sim)[:800]
        except Exception as e:
            res["simulasyon_hata"] = str(e)[:500]
            await db.settings.update_one({"id": _flag}, {"$set": {"id": _flag, "done": True, "sonuc": res}}, upsert=True)
            return
        if _err(sim):
            res["durum"] = "simülasyon hata döndü — GERÇEK iptal yapılmadı"
            await db.settings.update_one({"id": _flag}, {"$set": {"id": _flag, "done": True, "sonuc": res}}, upsert=True)
            return
        try:
            real = await run_in_threadpool(_call, False)
            res["iptal"] = str(real)[:800]
        except Exception as e:
            res["iptal_hata"] = str(e)[:500]
        # Durum doğrula
        try:
            st = await run_in_threadpool(lambda: serialize_object(cli._get_earsiv_client().service.GetEArchiveInvoiceStatus(
                REQUEST_HEADER=cli._make_header(), UUID=o["invoice_uuid"])))
            hdr = ((st.get("INVOICE") or [{}])[0] or {}).get("HEADER") or {}
            res["son_durum"] = f"{hdr.get('STATUS')} {hdr.get('STATUS_DESC')} {hdr.get('PROFILE')}"
        except Exception as e:
            res["son_durum_hata"] = str(e)[:300]
        await db.settings.update_one({"id": _flag}, {"$set": {"id": _flag, "done": True, "sonuc": res}}, upsert=True)
        logger.info(f"[scheduler] {_no} fatura iptal: {res}")
        # İptal olduysa yeniden kesimi HEMEN çalıştır (10 dk beklemeden)
        await _run_fatura_yeniden_kesim()
    except Exception as e:
        logger.warning(f"[scheduler] _ensure_hb4029_fatura_iptal failed: {e}")


async def _run_fatura_yeniden_kesim():
    """Her 10 dk: listedeki siparişlerin ESKİ faturası Doğan'da İPTAL mi? İptalse sıfırla +
    yeniden kes. GÜVENLİK: sipariş üzerindeki fatura no beklenen ESKİ fatura değilse (personel
    elle yeniden kesmiş) HİÇBİR ŞEY yapılmaz; iptal görünmüyorsa beklenir. Sonuç settings'e."""
    from routes.deps import db  # lazy import
    for _no, _eski in _FATURA_YENIDEN_KESIM:
        _flag = f"fatura_yeniden_kesim:{_no}:{_eski}"
        try:
            if ((await db.settings.find_one({"id": _flag}, {"_id": 0, "done": 1})) or {}).get("done"):
                continue
            _now = datetime.now(timezone.utc).isoformat()
            res = {"at": _now}
            o = await db.orders.find_one({"order_number": _no}, {"_id": 0})
            if not o:
                continue
            if (o.get("invoice_number") or "") != _eski:
                res["durum"] = f"siparişteki fatura artık {o.get('invoice_number') or 'yok'} — beklenen eski fatura değil, dokunulmadı"
                await db.settings.update_one({"id": _flag}, {"$set": {"id": _flag, "done": True, "sonuc": res}}, upsert=True)
                continue
            _uuid = (o.get("invoice_uuid") or "").strip()
            if not _uuid:
                continue
            ds = await db.settings.find_one({"id": "dogan_edonusum"}, {"_id": 0}) or {}
            if not (ds.get("enabled") and ds.get("username")):
                continue
            from dogan_client import DoganClient
            from fastapi.concurrency import run_in_threadpool
            from zeep.helpers import serialize_object
            cli = DoganClient(username=ds["username"], password=ds["password"], is_test=ds.get("is_test", True))
            r = await run_in_threadpool(lambda: cli._get_earsiv_client().service.GetEArchiveInvoiceStatus(
                REQUEST_HEADER=cli._make_header(), UUID=_uuid))
            ser = serialize_object(r) or {}
            inv = ((ser.get("INVOICE") or [{}])[0] or {}).get("HEADER") or {}
            iptal = (str(inv.get("STATUS")) == "201" or "İPTAL" in str(inv.get("STATUS_DESC") or "").upper()
                     or str(inv.get("PROFILE") or "").upper() == "IPTAL")
            if str(inv.get("INVOICE_ID") or "") != _eski or not iptal:
                # Henüz iptal edilmemiş → bekle (son kontrol kaydı)
                await db.settings.update_one({"id": _flag}, {"$set": {"id": _flag, "son_kontrol": _now,
                                             "eski_durum": f"{inv.get('STATUS')} {inv.get('STATUS_DESC')}"}}, upsert=True)
                continue
            await db.orders.update_one({"id": o["id"], "invoice_number": _eski}, {
                "$set": {"invoice_issued": False, "hepsiburada_invoice_uploaded": False},
                "$unset": {"invoice_number": "", "invoice_uuid": "", "invoice_type": "",
                           "invoice_provider": "", "invoice_provider_response": "",
                           "invoice_intl_txn_id": "", "invoice_dogan_id": "", "invoice_pdf_url": "",
                           "invoice_issued_at": "", "invoice_issued_by": "",
                           "invoice_last_error": "", "invoice_last_error_at": "",
                           "invoice_in_progress": "", "invoice_in_progress_at": "",
                           "hepsiburada_invoice_error": ""}})
            from routes.orders import create_invoice_for_order
            from fastapi import HTTPException as _HE
            try:
                r2 = await create_invoice_for_order(
                    order_id=o["id"], invoice_type="auto",
                    current_user={"id": "system", "email": "sistem (iptal sonrası yeniden kesim)", "is_admin": True})
                res["yeni"] = {k: r2.get(k) for k in ("success", "invoice_number", "invoice_type", "message")}
            except _HE as he:
                res["hata"] = f"fatura kesilemedi: {he.detail}"
            except Exception as e:
                res["hata"] = f"fatura kesilemedi: {e}"
            await db.settings.update_one({"id": _flag}, {"$set": {"id": _flag, "done": True, "sonuc": res}}, upsert=True)
            logger.info(f"[scheduler] fatura yeniden kesim {_no}: {res}")
        except Exception as e:
            logger.warning(f"[scheduler] fatura yeniden kesim {_no} hata: {e}")


async def _ensure_hb4153_fatura_yeniden():
    """TEK SEFERLİK (Kadir isteği 2026-09-28): HB4153705034 eski faturası (FCT2026000021268)
    Doğan'da İPTAL edildi → güncel (doğru tutarlı) fatura yeniden kesilir ve HB'ye gönderilir.
    GÜVENLİK: eski fatura Doğan'da İPTAL görünmüyorsa HİÇBİR ŞEY yapılmaz (çift fatura yok).
    Ayrıca (kullanıcı isteği) HB toplam onarımının eski-değer alanları kayıtlardan silinir."""
    from routes.deps import db  # lazy import
    _flag = "hb4153_fatura_yeniden_v1"
    try:
        if ((await db.settings.find_one({"id": _flag}, {"_id": 0, "done": 1})) or {}).get("done"):
            return
        _now = datetime.now(timezone.utc).isoformat()
        # Eski-değer izlerini sil (geri alma istenmiyor)
        await db.orders.update_many({"total_fix_from": {"$exists": True}},
                                    {"$unset": {"total_fix_from": "", "total_fixed_at": "", "total_fix_reason": ""}})
        await db.settings.update_one({"id": "hb_toplam_onarim_v1"}, {"$unset": {"fixed": ""}})
        res = {"at": _now}
        o = await db.orders.find_one({"order_number": "HB4153705034", "platform": "hepsiburada"}, {"_id": 0})
        if not o:
            res["hata"] = "sipariş yok"
        else:
            _uuid = (o.get("invoice_uuid") or "").strip()
            iptal = False
            if o.get("invoice_issued") and _uuid:
                ds = await db.settings.find_one({"id": "dogan_edonusum"}, {"_id": 0}) or {}
                from dogan_client import DoganClient
                from fastapi.concurrency import run_in_threadpool
                from zeep.helpers import serialize_object
                cli = DoganClient(username=ds["username"], password=ds["password"], is_test=ds.get("is_test", True))
                r = await run_in_threadpool(lambda: cli._get_earsiv_client().service.GetEArchiveInvoiceStatus(
                    REQUEST_HEADER=cli._make_header(), UUID=_uuid))
                ser = serialize_object(r) or {}
                inv = ((ser.get("INVOICE") or [{}])[0] or {}).get("HEADER") or {}
                res["eski_fatura"] = {"no": inv.get("INVOICE_ID"), "status": inv.get("STATUS"),
                                      "desc": inv.get("STATUS_DESC"), "profile": inv.get("PROFILE")}
                iptal = str(inv.get("STATUS")) == "201" or "İPTAL" in str(inv.get("STATUS_DESC") or "").upper() \
                    or str(inv.get("PROFILE") or "").upper() == "IPTAL"
            elif not o.get("invoice_issued"):
                iptal = True   # zaten sıfırlanmış
            if not iptal:
                res["hata"] = "eski fatura Doğan'da İPTAL görünmüyor — yeniden kesilmedi"
            else:
                await db.orders.update_one({"id": o["id"]}, {
                    "$set": {"invoice_issued": False, "hepsiburada_invoice_uploaded": False},
                    "$unset": {"invoice_number": "", "invoice_uuid": "", "invoice_type": "",
                               "invoice_provider": "", "invoice_provider_response": "",
                               "invoice_intl_txn_id": "", "invoice_dogan_id": "", "invoice_pdf_url": "",
                               "invoice_issued_at": "", "invoice_issued_by": "",
                               "invoice_last_error": "", "invoice_last_error_at": "",
                               "invoice_in_progress": "", "invoice_in_progress_at": "",
                               "hepsiburada_invoice_error": ""}})
                from routes.orders import create_invoice_for_order
                from fastapi import HTTPException as _HE
                try:
                    r2 = await create_invoice_for_order(
                        order_id=o["id"], invoice_type="auto",
                        current_user={"id": "system", "email": "sistem (yeniden kesim)", "is_admin": True})
                    res["yeni"] = {k: r2.get(k) for k in ("success", "invoice_number", "invoice_type", "message")}
                except _HE as he:
                    res["hata"] = f"fatura kesilemedi: {he.detail}"
                except Exception as e:
                    res["hata"] = f"fatura kesilemedi: {e}"
                o2 = await db.orders.find_one({"id": o["id"]}, {"_id": 0, "invoice_number": 1, "total": 1,
                                                               "hepsiburada_invoice_uploaded": 1,
                                                               "hepsiburada_invoice_error": 1,
                                                               "invoice_last_error": 1})
                res["son_durum"] = o2
        await db.settings.update_one({"id": _flag}, {"$set": {"id": _flag, "done": True, "sonuc": res}}, upsert=True)
        logger.info(f"[scheduler] HB4153705034 fatura yeniden kesim: {res}")
    except Exception as e:
        logger.warning(f"[scheduler] _ensure_hb4153_fatura_yeniden failed: {e}")


async def _ensure_body_indirim10():
    """TEK SEFERLİK (Kadir isteği 2026-09-29): Body kategorisindeki (5378) TÜM ürünlerin
    İNDİRİMLİ FİYAT alanı = etiket fiyatı × 0,90 (tam TL'ye aşağı yuvarlanır → en az %10).
    Yalnız sale_price (ve feed için varyantta dolu olan sale_price) değişir. Trendyol/Amazon
    tabanı (member_price_1 || price) etkilenmez; boş member_price_1 eski fiyatla sabitlenir.
    Body '3 AL 2 ÖDE' kampanyası skip_discounted=False → indirimli fiyatta da geçerli kalır."""
    import math
    from routes.deps import db  # lazy import
    _flag = "body_indirim10_v2"
    try:
        if ((await db.settings.find_one({"id": _flag}, {"_id": 0, "done": 1})) or {}).get("done"):
            return
        _now = datetime.now(timezone.utc).isoformat()
        q = {"$or": [{"category_ids": "5378"}, {"category_ids": 5378},
                     {"category_id": "5378"}, {"category_id": 5378}],
             "is_deleted": {"$ne": True}}
        items = []
        async for p in db.products.find(q, {"_id": 0, "id": 1, "stock_code": 1, "name": 1, "price": 1,
                                            "sale_price": 1, "member_price_1": 1, "variants": 1}):
            from routes.integrations_common import _to_float_tr
            price = _to_float_tr(p.get("price"))
            mp1 = _to_float_tr(p.get("member_price_1"))
            if price <= 0:
                continue
            # v2: v1'de member_price_1 metin ("1.162,00") olan ürünler atlanmıştı → yalnız eksikler.
            try:
                if p.get("sale_price") not in (None, "", 0) and abs(float(p["sale_price"]) - math.floor(price * 0.9)) < 0.01:
                    continue
            except Exception:
                pass
            sale = float(math.floor(price * 0.9))
            upd = {"sale_price": sale, "updated_at": _now}
            if mp1 <= 0:
                upd["member_price_1"] = price
            vs, touched = [], 0
            for v in (p.get("variants") or []):
                if isinstance(v, dict) and (v.get("price") not in (None, "", 0) or v.get("sale_price") not in (None, "", 0)):
                    v = dict(v)
                    try:
                        adj = float(v.get("price_adjustment") or v.get("price_diff") or 0)
                    except Exception:
                        adj = 0.0
                    v["sale_price"] = round(sale + adj, 2)
                    touched += 1
                vs.append(v)
            if touched:
                upd["variants"] = vs
            await db.products.update_one({"id": p["id"]}, {"$set": upd})
            items.append({"id": p["id"], "sc": p.get("stock_code"), "ad": p.get("name"), "fiyat": price,
                          "eski_indirimli": p.get("sale_price"), "yeni_indirimli": sale})
        await db.settings.update_one({"id": _flag}, {"$set": {"id": _flag, "done": True, "at": _now,
                                     "changed": len(items), "items": items}}, upsert=True)
        logger.info(f"[scheduler] Body %10 indirimli fiyat: {len(items)} ürün")
    except Exception as e:
        logger.warning(f"[scheduler] _ensure_body_indirim10 failed: {e}")


async def _ensure_sale_menu_seed():
    """TEK SEFERLİK (Kadir isteği 2026-09-29): SALE menüsünün ilk kalemleri — 1) "Özel Fiyatlar"
    (/sale → İNDİRİM kategorisindeki ürünler), 2) "3 Al 2 Öde" (Body kampanyası sayfası).
    Menü zaten kaydedilmişse DOKUNMAZ; sonrası Admin › Kampanyalar › SALE Menüsü'nden yönetilir."""
    from routes.deps import db  # lazy import
    try:
        if await db.settings.find_one({"id": "sale_menu"}, {"_id": 1}):
            return
        items = [{"id": "ozel-fiyatlar", "label": "Özel Fiyatlar", "type": "link", "link": "/sale",
                  "active": True, "order": 0}]
        c = await db.coupons.find_one({"code": "AUTO-BODY-3AL2"}, {"_id": 0, "id": 1})
        if c:
            items.append({"id": "3al2ode", "label": "3 Al 2 Öde", "type": "campaign", "campaign_id": c["id"],
                          "active": True, "order": 1})
        await db.settings.update_one({"id": "sale_menu"}, {"$setOnInsert": {
            "id": "sale_menu", "items": items, "updated_at": datetime.now(timezone.utc).isoformat(),
            "updated_by": "sistem (Kadir isteği)"}}, upsert=True)
        logger.info(f"[scheduler] SALE menüsü ilk kalemler: {len(items)}")
    except Exception as e:
        logger.warning(f"[scheduler] _ensure_sale_menu_seed failed: {e}")


async def _ensure_least_privilege_grants():
    """IAM-02 tek seferlik rol izin geçişinin yedek tetikleyicisi (bkz. admin_rbac)."""
    try:
        from routes.admin_rbac import grant_least_privilege_upgrade_permissions
        res = await grant_least_privilege_upgrade_permissions()
        if not res.get("skipped"):
            logger.warning(f"[scheduler] IAM-02 rol izin gecisi: {len(res.get('roles') or [])} rol guncellendi")
    except Exception as e:
        logger.warning(f"[scheduler] _ensure_least_privilege_grants failed: {e}")


async def _ensure_slug_kart_id():
    """TEK SEFERLİK (Kadir isteği 2026-09-29): ürün slug'ı HER ZAMAN {ad}-{ürün kart id}.
    Admin formu çok renkli üründe {ad}-{Date.now()} (ör. -1788251144324) gönderiyordu; ad
    değişiminde de renk-grubu id'si (csv_card_id) kullanılıyordu. Uymayan tüm ürünlerde slug
    düzeltilir, ESKİ slug slug_aliases'a eklenir (eski linkler/Meta/Google kırılmaz → ürün
    sayfası doğru slug'a yönlendirir). Başka ürünle çakışırsa dokunulmaz (listelenir)."""
    from routes.deps import db  # lazy import
    from routes.products import slug_with_card_id
    _flag = "slug_kart_id_v1"
    try:
        if ((await db.settings.find_one({"id": _flag}, {"_id": 0, "done": 1})) or {}).get("done"):
            return
        taken = {}
        async for p in db.products.find({}, {"_id": 0, "id": 1, "slug": 1, "slug_aliases": 1}):
            for sl in [p.get("slug")] + list(p.get("slug_aliases") or []):
                if sl:
                    taken.setdefault(str(sl), set()).add(p["id"])
        changed, clash = [], []
        async for p in db.products.find({"is_deleted": {"$ne": True}},
                                        {"_id": 0, "id": 1, "name": 1, "slug": 1, "slug_aliases": 1, "urun_karti_id": 1}):
            card = str(p.get("urun_karti_id") or "").strip()
            if not card or not p.get("name"):
                continue
            new = slug_with_card_id(p["name"], card)
            old = p.get("slug") or ""
            if new == old:
                continue
            owners = taken.get(new, set()) - {p["id"]}
            if owners:
                clash.append({"id": p["id"], "yeni": new})
                continue
            aliases = [a for a in (p.get("slug_aliases") or []) if a != new]
            if old and old not in aliases:
                aliases.append(old)
            await db.products.update_one({"id": p["id"]}, {"$set": {"slug": new, "slug_aliases": aliases}})
            taken.setdefault(new, set()).add(p["id"])
            changed.append({"id": p["id"], "eski": old, "yeni": new})
        await db.settings.update_one({"id": _flag}, {"$set": {"id": _flag, "done": True,
                                     "at": datetime.now(timezone.utc).isoformat(), "changed": len(changed),
                                     "clash": clash, "items": changed}}, upsert=True)
        logger.info(f"[scheduler] Slug kart id düzeltmesi: {len(changed)} ürün, çakışma {len(clash)}")
    except Exception as e:
        logger.warning(f"[scheduler] _ensure_slug_kart_id failed: {e}")


async def _ensure_fiyat_yukleme_0929():
    """TEK SEFERLİK (Kadir isteği 2026-09-29): 'Site_Fiyat_Yükleme' listesi — satış/indirimli fiyat
    + Sale'de Evet/Hayır → İNDİRİM kategorisine ekle/çıkar. Ayrıntı price_upload_0929 modülünde."""
    from routes.deps import db  # lazy import
    try:
        import price_upload_0929
        res = await price_upload_0929.apply(db)
        logger.info(f"[scheduler] Fiyat yükleme 0929: {res}")
        res2 = await price_upload_0929.apply_member_price(db)   # satış fiyatı → Üye Tipi 1
        logger.info(f"[scheduler] Fiyat yükleme 0929 üye tipi: {res2}")
        # EN GÜNCEL: eritme listesi (0929'dan SONRA uygulanır → aynı ürünlerde bu geçerli).
        import price_upload_eritme
        res3 = await price_upload_eritme.apply(db)
        logger.info(f"[scheduler] Fiyat yükleme eritme: {res3}")
    except Exception as e:
        logger.warning(f"[scheduler] _ensure_fiyat_yukleme_0929 failed: {e}")


async def _ensure_indirimli_temizle_body_haric():
    """TEK SEFERLİK (Kadir isteği 2026-09-29): Body (5378) HARİÇ tüm ürünlerde indirimli fiyat
    (sale_price) doluysa temizlenir; varyantlardaki dolu sale_price da temizlenir (feed).
    Etiket fiyatı, Üye Tipi fiyatı ve diğer alanlar değişmez."""
    from routes.deps import db  # lazy import
    _flag = "indirimli_temizle_body_haric_v1"
    try:
        if ((await db.settings.find_one({"id": _flag}, {"_id": 0, "done": 1})) or {}).get("done"):
            return
        _now = datetime.now(timezone.utc).isoformat()
        _empty = [None, "", 0, 0.0]
        q = {"is_deleted": {"$ne": True},
             "$nor": [{"category_ids": "5378"}, {"category_ids": 5378},
                      {"category_id": "5378"}, {"category_id": 5378}],
             "$or": [{"sale_price": {"$nin": _empty}}, {"variants.sale_price": {"$nin": _empty}}]}
        n, nv = 0, 0
        async for p in db.products.find(q, {"_id": 0, "id": 1, "sale_price": 1, "variants": 1}):
            upd = {"sale_price": None, "updated_at": _now}
            vs, touched = [], 0
            for v in (p.get("variants") or []):
                if isinstance(v, dict) and v.get("sale_price") not in _empty:
                    v = dict(v)
                    v["sale_price"] = None
                    touched += 1
                vs.append(v)
            if touched:
                upd["variants"] = vs
                nv += 1
            await db.products.update_one({"id": p["id"]}, {"$set": upd})
            n += 1
        await db.settings.update_one({"id": _flag}, {"$set": {"id": _flag, "done": True, "at": _now,
                                     "changed": n, "variant_products": nv}}, upsert=True)
        logger.info(f"[scheduler] İndirimli fiyat temizlendi (Body hariç): {n} ürün")
    except Exception as e:
        logger.warning(f"[scheduler] _ensure_indirimli_temizle_body_haric failed: {e}")


async def _ensure_vela_beden_fark_duzelt():
    """TEK SEFERLİK (kullanıcı, 2026-09-30): Vela Kareli Midi Etek Bej (kart 2729, id 2221) panelde
    1290 TL iken vitrinde 910 görünüyordu — XXS varyantında ESKİDEN KALMA −380 beden farkı vardı.
    Yalnız bu ürünün beden farkı sıfırlanır ve varyant fiyat alanları ürün fiyatına eşitlenir.
    (Tarama: diğer beden farklı ürünler kullanıcının eritme listesindeki Havlu Kumaş ürünleri — dokunulmaz.)"""
    from routes.deps import db  # lazy import
    _flag = "vela_beden_fark_duzelt_v1"
    try:
        if ((await db.settings.find_one({"id": _flag}, {"_id": 0, "done": 1})) or {}).get("done"):
            return
        p = await db.products.find_one({"id": "2221", "urun_karti_id": "2729"},
                                       {"_id": 0, "id": 1, "price": 1, "sale_price": 1, "variants": 1})
        if not p:
            await db.settings.update_one({"id": _flag}, {"$set": {"id": _flag, "done": True, "sonuc": "urun_yok"}}, upsert=True)
            return
        try:
            price = float(p.get("price") or 0)
        except Exception:
            price = 0.0
        try:
            sp = float(p.get("sale_price") or 0)
        except Exception:
            sp = 0.0
        sale = sp if 0 < sp < price else None
        vs, before = [], {}
        for v in (p.get("variants") or []):
            if isinstance(v, dict):
                v = dict(v)
                a = v.get("price_adjustment") or v.get("price_diff") or 0
                if a:
                    before[str(v.get("size") or "?")] = a
                v["price_adjustment"] = 0
                v["price_diff"] = 0
                v["price"] = round(price, 2)
                v["sale_price"] = round(sale, 2) if sale else None
            vs.append(v)
        now = datetime.now(timezone.utc).isoformat()
        await db.products.update_one({"id": "2221"}, {"$set": {"variants": vs, "updated_at": now}})
        await db.settings.update_one({"id": _flag}, {"$set": {"id": _flag, "done": True, "at": now,
                                     "fiyat": price, "indirimli": sale, "eski_beden_farki": before}}, upsert=True)
        logger.warning(f"[fiyat] Vela 2729 beden farkı sıfırlandı: {before}")
    except Exception as e:
        logger.warning(f"[scheduler] _ensure_vela_beden_fark_duzelt failed: {e}")


async def _ensure_2802_varyant_renk_siyah():
    """TEK SEFERLİK (kullanıcı, 2026-09-30): Suni Deri High Waist Pantolon Siyah (kart 2802, id 9738)
    ürünün her yerinde 'Siyah' iken varyantlarda renk 'Bej' kalmıştı → YALNIZ bu ürünün varyant
    rengi 'Siyah' yapılır (stok/fiyat/barkod değişmez). Sonuç: settings.varyant_renk_2802_v1."""
    from routes.deps import db  # lazy import
    _flag = "varyant_renk_2802_v1"
    try:
        if ((await db.settings.find_one({"id": _flag}, {"_id": 0, "done": 1})) or {}).get("done"):
            return
        p = await db.products.find_one({"id": "9738"}, {"_id": 0, "id": 1, "urun_karti_id": 1, "color": 1, "variants": 1})
        if not p or str(p.get("urun_karti_id")) != "2802":
            await db.settings.update_one({"id": _flag}, {"$set": {"id": _flag, "done": True, "sonuc": "urun_yok"}}, upsert=True)
            return
        eski, vs = [], []
        for v in (p.get("variants") or []):
            if isinstance(v, dict):
                v = dict(v)
                eski.append(v.get("color"))
                v["color"] = "Siyah"
            vs.append(v)
        now = datetime.now(timezone.utc).isoformat()
        await db.products.update_one({"id": "9738"}, {"$set": {"variants": vs, "updated_at": now}})
        await db.settings.update_one({"id": _flag}, {"$set": {"id": _flag, "done": True, "at": now,
                                     "eski_renkler": eski}}, upsert=True)
        logger.warning(f"[katalog] 2802 varyant rengi Siyah yapıldı (eski: {eski})")
    except Exception as e:
        logger.warning(f"[scheduler] _ensure_2802_varyant_renk_siyah failed: {e}")


async def _ensure_noctia_somon_renk():
    """TEK SEFERLİK (kullanıcı onayı, 2026-09-30): Noctia Pelerinli Ceket Somon (kart 2751) ürün
    rengi alanında 'Ceket Somon' yazıyordu (varyantlar 'Somon') → YALNIZ bu ürünün rengi 'Somon'."""
    from routes.deps import db  # lazy import
    _flag = "noctia_somon_renk_v1"
    _pid = "0b5e1d88-8e4e-4a64-af54-0469439139d7"
    try:
        if ((await db.settings.find_one({"id": _flag}, {"_id": 0, "done": 1})) or {}).get("done"):
            return
        p = await db.products.find_one({"id": _pid}, {"_id": 0, "color": 1, "urun_karti_id": 1})
        now = datetime.now(timezone.utc).isoformat()
        if p and str(p.get("urun_karti_id")) == "2751" and str(p.get("color") or "").strip() == "Ceket Somon":
            await db.products.update_one({"id": _pid}, {"$set": {"color": "Somon", "updated_at": now}})
            res = "duzeltildi"
        else:
            res = "atlandi"
        await db.settings.update_one({"id": _flag}, {"$set": {"id": _flag, "done": True, "at": now,
                                     "sonuc": res, "eski": (p or {}).get("color")}}, upsert=True)
    except Exception as e:
        logger.warning(f"[scheduler] _ensure_noctia_somon_renk failed: {e}")


async def _ensure_havlu_etek_950():
    """TEK SEFERLİK (kullanıcı, 2026-09-30: "eteğin bedenini 950 yap, fiyatını"): Havlu Kumaş Etek Bej
    (id 5483, L 490) ve Havlu Kumaş Etek Pembe (id 4235, S+L 490) — eritme listesinde olmayan eski
    beden farkı sıfırlanır; tüm bedenler ürünün indirimli fiyatıyla (950) satılır. YALNIZ bu 2 ürün."""
    from routes.deps import db  # lazy import
    _flag = "havlu_etek_950_v1"
    try:
        if ((await db.settings.find_one({"id": _flag}, {"_id": 0, "done": 1})) or {}).get("done"):
            return
        now = datetime.now(timezone.utc).isoformat()
        out = {}
        for pid in ("5483", "4235"):
            p = await db.products.find_one({"id": pid}, {"_id": 0, "price": 1, "sale_price": 1, "variants": 1, "name": 1})
            if not p or "Havlu Kumaş Etek" not in str(p.get("name") or ""):
                out[pid] = "atlandi"
                continue
            try:
                price = float(p.get("price") or 0)
                sp = float(p.get("sale_price") or 0)
            except Exception:
                out[pid] = "fiyat_okunamadi"
                continue
            sale = sp if 0 < sp < price else None
            vs, eski = [], {}
            for v in (p.get("variants") or []):
                if isinstance(v, dict):
                    v = dict(v)
                    a = v.get("price_adjustment") or v.get("price_diff") or 0
                    if a:
                        eski[str(v.get("size") or "?")] = a
                    v["price_adjustment"] = 0
                    v["price_diff"] = 0
                    if v.get("price") not in (None, "", 0) or v.get("sale_price") not in (None, "", 0) or a:
                        v["price"] = round(price, 2)
                        v["sale_price"] = round(sale, 2) if sale else None
                vs.append(v)
            await db.products.update_one({"id": pid}, {"$set": {"variants": vs, "updated_at": now}})
            out[pid] = {"fiyat": price, "indirimli": sale, "eski_beden_farki": eski}
        await db.settings.update_one({"id": _flag}, {"$set": {"id": _flag, "done": True, "at": now, "sonuc": out}}, upsert=True)
        logger.warning(f"[fiyat] Havlu Kumaş Etek beden farkı sıfırlandı: {out}")
    except Exception as e:
        logger.warning(f"[scheduler] _ensure_havlu_etek_950 failed: {e}")


async def _ensure_amazon_otomatik_kurulum():
    """TEK SEFERLİK (kullanıcı, 2026-10-01: "Amazon'da tüm kategorileri ve özellik değerlerini ayarla,
    aktarılmamış tüm ürünleri aktar … sen kendin çalıştır"): eşlemesiz kategorilere Amazon ürün tipi
    (mevcut eşlemelere dokunmaz) + Amazon'da olmayan / eksik bedenli, stoğu olan ürünleri mevcut
    aktarımla gönderir. Sonuç Entegrasyon Logları'nda (category_auto_map, bulk_listing).
    Bayrak İŞ BAŞINDA yazılır: yeniden başlatmada aktarım kendiliğinden ikinci kez başlamaz."""
    from routes.deps import db  # lazy import
    _flag = "amazon_otomatik_kurulum_v14"  # v2-v12 (v11: Ceket → BLAZER; v12: kullanıcı onaylı mantıklı varsayılanlar): önceki çalıştırmalar deploy ile yarıda kesildi
    try:
        if ((await db.settings.find_one({"id": _flag}, {"_id": 0, "done": 1})) or {}).get("done"):
            return
        await db.settings.update_one({"id": _flag}, {"$set": {"id": _flag, "done": True,
                                      "at": datetime.now(timezone.utc).isoformat()}}, upsert=True)
        from routes.amazon_autosetup import run_full_auto_setup
        res = await run_full_auto_setup("sistem (kullanıcı isteği)")
        await db.settings.update_one({"id": _flag}, {"$set": {"kategori": res.get("summary")}})
        logger.warning(f"[amazon] otomatik kurulum bitti: {res.get('summary')}")
    except Exception as e:
        logger.warning(f"[scheduler] _ensure_amazon_otomatik_kurulum failed: {e}")


async def _ensure_amazon_katalog_tazele():
    """TEK SEFERLİK (2026-10-01): toplu gönderimden ~2 saat sonra Amazon kataloğunu yeniden çek —
    kullanıcı "son durum nedir" diye sordu; yalnız OKUMA (Amazon'a yazmaz)."""
    from routes.deps import db  # lazy import
    _flag = "amazon_katalog_tazele_v1"
    try:
        if ((await db.settings.find_one({"id": _flag}, {"_id": 0, "done": 1})) or {}).get("done"):
            return
        await db.settings.update_one({"id": _flag}, {"$set": {"id": _flag, "done": True,
                                      "at": datetime.now(timezone.utc).isoformat()}}, upsert=True)
        from routes.amazon_catalog import run_catalog_sync
        await run_catalog_sync()
    except Exception as e:
        logger.warning(f"[scheduler] _ensure_amazon_katalog_tazele failed: {e}")


async def _ensure_amazon_pasif_teshis():
    """TEK SEFERLİK (2026-10-01, kullanıcı: "çıkar"): stoğu olup Amazon'da pasif SKU'ların Amazon
    gerekçelerini oku (yalnız okuma)."""
    from routes.deps import db  # lazy import
    _flag = "amazon_pasif_teshis_v1"
    try:
        if ((await db.settings.find_one({"id": _flag}, {"_id": 0, "done": 1})) or {}).get("done"):
            return
        await db.settings.update_one({"id": _flag}, {"$set": {"id": _flag, "done": True}}, upsert=True)
        from routes.amazon_autosetup import diagnose_inactive_in_stock
        await diagnose_inactive_in_stock()
    except Exception as e:
        logger.warning(f"[scheduler] _ensure_amazon_pasif_teshis failed: {e}")


async def _ensure_amazon_volanli_bluz_yeniden():
    """TEK SEFERLİK (2026-10-01, kullanıcı: eksik kalanları aktar): Volanlı Bluz (FCFW0100006) ailesi
    Amazon'da pasif (13013 / 404) → mevcut aktarımla yeniden gönder + 10 dk sonra durum oku."""
    from routes.deps import db  # lazy import
    _flag = "amazon_volanli_bluz_yeniden_v1"
    try:
        if ((await db.settings.find_one({"id": _flag}, {"_id": 0, "done": 1})) or {}).get("done"):
            return
        await db.settings.update_one({"id": _flag}, {"$set": {"id": _flag, "done": True}}, upsert=True)
        from routes.amazon_autosetup import repush_and_recheck
        await repush_and_recheck("FCFW0100006")
    except Exception as e:
        logger.warning(f"[scheduler] _ensure_amazon_volanli_bluz_yeniden failed: {e}")


async def _ensure_seo_varsayilan_doldur():
    """TEK SEFERLİK (2026-10-02, kullanıcı: "SEO'da kategorilerimiz çıkmıyor, çıkmasını sağla"):
    tenant_config.seo_geo'da BOŞ (veya model varsayılanında kalmış) alanlar, mağazanın hazır SEO
    tohumundaki değerlerle doldurulur. Panelden elle girilmiş HİÇBİR değer değiştirilmez."""
    from routes.deps import db  # lazy import
    _flag = "seo_varsayilan_doldur_v1"
    try:
        if ((await db.settings.find_one({"id": _flag}, {"_id": 0, "done": 1})) or {}).get("done"):
            return
        import tenant_config as _tc
        doc = await db.settings.find_one({"id": "tenant_config"}, {"_id": 0}) or {}
        cur = dict(doc.get("seo_geo") or {})
        seed = (_tc.migration_seed(None).get("seo_geo") or {})
        model_default = _tc.TenantConfig().model_dump().get("seo_geo") or {}
        store = ((doc.get("brand") or {}).get("store_name") or "").strip()
        seed_store = "ATILLA"
        upd = {}
        for k in ("default_title", "default_description", "organization_description",
                  "llms_description", "default_og_image_url", "category_description_template"):
            v = cur.get(k)
            if (v in (None, "") or v == model_default.get(k)) and seed.get(k) and seed.get(k) != v:
                # Tohum yalnız AYNI mağaza için geçerli (beyaz etikette başka marka adı yazılmaz).
                if seed_store in str(seed[k]) and store.upper() != seed_store:
                    continue
                upd[f"seo_geo.{k}"] = seed[k]
        if upd:
            await db.settings.update_one({"id": "tenant_config"}, {"$set": upd})
            try:
                _tc.invalidate(db)
            except Exception:
                pass
        await db.settings.update_one({"id": _flag}, {"$set": {"id": _flag, "done": True,
                                      "at": datetime.now(timezone.utc).isoformat(), "set": list(upd)}}, upsert=True)
        logger.warning(f"[seo] varsayılan SEO alanları dolduruldu: {list(upd)}")
    except Exception as e:
        logger.warning(f"[scheduler] _ensure_seo_varsayilan_doldur failed: {e}")


async def _ensure_hb4029_gider_pusulasi_duzelt():
    """TEK SEFERLİK (kullanıcı, 2026-10-02): HB4029270518'in faturası doğru tutarla YENİDEN kesildi
    (eski FCT2026000020776 iptal) ama gider pusulası hâlâ eski faturaya bağlıydı. Pusula ve iade
    (claim) kayıtlarındaki satış faturası no = siparişin GEÇERLİ faturası; tam iadede net = sipariş
    (yeni fatura) tutarı, kısmide HB iade tutarı; tüm kardeş claim kalemleri dahil. Pusula NUMARASI
    korunur, stoğa DOKUNULMAZ, eski değerler _fix_backup'ta. Sonuç Entegrasyon Logları'nda."""
    from routes.deps import db  # lazy import
    _no, _flag = "HB4029270518", "hb4029_gider_pusulasi_duzelt_v1"
    try:
        if ((await db.settings.find_one({"id": _flag}, {"_id": 0, "done": 1})) or {}).get("done"):
            return
        await db.settings.update_one({"id": _flag}, {"$set": {"id": _flag, "done": True}}, upsert=True)
        from routes.integrations_common import _claim_bucket, log_integration_event
        now = datetime.now(timezone.utc).isoformat()
        order = await db.orders.find_one({"order_number": _no}, {"_id": 0}) or {}
        inv_no = str(order.get("invoice_number") or "").strip()
        claims = [c for c in await db.trendyol_claims.find({"order_number": _no}, {"_id": 0}).to_list(None)
                  if _claim_bucket(c) != "iptal"]
        gps = await db.gider_pusulasi.find({"order_number": _no}, {"_id": 0}).to_list(None)
        res = {"at": now, "fatura": inv_no, "claim": len(claims), "pusula": len(gps)}
        if not order or not inv_no or not gps:
            res["durum"] = "sipariş/fatura/pusula bulunamadı — değişiklik yok"
        else:
            items_all = []
            refund = 0.0
            for c in claims:
                items_all.extend(c.get("items") or [])
                refund += float(c.get("refund_amount") or 0)
            ret_units = sum(int(i.get("quantity", 1) or 1) for i in items_all)
            ord_units = sum(int(i.get("quantity", 1) or 1) for i in (order.get("items") or []))
            full = bool(ord_units) and ret_units >= ord_units
            net = round(float(order.get("total") or 0), 2) if full else round(refund, 2)
            if net <= 0:
                net = round(refund or float(order.get("total") or 0), 2)
            gp = sorted(gps, key=lambda g: g.get("created_at") or "")[-1]
            vat_rate = (gp.get("totals") or {}).get("vat_rate") or 10
            # Kalemler: mevcut pusula kalem yapısı korunur, eksik kardeş claim kalemleri eklenir
            gp_items = list(gp.get("items") or [])
            have = {str(i.get("barcode") or "") for i in gp_items}
            for it in items_all:
                bc = str(it.get("barcode") or "").strip()
                if bc and bc in have:
                    continue
                have.add(bc)
                gp_items.append({"name": it.get("productName", ""), "barcode": bc, "size": "",
                                 "quantity": it.get("quantity", 1), "unit_price": it.get("unit_price", 0),
                                 "discount": it.get("discount_amount", 0), "net_price": it.get("price", 0),
                                 "reason": it.get("reason", "")})
            if gp_items and net > 0:
                cur = round(sum(round(float(g.get("net_price", 0) or 0), 2) * int(g.get("quantity", 1) or 1)
                                for g in gp_items), 2)
                diff = round(net - cur, 2)
                if abs(diff) >= 0.01:
                    last = gp_items[-1]
                    q = int(last.get("quantity", 1) or 1)
                    last["net_price"] = round(float(last.get("net_price", 0) or 0) + diff / q, 2)
                    last["discount"] = round(max(0.0, float(last.get("unit_price", 0) or 0) - last["net_price"]), 2)
            vat = round(net * vat_rate / (100 + vat_rate), 2)
            upd = {"sales_invoice_no": inv_no, "items": gp_items,
                   "totals.net": net, "totals.vat_amount": vat, "totals.net_without_vat": round(net - vat, 2),
                   "totals.gross": round(sum(float(g.get("unit_price", 0) or 0) * int(g.get("quantity", 1) or 1) for g in gp_items), 2),
                   "fixed_at": now, "fix_reason": "fatura yeniden kesildi — pusula yeni faturaya bağlandı",
                   "_fix_backup": {"sales_invoice_no": gp.get("sales_invoice_no"), "items": gp.get("items"),
                                   "totals": gp.get("totals")}}
            key = {"claim_id": gp["claim_id"]} if gp.get("claim_id") else {"order_number": _no, "number": gp.get("number")}
            await db.gider_pusulasi.update_one(key, {"$set": upd})
            await db.trendyol_claims.update_many({"order_number": _no}, {"$set": {"invoice_number": inv_no}})
            res.update({"pusula_no": gp.get("display_number"), "eski_fatura": gp.get("sales_invoice_no"),
                        "eski_net": (gp.get("totals") or {}).get("net"), "yeni_net": net, "tam_iade": full,
                        "kalem": len(gp_items), "iade_tutari_hb": round(refund, 2),
                        "siparis_tutari": order.get("total")})
        await db.settings.update_one({"id": _flag}, {"$set": {"sonuc": res}})
        await log_integration_event(
            "hepsiburada", "gider_pusulasi_duzelt", "order", _no, "success" if res.get("pusula_no") else "error",
            (f"{_no} gider pusulası {res.get('pusula_no')}: fatura {res.get('eski_fatura')} → {res.get('fatura')}, "
             f"net {res.get('eski_net')} → {res.get('yeni_net')} TL ({'tam' if res.get('tam_iade') else 'kısmi'} iade, "
             f"{res.get('kalem')} kalem; HB iade tutarı {res.get('iade_tutari_hb')}, sipariş {res.get('siparis_tutari')})")
            if res.get("pusula_no") else f"{_no}: {res.get('durum')}")
        logger.warning(f"[gp] HB4029270518 pusula düzeltme: {res}")
    except Exception as e:
        logger.warning(f"[scheduler] _ensure_hb4029_gider_pusulasi_duzelt failed: {e}")


async def _ensure_hb4029_pusula_tutar_duzelt():
    """TEK SEFERLİK (kullanıcı onayı, 2026-10-02: "faturayı daha önce düzelttik, sen gider pusulasını
    düzelt"): HB4029270518 gider pusulası — HB OMS her satıra sipariş toplamını (6.112,50) yazmıştı;
    gerçek 2 × 3.056,25 = 6.112,50. YALNIZ gider_pusulasi kaydı değişir (sipariş, iade kaydı, fatura
    DEĞİŞMEZ). Pusula/fatura numarası korunur; eski değerler _fix_backup_tutar alanında."""
    from routes.deps import db  # lazy import
    _no, _flag = "HB4029270518", "hb4029_pusula_tutar_duzelt_v1"
    _wrong, _right = 6112.5, 3056.25
    try:
        if ((await db.settings.find_one({"id": _flag}, {"_id": 0, "done": 1})) or {}).get("done"):
            return
        await db.settings.update_one({"id": _flag}, {"$set": {"id": _flag, "done": True}}, upsert=True)
        from routes.integrations_common import log_integration_event
        now = datetime.now(timezone.utc).isoformat()
        out = []
        async for g in db.gider_pusulasi.find({"order_number": _no}, {"_id": 0}):
            gi = []
            for it in (g.get("items") or []):
                it = dict(it)
                for k in ("unit_price", "net_price"):
                    try:
                        if abs(float(it.get(k) or 0) - _wrong) < 0.005:
                            it[k] = _right
                    except Exception:
                        pass
                it["discount"] = round(max(0.0, float(it.get("unit_price", 0) or 0) - float(it.get("net_price", 0) or 0)), 2)
                gi.append(it)
            net = round(sum(float(i.get("net_price", 0) or 0) * int(i.get("quantity", 1) or 1) for i in gi), 2)
            vr = (g.get("totals") or {}).get("vat_rate") or 10
            vat = round(net * vr / (100 + vr), 2)
            key = {"claim_id": g["claim_id"]} if g.get("claim_id") else {"order_number": _no, "number": g.get("number")}
            await db.gider_pusulasi.update_one(key, {"$set": {
                "items": gi, "totals.net": net, "totals.gross": net, "totals.discount": 0,
                "totals.vat_amount": vat, "totals.net_without_vat": round(net - vat, 2),
                "fixed_at": now, "fix_reason": "HB satır fiyatı sipariş toplamıyla gelmişti — 2 × 3.056,25",
                "_fix_backup_tutar": {"items": g.get("items"), "totals": g.get("totals"), "at": now}}})
            out.append({"no": g.get("display_number"), "eski_net": (g.get("totals") or {}).get("net"),
                        "yeni_net": net, "kdv": vat, "kalem": len(gi)})
        await db.settings.update_one({"id": _flag}, {"$set": {"sonuc": out, "at": now}})
        await log_integration_event("hepsiburada", "gider_pusulasi_tutar_duzelt", "order", _no,
                                    "success" if out else "error",
                                    f"{_no} gider pusulası tutar düzeltmesi: {out or 'pusula bulunamadı'}")
        logger.warning(f"[gp] HB4029270518 pusula tutar düzeltme: {out}")
    except Exception as e:
        logger.warning(f"[scheduler] _ensure_hb4029_pusula_tutar_duzelt failed: {e}")


async def _ensure_hb4029_iade_siparis_tutar_duzelt_v2():
    """TEK SEFERLİK v2 (kullanıcı 2026-10-06: "bunu da yapmıştın hala aynı görünüyor"): v1 düzeltmesi
    iade kayıtlarını `amount_overridden` ile KİLİTLEMEDİĞİ için 30 dk'lık HB iade senkronu tutarları
    HB'nin hatalı satır fiyatıyla (6.112,50) geri yazdı. Aynı düzeltme yeniden uygulanır ve iade
    kayıtları kilitlenir (senkron artık tutar/kalem EZMEZ; durum/kargo tazelenmeye devam eder)."""
    await _ensure_hb4029_iade_siparis_tutar_duzelt(flag="hb4029_iade_siparis_tutar_duzelt_v2", lock=True)


async def _ensure_hb4029_iade_siparis_tutar_duzelt(flag: str = "hb4029_iade_siparis_tutar_duzelt_v1",
                                                    lock: bool = False):
    """TEK SEFERLİK (kullanıcı onayı, 2026-10-03: "evet ikisini de düzelt hb siparişi için"):
    HB4029270518 — HB iade (claim) kayıtları ve sipariş kalemleri her satırda sipariş toplamını
    (6.112,50) taşıyordu; gerçek 2 × 3.056,25 = 6.112,50. Claim tutar/kalem fiyatları + sipariş kalem
    fiyatları ve toplamları düzeltilir. FATURA ve gider pusulası numarasına dokunulmaz; eski değerler
    _fix_backup_tutar alanında. Sonuç Entegrasyon Logları'nda."""
    from routes.deps import db  # lazy import
    _no, _flag = "HB4029270518", flag
    _wrong, _right, _wrong_tot, _right_tot = 6112.5, 3056.25, 12225.0, 6112.5

    def _eq(v, t):
        try:
            return not isinstance(v, bool) and abs(float(v) - t) < 0.005
        except Exception:
            return False
    try:
        if ((await db.settings.find_one({"id": _flag}, {"_id": 0, "done": 1})) or {}).get("done"):
            return
        await db.settings.update_one({"id": _flag}, {"$set": {"id": _flag, "done": True}}, upsert=True)
        from routes.integrations_common import log_integration_event
        now = datetime.now(timezone.utc).isoformat()
        res = {"at": now, "claim": 0}
        async for c in db.trendyol_claims.find({"order_number": _no}, {"_id": 0}):
            # İlk yedek (v1) varsa EZİLMEZ; sonraki turun yedeği ayrı anahtara yazılır.
            _bk = "_fix_backup_tutar" if not c.get("_fix_backup_tutar") else f"_fix_backup_tutar_{flag[-2:]}"
            upd = {_bk: {"refund_amount": c.get("refund_amount"), "items": c.get("items"), "at": now}}
            for k in ("refund_amount", "total_amount", "amount"):
                if _eq(c.get(k), _wrong):
                    upd[k] = _right
            its = []
            for it in (c.get("items") or []):
                it = dict(it)
                for k in ("price", "unit_price", "total_price", "amount", "refund_amount"):
                    if _eq(it.get(k), _wrong):
                        it[k] = _right
                its.append(it)
            upd["items"] = its
            if lock:
                upd["amount_overridden"] = True       # HB senkronu tutar/kalemleri bir daha ezmesin
                upd["amount_overridden_reason"] = "HB satır fiyatı sipariş toplamıyla geliyordu (kullanıcı onaylı düzeltme)"
            await db.trendyol_claims.update_one({"claim_id": c["claim_id"]}, {"$set": upd})
            res["claim"] += 1
        o = await db.orders.find_one({"order_number": _no}, {"_id": 0})
        if o:
            items = []
            for it in (o.get("items") or []):
                it = dict(it)
                for k, v in list(it.items()):
                    if isinstance(v, (int, float)) and _eq(v, _wrong):
                        it[k] = _right
                items.append(it)
            top = {k: _right_tot for k in ("subtotal", "total", "items_total", "grand_total", "net_total",
                                             "order_total", "marketplace_total", "paid_total")
                   if _eq(o.get(k), _wrong_tot)}
            if items != (o.get("items") or []) or top:   # zaten doğruysa sipariş (ve yedeği) dokunulmaz
                _obk = "_fix_backup_tutar" if not o.get("_fix_backup_tutar") else f"_fix_backup_tutar_{flag[-2:]}"
                await db.orders.update_one({"order_number": _no}, {"$set": {
                    "items": items, **top, "updated_at": now,
                    _obk: {"items": o.get("items"), **{k: o.get(k) for k in top}, "at": now}}})
            res["siparis_toplam_alanlari"] = sorted(top)
        await db.settings.update_one({"id": _flag}, {"$set": {"sonuc": res}})
        await log_integration_event("hepsiburada", "iade_siparis_tutar_duzelt", "order", _no, "success",
                                    f"{_no}: iade kayıtları ve sipariş kalemleri 2 × 3.056,25 = 6.112,50 TL — {res}")
        logger.warning(f"[hb] HB4029270518 iade/sipariş tutar düzeltme: {res}")
    except Exception as e:
        logger.warning(f"[scheduler] _ensure_hb4029_iade_siparis_tutar_duzelt failed: {e}")


async def _run_r2_files_migration():
    """KULLANICI ONAYI (2026-10-03, "R2'ye taşı, diğerlerini yapma"): db.files içinde base64 saklanan
    GÖRSEL ve VİDEOLAR Cloudflare R2'ye taşınır (Atlas boyutu/yedek/aktarım maliyeti). Her dosya:
    R2'ye yükle → R2'den GERİ OKU, boyut birebir aynı mı doğrula → ancak o zaman r2_url yazılıp
    data_b64 silinir (site zaten r2_url varsa R2'den sunar). Doğrulanamayan dosyanın base64'ü KORUNUR.
    PDF/fatura, dekont ve silinmiş dosyalara DOKUNULMAZ. Tek tek, yavaş (siteyi yormaz); yeniden
    başlatmada kaldığı yerden devam eder; bitince bayrak + Entegrasyon Logları."""
    from routes.deps import db  # lazy import
    import base64 as _b64
    _ID = "r2_files_migration_v1"
    try:
        st = await db.settings.find_one({"id": _ID}, {"_id": 0}) or {}
        from services import r2_storage as r2
        from routes.integrations_common import log_integration_event
        q = {"data_b64": {"$exists": True, "$nin": [None, ""]},
             "content_type": {"$regex": "^(image|video)/"},
             "is_deleted": {"$ne": True},
             # Başarısız olanlar 5 denemeye kadar YENİDEN denenir (R2 geçici hata / istemci ayarı
             # düzeldikten sonra kalıcı olarak dışarıda kalmasınlar).
             "r2_mig_attempts": {"$not": {"$gte": 5}}}
        # TEKRARLI (30 dk): ilk taşıma bittikten sonra da R2 yüklemesi geçici olarak başarısız olup
        # veritabanına düşen YENİ görseller (yükleme yedeği) R2'ye taşınır. Bekleyen yoksa hiçbir şey yapmaz.
        if not await db.files.count_documents(q, limit=1):
            await _relink_product_images_to_r2(db)
            return
        _hc = (await asyncio.to_thread(r2.health_check)) if r2.is_enabled() else {"ok": False, "error": "yapılandırma eksik"}
        if not _hc.get("ok"):
            logger.warning("[r2-mig] R2 yapılandırılmamış / erişilemiyor — taşıma başlamadı")
            _why = r2.explain_error(Exception(_hc.get("error") or ""))
            await log_integration_event("r2", "files_migration", "bulk", "", "error",
                                        "R2 görsel deposuna erişilemiyor: yeni yüklenen görseller veritabanında "
                                        f"bekliyor. Neden: {_why}")
            return
        now = lambda: datetime.now(timezone.utc).isoformat()  # noqa: E731
        moved = int(st.get("moved") or 0)
        failed = int(st.get("failed") or 0)
        freed = int(st.get("bytes_freed") or 0)
        await db.settings.update_one({"id": _ID}, {"$set": {"id": _ID, "status": "running",
                                     "started_at": st.get("started_at") or now(), "updated_at": now()}}, upsert=True)
        remaining = await db.files.count_documents(q)
        await log_integration_event("r2", "files_migration", "bulk", str(remaining), "info",
                                    f"R2 taşıma başladı/devam: {remaining} görsel/video kaldı (taşınan {moved}, hata {failed})")
        last_id = None
        while True:
            qq = dict(q)
            if last_id is not None:
                qq["_id"] = {"$gt": last_id}
            batch = await db.files.find(qq, {"_id": 1, "storage_path": 1, "content_type": 1, "data_b64": 1,
                                             "r2_key": 1, "id": 1}).sort("_id", 1).limit(20).to_list(20)
            if not batch:
                break
            for doc in batch:
                last_id = doc["_id"]
                try:
                    data = _b64.b64decode(doc["data_b64"])
                    key = doc.get("r2_key") or f"uploads/{doc.get('storage_path') or doc.get('id')}"
                    got, _ct = await asyncio.to_thread(r2.get_object, key)
                    if got is None or len(got) != len(data):
                        await asyncio.to_thread(r2.put_object, key, data, doc.get("content_type") or "application/octet-stream")
                        got, _ct = await asyncio.to_thread(r2.get_object, key)
                    if got is None or len(got) != len(data):
                        raise RuntimeError("R2 doğrulama başarısız (boyut uyuşmuyor)")
                    await db.files.update_one({"_id": doc["_id"]}, {
                        "$set": {"r2_key": key, "r2_url": r2.public_url(key), "r2_migrated_at": now()},
                        "$unset": {"data_b64": ""}})
                    moved += 1
                    freed += len(doc["data_b64"])
                except Exception as e:
                    failed += 1
                    await db.files.update_one({"_id": doc["_id"]}, {"$set": {"r2_mig_failed": True,
                                                                             "r2_mig_error": r2.explain_error(e)[:200]},
                                                                    "$inc": {"r2_mig_attempts": 1}})
                await asyncio.sleep(0.15)
            await db.settings.update_one({"id": _ID}, {"$set": {"moved": moved, "failed": failed,
                                         "bytes_freed": freed, "updated_at": now()}})
            if moved and moved % 500 < 20:
                await log_integration_event("r2", "files_migration", "bulk", str(moved), "info",
                                            f"R2 taşıma sürüyor: {moved} taşındı, {failed} hata, "
                                            f"~{freed / 1024 / 1024:.0f} MB veritabanından boşaldı")
        await db.settings.update_one({"id": _ID}, {"$set": {"done": True, "status": "done", "finished_at": now(),
                                     "moved": moved, "failed": failed, "bytes_freed": freed}})
        await log_integration_event("r2", "files_migration", "bulk", str(moved), "success" if not failed else "error",
                                    f"R2 taşıma bitti: {moved} görsel/video R2'ye taşındı, {failed} hata "
                                    f"(hatalıların veritabanı kopyası korundu), ~{freed / 1024 / 1024:.0f} MB boşaldı")
        logger.warning(f"[r2-mig] bitti: moved={moved} failed={failed} freed={freed}")
        await _relink_product_images_to_r2(db)
    except Exception as e:
        logger.warning(f"[scheduler] _run_r2_files_migration failed: {e}")


async def _audit_repair_product_images(db, min_card_id: int = 3000, days: int = 90, limit_products: int = 600) -> dict:
    """KULLANICI TALEBİ ("eksik olanları tespit ederek direkt taşı"): yeni ürünlerin HER görselini
    sunucudan açıp dener. Açılmayan (HTTP≠200 ya da görsel değil) görselin dosyası veritabanında
    (db.files) bulunursa R2'ye yüklenir ve ürün görsel adresi R2 CDN adresine çevrilir (aynı görsel,
    sıra + ölçü-tablosu işareti korunur). Bulunamayanlar ürün kart no + sıra ile raporlanır.
    Sonuç: Entegrasyon Logları (r2 / image_audit) + settings.image_audit_last."""
    import re as _re
    import base64 as _b64
    from datetime import timedelta as _td
    from services import r2_storage as r2
    from security_fetch import safe_fetch
    from routes.integrations_common import log_integration_event
    since = (datetime.now(timezone.utc) - _td(days=days)).isoformat()
    q = {"is_deleted": {"$ne": True}, "images.0": {"$exists": True}}
    checked = broken = repaired = 0
    lost, repaired_cards, sample = [], set(), []
    cache: dict = {}

    async def _ok(url: str) -> bool:
        if url in cache:
            return cache[url]
        good = False
        try:
            r = await safe_fetch(url, max_bytes=64 * 1024, timeout=15, allow_redirects=3,
                                 headers={"Range": "bytes=0-1023"})
            good = r.status_code in (200, 206) and r.content_type.lower().startswith(("image/", "video/"))
        except Exception:
            good = False
        cache[url] = good
        return good

    async def _repair(url: str) -> str:
        name = (url.split("?")[0].rstrip("/").rsplit("/", 1)[-1] or "").strip()
        if not name:
            return ""
        f = await db.files.find_one({"$or": [{"storage_path": name}, {"r2_key": f"uploads/{name}"}],
                                     "is_deleted": {"$ne": True}},
                                    {"_id": 1, "storage_path": 1, "data_b64": 1, "content_type": 1, "r2_key": 1, "r2_url": 1})
        if not f:
            return ""
        if f.get("r2_url") and str(f["r2_url"]).startswith("http") and await _ok(f["r2_url"]):
            return f["r2_url"]
        if not f.get("data_b64") or not r2.is_enabled():
            return ""
        data = _b64.b64decode(f["data_b64"])
        key = f.get("r2_key") or f"uploads/{f.get('storage_path') or name}"
        await asyncio.to_thread(r2.put_object, key, data, f.get("content_type") or "image/webp")
        got, _ = await asyncio.to_thread(r2.get_object, key)
        if got is None or len(got) != len(data):
            return ""
        u = r2.public_url(key)
        await db.files.update_one({"_id": f["_id"]}, {"$set": {"r2_key": key, "r2_url": u,
                                                               "r2_migrated_at": datetime.now(timezone.utc).isoformat()}})
        return u

    n = 0
    per_card: list = []
    async for p in db.products.find(q, {"_id": 0, "id": 1, "urun_karti_id": 1, "name": 1, "images": 1,
                                        "created_at": 1}).sort("created_at", -1):
        cid = str(p.get("urun_karti_id") or "").strip()
        if not ((cid.isdigit() and int(cid) >= min_card_id) or str(p.get("created_at") or "") >= since):
            continue
        n += 1
        if n > limit_products:
            break
        imgs = p.get("images") or []
        new, changed = [], False
        _pc = {"n": 0, "ok": 0, "bad": []}
        for idx, im in enumerate(imgs):
            u = im.get("url") if isinstance(im, dict) else im
            u = str(u or "").strip()
            if not u or u.startswith("data:"):
                new.append(im)
                continue
            full = u
            if full.startswith("/"):
                from abandoned_cart_mail import api_base as _api_base
                full = (await _api_base(db)) + full
            checked += 1
            _pc["n"] += 1
            if await _ok(full):
                _pc["ok"] += 1
                new.append(im)
                continue
            _pc["bad"].append(idx + 1)
            broken += 1
            if len(sample) < 8:
                sample.append(f"{cid or p.get('id')}#{idx + 1}: {u[:90]}")
            try:
                fixed = await _repair(full)
            except Exception as _re_err:
                logger.warning(f"[img-audit] onarım hata {cid}#{idx + 1}: {_re_err}")
                fixed = ""
            if fixed:
                repaired += 1
                repaired_cards.add(cid or str(p.get("id")))
                new.append({**im, "url": fixed} if isinstance(im, dict) else fixed)
                changed = True
            else:
                lost.append(f"{cid or p.get('id')} ({(p.get('name') or '')[:30]}) görsel {idx + 1}")
                new.append(im)
        if changed:
            await db.products.update_one({"id": p["id"]}, {"$set": {"images": new}})
        if cid.isdigit() and int(cid) >= 3042:
            per_card.append((int(cid), f"{cid} {(p.get('name') or '')[:28]}: {_pc['n']} görsel, {_pc['ok']} açılıyor"
                             + (f", AÇILMAYAN sıra {','.join(map(str, _pc['bad']))}" if _pc["bad"] else " ✓")))
        await asyncio.sleep(0.05)
    res = {"products": n, "checked": checked, "broken": broken, "repaired": repaired,
           "repaired_cards": sorted(repaired_cards)[:100], "lost": lost[:200], "sample": sample,
           "at": datetime.now(timezone.utc).isoformat()}
    await db.settings.update_one({"id": "image_audit_last"}, {"$set": {"id": "image_audit_last", **res}}, upsert=True)
    try:
        _h = (await asyncio.to_thread(r2.health_check)) if r2.is_enabled() else {"ok": False, "error": "yapılandırma eksik"}
        _r2s = "R2 erişimi: TAMAM" if _h.get("ok") else f"R2 erişimi: HATA — {r2.explain_error(Exception(_h.get('error') or ''))} [{str(_h.get('error') or '')[:120]}]"
    except Exception as _he:
        _r2s = f"R2 erişimi kontrol edilemedi: {type(_he).__name__}"
    res["r2"] = _r2s
    msg = (f"{_r2s} · Görsel denetimi: {n} ürün, {checked} görsel denendi · açılmayan {broken} · R2'ye taşınıp "
           f"onarılan {repaired}" + (f" (kart: {', '.join(res['repaired_cards'][:30])})" if repaired else "")
           + (f" · BULUNAMAYAN {len(lost)}: {'; '.join(lost[:15])}" if lost else "")
           + (f" · örnek: {' | '.join(sample[:3])}" if broken and not repaired else ""))
    await log_integration_event("r2", "image_audit", "bulk", str(n),
                                "success" if not lost and broken == repaired else "error", msg[:1900])
    logger.warning(f"[img-audit] {msg[:500]}")
    # Kart 3042+ ürün-bazlı rapor (kullanıcı talebi) — Entegrasyon Logları'nda parça parça
    per_card.sort()
    res["per_card_3042"] = [t for _, t in per_card]
    await db.settings.update_one({"id": "image_audit_last"}, {"$set": {"per_card_3042": res["per_card_3042"]}})
    lines = res["per_card_3042"]
    for i in range(0, len(lines), 12):
        part = lines[i:i + 12]
        _bad = any("AÇILMAYAN" in t for t in part)
        await log_integration_event("r2", "image_audit_3042", "bulk", f"{i // 12 + 1}",
                                    "error" if _bad else "success",
                                    f"Kart 3042+ görsel raporu ({i + 1}-{i + len(part)}/{len(lines)}): " + " | ".join(part))
    return res


async def _run_image_audit_job():
    try:
        from routes.deps import db
        await _audit_repair_product_images(db)
    except Exception as e:
        logger.warning(f"[scheduler] _run_image_audit_job failed: {e}")


async def _release_early_access_to_marketplaces():
    """ERKEN ERİŞİM BİTİŞİ (kullanıcı talebi): üyelere özel kategorinin bitiş tarihi geçince o
    kategorideki (ve alt kategorilerindeki) ürünlerin GÜNCEL stok + fiyatı Trendyol ve
    Hepsiburada'ya HEMEN gönderilir (periyodik senkron aralığını beklemeden). Her bitiş tarihi
    için bir kez çalışır (members_only_released_for = bitiş değeri)."""
    from routes.deps import db
    try:
        from routes.products import _parse_dt, _expand_category_ids, invalidate_members_only_cache
        now = datetime.now(timezone.utc)
        cats = await db.categories.find({"members_only": True, "members_only_end": {"$nin": [None, ""]}},
                                        {"_id": 0, "id": 1, "name": 1, "members_only_end": 1,
                                         "members_only_released_for": 1}).to_list(200)
        due = []
        for c in cats:
            end = _parse_dt(c.get("members_only_end"))
            if end and end <= now and c.get("members_only_released_for") != c.get("members_only_end"):
                due.append(c)
        if not due:
            return
        invalidate_members_only_cache()
        from marketplace_block import refresh_blocked
        await refresh_blocked(force=True)   # engel kalksın (bitmiş kategori artık kümede değil)
        from routes.integrations_common import log_integration_event
        all_cats = await db.categories.find({}, {"_id": 0, "id": 1, "parent_id": 1}).to_list(5000)
        children: dict = {}
        for x in all_cats:
            if x.get("parent_id") not in (None, ""):
                children.setdefault(str(x["parent_id"]), []).append(str(x["id"]))
        for c in due:
            ids, stack = {str(c["id"])}, [str(c["id"])]
            while stack:
                for ch in children.get(stack.pop(), []):
                    if ch not in ids:
                        ids.add(ch); stack.append(ch)
            forms = list(ids) + [int(i) for i in ids if i.isdigit()]
            prods = await db.products.find(
                {"is_active": True, "is_deleted": {"$ne": True},
                 "$or": [{"category_id": {"$in": forms}}, {"category_ids": {"$in": forms}}, {"categories": {"$in": forms}}]},
                {"_id": 0}).to_list(length=None)
            ty = hb = 0
            if prods:
                try:
                    from routes.integrations import _sync_inventory_to_trendyol, get_trendyol_config
                    cfg = await get_trendyol_config()
                    if cfg and cfg.get("is_active"):
                        await _sync_inventory_to_trendyol(prods)
                        ty = len(prods)
                except Exception as e:
                    logger.warning(f"[erken-erisim] Trendyol gönderim hatası: {e}")
                try:
                    from routes.category_mapping import _get_hb_client
                    from routes.integrations_hepsiburada import (_hb_push_stock_price, _hb_listing_items_from_product,
                                                                 _hb_markup, _hb_price_source, _hb_sku_source)
                    client, err = await _get_hb_client()
                    if client and not err:
                        markup, psrc, ssrc = await _hb_markup(), await _hb_price_source(), await _hb_sku_source()
                        items = []
                        for p in prods:
                            items.extend(_hb_listing_items_from_product(p, markup, psrc, ssrc) or [])
                        if items:
                            await _hb_push_stock_price(client, items, do_price=True, do_stock=True)
                            hb = len(prods)
                except Exception as e:
                    logger.warning(f"[erken-erisim] Hepsiburada gönderim hatası: {e}")
            await db.categories.update_one({"id": c["id"]}, {"$set": {
                "members_only_released_for": c.get("members_only_end"),
                "members_only_released_at": now.isoformat()}})
            await log_integration_event("site", "erken_erisim_bitti", "category", str(c["id"]), "success",
                                        f"'{c.get('name')}' erken erişimi bitti: {len(prods)} ürünün stok+fiyatı "
                                        f"gönderildi (Trendyol {ty}, Hepsiburada {hb}).")
            logger.warning(f"[erken-erisim] {c.get('name')}: {len(prods)} ürün pazaryerine gönderildi")
    except Exception as e:
        logger.warning(f"[scheduler] _release_early_access_to_marketplaces failed: {e}")


async def _relink_product_images_to_r2(db) -> int:
    """Ürün görsellerinde kalan sunucu-yedeği adreslerini ('/api/upload/files/X' veya
    'https://api.../api/upload/files/X') dosya R2'ye taşındıysa R2 CDN adresine çevirir.
    Aynı görsel — yalnız adresi değişir (vitrin/pazaryeri her yerden açabilsin). Sıra ve
    ölçü-tablosu işaretleri korunur."""
    import re as _re
    pat = _re.compile(r"/api/(?:upload/)?files/([^/?#]+)")
    fixed = 0
    try:
        cur = db.products.find({"$or": [{"images": {"$regex": "/api/(upload/)?files/"}},
                                        {"images.url": {"$regex": "/api/(upload/)?files/"}}]},
                               {"_id": 0, "id": 1, "images": 1})
        async for p in cur:
            imgs = p.get("images") or []
            new, changed = [], False
            for im in imgs:
                u = im.get("url") if isinstance(im, dict) else im
                m = pat.search(str(u or "")) if u else None
                if m:
                    f = await db.files.find_one({"storage_path": m.group(1)}, {"_id": 0, "r2_url": 1})
                    r2u = (f or {}).get("r2_url") or ""
                    if r2u.startswith("http"):
                        im = {**im, "url": r2u} if isinstance(im, dict) else r2u
                        changed = True
                new.append(im)
            if changed:
                await db.products.update_one({"id": p["id"]}, {"$set": {"images": new}})
                fixed += 1
        if fixed:
            logger.warning(f"[r2-mig] ürün görsel adresi R2'ye bağlandı: {fixed} ürün")
    except Exception as e:
        logger.warning(f"[r2-mig] ürün görsel bağlama hata: {e}")
    return fixed


async def _run_private_archive():
    """GECE (kullanıcı, 2026-10-03: "faturalar ve müşteri dekontları 1 ay sonra başka yere yedeklensin,
    veritabanında kalmasın — R2 yap"): 30 günden eski DEKONTLAR (orders.payment_receipt) ve
    veritabanındaki manuel fatura dosyaları (db.files, PDF) GİZLİ R2 arşiv deposuna (R2_ARCHIVE_BUCKET,
    herkese kapalı) taşınır. Her dosya arşive yazılır → GERİ OKUNUP birebir aynı olduğu doğrulanır →
    ancak o zaman veritabanındaki kopya silinir (archive_key kalır; panel arşivden okur). Doğrulanamayan
    dosya veritabanında KALIR. Depo tanımlı değilse hiçbir şey yapmaz. Silme YOK, yalnız taşıma.
    Kapsam (denetim 2026-10): önceki dekont sürümleri (payment_receipt_versions) ve şifreli dekontlar
    (data_enc — routes.orders.get_receipt_bytes ile çözülür) de dahildir."""
    from routes.deps import db  # lazy import
    import base64 as _b64
    import hashlib as _hl
    try:
        from services import r2_storage as r2
        if not r2.archive_enabled():
            return
        if not (await asyncio.to_thread(r2.archive_health)).get("ok"):
            logger.warning("[arsiv] R2 arşiv deposuna erişilemiyor — atlandı")
            return
        from routes.integrations_common import log_integration_event
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        n_rc = n_fi = n_err = 0
        freed = 0

        async def _put_verified(key, data, ct):
            await asyncio.to_thread(r2.archive_put, key, data, ct)
            got, _ = await asyncio.to_thread(r2.archive_get, key)
            return got is not None and _hl.sha256(got).digest() == _hl.sha256(data).digest()

        # Dekont ARŞİVE ŞİFRELİ gider (at-rest şifreleme 30 gün sonra bitmesin): şifreli dekontun
        # Fernet token baytları (data_enc) OLDUĞU GİBİ yazılır; eski düz (data_b64) dekont önce
        # security.crypto ile şifrelenir. Kayda archive_enc_format="fernet-bytes-v1" yazılır →
        # get_payment_receipt arşivden okurken çözer. Token çözülemiyorsa (anahtar?) dekont ATLANIR
        # (veritabanından silinmez). Dönüş: (arşive yazılacak token baytları, düz baytlar).
        def _receipt_archive_payload(r: dict):
            from security import crypto as _c
            from routes.orders import _encrypt_receipt_bytes
            enc = r.get("data_enc")
            if enc:
                token = str(enc).encode("ascii")
                plain = _c._fernet_dec.decrypt(token)   # çözülemiyorsa istisna → dekont atlanır
                return token, plain
            if r.get("data_b64"):
                plain = _b64.b64decode(r["data_b64"])
                token = _encrypt_receipt_bytes(plain).encode("ascii")
                if _c._fernet_dec.decrypt(token) != plain:
                    raise RuntimeError("dekont şifreleme doğrulaması başarısız")
                return token, plain
            raise RuntimeError("dekont baytları okunamadı")

        async def _put_verified_enc(key, token: bytes, plain: bytes):
            """Şifreli dekontu arşive yazar; GERİ OKUNAN = yazılan token VE çözümü = düz dekont olmalı."""
            from security import crypto as _c
            await asyncio.to_thread(r2.archive_put, key, token, "application/octet-stream")
            got, _ = await asyncio.to_thread(r2.archive_get, key)
            if got is None or _hl.sha256(got).digest() != _hl.sha256(token).digest():
                return False
            try:
                return _c._fernet_dec.decrypt(bytes(got)) == plain
            except Exception:
                return False

        def _stored_fields(r: dict) -> dict:
            """Doğrula-sonra-sil koşulu: arşivlenen İÇERİK hâlâ aynıysa sil (arada yenisi yüklendiyse dokunma)."""
            return {k: r[k] for k in ("data_b64", "data_enc") if r.get(k) not in (None, "")}

        def _ext_of(r: dict) -> str:
            fn = str(r.get("filename") or "")
            return (fn.rsplit(".", 1)[-1].lower() if "." in fn else "bin")[:5]

        # 1) Dekontlar (siparişteki güncel dekont)
        q = {"$or": [{"payment_receipt.data_b64": {"$exists": True, "$nin": [None, ""]}},
                     {"payment_receipt.data_enc": {"$exists": True, "$nin": [None, ""]}}],
             "payment_receipt.uploaded_at": {"$lt": cutoff}}
        async for o in db.orders.find(q, {"_id": 0, "id": 1, "order_number": 1, "payment_receipt": 1}).limit(2000):
            if not await _still_leader():  # fencing: liderlik kaybedildiyse silme yapma
                break
            r = o.get("payment_receipt") or {}
            try:
                token, plain = _receipt_archive_payload(r)
                key = f"dekontlar/{str(r.get('uploaded_at') or now)[:7]}/{o.get('order_number') or o['id']}-{uuid.uuid4().hex[:8]}.{_ext_of(r)}.enc"
                if not await _put_verified_enc(key, token, plain):
                    raise RuntimeError("doğrulama başarısız")
                _cond = {"id": o["id"], **{f"payment_receipt.{k}": v for k, v in _stored_fields(r).items()}}
                await db.orders.update_one(_cond, {
                    "$set": {"payment_receipt.archive_key": key, "payment_receipt.archived_at": now,
                             "payment_receipt.archive_enc_format": "fernet-bytes-v1"},
                    "$unset": {"payment_receipt.data_b64": "", "payment_receipt.data_enc": ""}})
                n_rc += 1
                freed += sum(len(str(v)) for v in _stored_fields(r).values())
            except Exception as e:
                n_err += 1
                logger.warning(f"[arsiv] dekont {o.get('order_number')}: {e}")
            await asyncio.sleep(0.1)
        # 1b) ÖNCEKİ dekont sürümleri (payment_receipt_versions — yeni dekont yüklenince eskisi buraya
        # taşınır). 30 günden eski sürümler de AYNI doğrula-sonra-sil kalıbıyla gizli arşive gider;
        # kayıt (metadata + archive_key) kalır, baytlar silinir.
        n_rv = 0
        vq = {"$and": [
            {"$or": [{"receipt.data_b64": {"$exists": True, "$nin": [None, ""]}},
                     {"receipt.data_enc": {"$exists": True, "$nin": [None, ""]}}]},
            # dekontun kendi yüklenme tarihi (ya da sürüm kaydı) 30 günden eski
            {"$or": [{"receipt.uploaded_at": {"$lt": cutoff}}, {"created_at": {"$lt": cutoff}}]},
        ]}
        async for v in db.payment_receipt_versions.find(
                vq, {"_id": 0, "id": 1, "order_id": 1, "order_number": 1, "receipt": 1, "created_at": 1}).limit(2000):
            if not await _still_leader():
                break
            r = v.get("receipt") or {}
            try:
                token, plain = _receipt_archive_payload(r)
                key = (f"dekontlar/surumler/{str(r.get('uploaded_at') or v.get('created_at') or now)[:7]}/"
                       f"{v.get('order_number') or v.get('order_id') or 'siparis'}-{uuid.uuid4().hex[:8]}.{_ext_of(r)}.enc")
                if not await _put_verified_enc(key, token, plain):
                    raise RuntimeError("doğrulama başarısız")
                _cond = {"id": v["id"], **{f"receipt.{k}": val for k, val in _stored_fields(r).items()}}
                await db.payment_receipt_versions.update_one(_cond, {
                    "$set": {"receipt.archive_key": key, "receipt.archived_at": now,
                             "receipt.archive_enc_format": "fernet-bytes-v1"},
                    "$unset": {"receipt.data_b64": "", "receipt.data_enc": ""}})
                n_rv += 1
                freed += sum(len(str(val)) for val in _stored_fields(r).values())
            except Exception as e:
                n_err += 1
                logger.warning(f"[arsiv] dekont sürümü {v.get('order_number')}: {e}")
            await asyncio.sleep(0.1)
        # 2) Veritabanındaki manuel fatura dosyaları (PDF)
        fq = {"data_b64": {"$exists": True, "$nin": [None, ""]}, "content_type": "application/pdf",
              "created_at": {"$lt": cutoff}, "is_deleted": {"$ne": True}}
        async for f in db.files.find(fq, {"_id": 1, "storage_path": 1, "data_b64": 1, "created_at": 1}).limit(2000):
            if not await _still_leader():
                break
            try:
                data = _b64.b64decode(f["data_b64"])
                key = f"faturalar/{str(f.get('created_at') or now)[:7]}/{f.get('storage_path') or f['_id']}"
                if not await _put_verified(key, data, "application/pdf"):
                    raise RuntimeError("doğrulama başarısız")
                await db.files.update_one({"_id": f["_id"]}, {"$set": {"archive_key": key, "archived_at": now},
                                                              "$unset": {"data_b64": ""}})
                n_fi += 1
                freed += len(f["data_b64"])
            except Exception as e:
                n_err += 1
                logger.warning(f"[arsiv] dosya {f.get('storage_path')}: {e}")
            await asyncio.sleep(0.1)
        if n_rc or n_rv or n_fi or n_err:
            await log_integration_event("r2", "private_archive", "bulk", str(n_rc + n_rv + n_fi),
                                        "success" if not n_err else "error",
                                        f"Gizli arşiv: {n_rc} dekont, {n_rv} eski dekont sürümü, {n_fi} fatura dosyası taşındı, {n_err} hata "
                                        f"(hatalılar veritabanında kaldı), ~{freed / 1024 / 1024:.1f} MB boşaldı")
    except Exception as e:
        logger.warning(f"[scheduler] _run_private_archive failed: {e}")


async def _ensure_arsiv_erisim_testi():
    """Açılışta: gizli arşiv deposu (R2_ARCHIVE_BUCKET) tanımlıysa küçük bir test dosyası yazılır,
    geri okunur, silinir; sonuç Entegrasyon Logları'na "private_archive_test" olarak düşer.
    Depo tanımlı değilse hiçbir şey yapmaz (bayrak da yazılmaz → tanımlanınca sonraki açılışta dener)."""
    from routes.deps import db  # lazy import
    _flag = "arsiv_erisim_testi_v1"
    try:
        from services import r2_storage as r2
        if not r2.archive_enabled():
            return
        if await db.settings.find_one({"id": _flag, "ok": True}):
            return
        from routes.integrations_common import log_integration_event
        h = await asyncio.to_thread(r2.archive_health)
        ok, msg = False, ""
        if h.get("ok"):
            key = f"_test/erisim-{uuid.uuid4().hex[:8]}.txt"
            data = b"atilla arsiv erisim testi"
            await asyncio.to_thread(r2.archive_put, key, data, "text/plain")
            got, _ = await asyncio.to_thread(r2.archive_get, key)
            ok = got == data
            try:
                await asyncio.to_thread(lambda: r2._get_client().delete_object(Bucket=r2.archive_bucket(), Key=key))
            except Exception:
                pass
            msg = f"Arşiv deposu '{r2.archive_bucket()}' erişim testi: " + ("yazma/okuma BAŞARILI" if ok else "okuma doğrulanamadı")
        else:
            msg = f"Arşiv deposuna erişilemedi: {h.get('error')}"
        await db.settings.update_one({"id": _flag}, {"$set": {"id": _flag, "ok": ok, "msg": msg,
                                     "at": datetime.now(timezone.utc).isoformat()}}, upsert=True)
        await log_integration_event("r2", "private_archive_test", "system", "arsiv",
                                    "success" if ok else "error", msg)
        logger.warning(f"[arsiv] {msg}")
    except Exception as e:
        logger.warning(f"[scheduler] _ensure_arsiv_erisim_testi failed: {e}")


async def sweep_paid_order_side_effects():
    """KAÇIRILAN ÖDEME-SONRASI YAN ETKİLER (denetim 2026-10, 15 dk'da bir).

    Kart siparişi 'paid' geçişini (payment._mark_order_from_payment, ATOMİK) kazandıktan sonra
    süreç yan etkilerden önce düşerse (deploy/restart/timeout) müşteri onay maili, admin push'u
    ve kupon kullanım kaydı HİÇ yazılmıyordu. Bu iş son 7 günde ödenmiş KART siparişlerinden
    `order_confirmed_notified` ya da `coupon_redemptions_recorded` bayrağı eksik olanlar için
    AYNI idempotent fonksiyonu (_notify_paid_order_confirmed) yeniden çağırır:
      - mail/push yalnız `order_confirmed_notified` ATOMİK claim'ini kazanan çağrıda gider
        → değişmez 3 (tek mail, yalnız ödeme onayından sonra) korunur;
      - kupon kaydı (coupon_id, order_id) upsert'iyle idempotent → değişmez 5 korunur.
    Sipariş statüsüne / ödeme durumuna / stoğa DOKUNMAZ (değişmez 1, 2, 6).
    Kapsam dışı: iptal/iade edilmiş siparişler, admin'in SESSİZ kurtardığı siparişler
    (recovered_at), tamamı hediye çekiyle ödenenler ve son 10 dakikada ödenenler (o an
    callback'in kendisi yan etkileri işliyor olabilir)."""
    from routes.deps import db  # lazy import
    try:
        from routes.payment import _notify_paid_order_confirmed
        now = datetime.now(timezone.utc)
        q = {
            "payment_status": "paid",
            "payment_method": {"$in": ["credit_card", "card", "kredi_karti", "kart", "iyzico", "creditcard"]},
            "iyzico_payment_id": {"$nin": [None, ""]},
            "paid_at": {"$gte": (now - timedelta(days=7)).isoformat(),
                        "$lte": (now - timedelta(minutes=10)).isoformat()},
            "status": {"$nin": ["cancelled", "cancel_refunded", "refunded", "partial_refunded",
                                "returned", "return_approved", "return_requested",
                                "return_in_transit", "return_rejected"]},
            "recovered_at": {"$exists": False},
            "paid_with_gift_card_only": {"$ne": True},
            "$or": [{"order_confirmed_notified": {"$ne": True}},
                    {"coupon_redemptions_recorded": {"$ne": True}}],
        }
        rows = await db.orders.find(q, {"_id": 0, "id": 1, "order_number": 1,
                                        "order_confirmed_notified": 1}).limit(200).to_list(200)
        notified = 0
        for o in rows:
            if not await _still_leader():  # fencing: liderlik kaybedildiyse yazma yapma
                break
            try:
                _was = bool(o.get("order_confirmed_notified"))
                await _notify_paid_order_confirmed(o["id"])
                if not _was:
                    notified += 1
                    logger.warning(f"[side-effect-sweep] kaçırılan onay bildirimi işlendi order={o.get('order_number')}")
            except Exception as _e:
                logger.warning(f"[side-effect-sweep] order={o.get('order_number')} hata: {_e}")
        if rows:
            logger.info(f"[side-effect-sweep] {len(rows)} sipariş tarandı, {notified} kaçırılan bildirim")
    except Exception as e:
        logger.warning(f"[scheduler] sweep_paid_order_side_effects failed: {e}")


async def _ensure_guvenlik_uyari_adresleri():
    """TEK SEFERLİK (kullanıcı, 2026-10-04: "gönderen adres info@atilla.com.tr, alıcı
    belgin@seamoas.com ve kdrgry@gmail.com"): İşletme Kuralları'ndaki güvenlik uyarısı alıcıları ve
    gönderen adresi YALNIZ BOŞSA bu değerlerle doldurulur (panelden girilmiş değere dokunulmaz).
    Değerler kodda sabit DEĞİL — veri olarak ayara yazılır; sonra panelden değiştirilebilir."""
    from routes.deps import db  # lazy import
    _flag = "guvenlik_uyari_adresleri_v1"
    try:
        if await db.settings.find_one({"id": _flag, "done": True}):
            return
        await db.settings.update_one({"id": _flag}, {"$set": {"id": _flag, "done": True,
                                     "at": datetime.now(timezone.utc).isoformat()}}, upsert=True)
        import business_rules as BR
        cur = (await db.settings.find_one({"id": "business_rules"}, {"_id": 0}) or {}).get("values") or {}
        patch = {}
        # Beyaz etiket: alıcılar kodda DEĞİL — kurulumun kendi ortam değişkenlerinden gelir.
        _env_to = (os.environ.get("ALERT_TO_EMAIL") or os.environ.get("OWNER_SUPER_ADMIN_EMAILS") or "").strip()
        _env_from = (os.environ.get("ALERT_FROM_EMAIL") or "").strip()
        if _env_to and not str(cur.get("security.alert_emails") or "").strip():
            patch["security.alert_emails"] = _env_to
        if _env_from and not str(cur.get("security.alert_from") or "").strip():
            patch["security.alert_from"] = _env_from
        if patch:
            await BR.save_rules(db, patch)
        logger.warning(f"[guvenlik] uyarı adresleri ayarlandı: {sorted(patch)}")
    except Exception as e:
        logger.warning(f"[scheduler] _ensure_guvenlik_uyari_adresleri failed: {e}")


async def _ensure_guvenlik_sms_numarasi():
    """TEK SEFERLİK (kullanıcı, 2026-10-05: "hata, sızma tespit ettiğinde SMS entegrasyonu ile 05396738540'a
    mesaj gönder"): numara İşletme Kuralları `security.alert_sms_phones` alanına VERİ olarak yazılır (kodda
    gönderim mantığı numara bilmez — beyaz etiket). Alan boşsa yazılır; doluysa ve numara yoksa (en çok 3)
    SONUNA eklenir — panelden girilmiş numaralar korunur. Sonra panelden değiştirilebilir."""
    from routes.deps import db  # lazy import
    _flag = "guvenlik_sms_numarasi_v1"
    try:
        if await db.settings.find_one({"id": _flag, "done": True}):
            return
        await db.settings.update_one({"id": _flag}, {"$set": {"id": _flag, "done": True,
                                     "at": datetime.now(timezone.utc).isoformat()}}, upsert=True)
        import business_rules as BR
        from notification_service import normalize_phone_tr
        want = (os.environ.get("ALERT_SMS_PHONE") or "").strip()
        cur = str(((await db.settings.find_one({"id": "business_rules"}, {"_id": 0}) or {}).get("values") or {})
                  .get("security.alert_sms_phones") or "").strip()
        parts = [x.strip() for x in cur.replace(";", ",").split(",") if x.strip()]
        patch = {}
        if want and normalize_phone_tr(want) not in {normalize_phone_tr(x) for x in parts}:
            if len(parts) >= 3:
                logger.warning("[guvenlik] SMS numarası eklenmedi: alan dolu (3 numara)")
            else:
                patch["security.alert_sms_phones"] = ", ".join(parts + [want])
        # Kullanıcı, 2026-10-05: "kdrgry1342@gmail.com admin olarak panele girmeli" → tekrar SMS'ini bu yönetici
        # panele girince durdurur (alan boşsa yazılır; panelden girilmiş değere dokunulmaz).
        _vals = (await db.settings.find_one({"id": "business_rules"}, {"_id": 0}) or {}).get("values") or {}
        _stop = (os.environ.get("OWNER_SUPER_ADMIN_EMAILS") or "").split(",")[0].strip()
        if _stop and not str(_vals.get("security.alert_page_stop_emails") or "").strip():
            patch["security.alert_page_stop_emails"] = _stop
        if patch:
            await BR.save_rules(db, patch)
        logger.warning("[guvenlik] güvenlik SMS ayarları yazıldı: %s", sorted(patch))
    except Exception as e:
        logger.warning(f"[scheduler] _ensure_guvenlik_sms_numarasi failed: {type(e).__name__}")


async def _ensure_w12254_test_siparisi_sil():
    """TEK SEFERLİK (kullanıcı, 2026-10-04: "W12254 bunu sil"): güvenlik testi sırasında boş istekle
    YANLIŞLIKLA oluşan W12254 siparişi silinir — panelin normal silme akışıyla aynı: önce
    orders_deleted arşivine taşınır (Silinen Siparişler'den görülebilir), sonra orders'tan kaldırılır.
    GÜVENLİK: yalnız ürünsüz, ödemesi alınmamış ve tutarı ≤ 150 TL ise silinir; aksi halde DOKUNMAZ.
    Stok: ürün olmadığından stok hareketi yoktur."""
    from routes.deps import db  # lazy import
    _flag = "w12254_test_siparisi_sil_v1"
    try:
        if await db.settings.find_one({"id": _flag, "done": True}):
            return
        await db.settings.update_one({"id": _flag}, {"$set": {"id": _flag, "done": True,
                                     "at": datetime.now(timezone.utc).isoformat()}}, upsert=True)
        from routes.integrations_common import log_integration_event
        o = await db.orders.find_one({"order_number": "W12254"}, {"_id": 0})
        if not o:
            await log_integration_event("system", "test_order_delete", "order", "W12254", "success",
                                        "W12254 bulunamadı (zaten silinmiş)")
            return
        _items = (o.get("items") or []) + (o.get("lines") or [])
        _paid = str(o.get("payment_status") or "").lower() in ("paid", "refunded", "partially_refunded")
        if _items or _paid or float(o.get("total") or 0) > 150:
            await log_integration_event("system", "test_order_delete", "order", "W12254", "error",
                                        "W12254 beklenen boş test siparişi değil — SİLİNMEDİ (ürün/ödeme/tutar)")
            return
        now = datetime.now(timezone.utc).isoformat()
        o["deleted_at"] = now
        o["deleted_by"] = "sistem (kullanıcı isteği: boş test siparişi)"
        o["_stock_returned_on_delete"] = False
        await db.orders_deleted.replace_one({"id": o["id"]}, o, upsert=True)
        await db.orders.delete_one({"id": o["id"], "order_number": "W12254"})
        await db.order_events.insert_one({
            "id": str(uuid.uuid4()), "order_id": o["id"], "order_number": "W12254", "type": "delete",
            "message": "Boş test siparişi silindi (arşive taşındı)", "actor": "sistem", "created_at": now})
        await log_integration_event("system", "test_order_delete", "order", "W12254", "success",
                                    "W12254 boş test siparişi Silinen Siparişler arşivine taşındı")
        logger.warning("[w12254] boş test siparişi silindi (arşivde)")
    except Exception as e:
        logger.warning(f"[scheduler] _ensure_w12254_test_siparisi_sil failed: {e}")


async def _ensure_vera_imalat_stok_kilidi():
    """TEK SEFERLİK (kullanıcı, 2026-09-30: "sadece bu seferlik stok tetiklemeyecek"): Vera Ceket
    imalat kaydı eskiden depoya alınmış, stoğu elle girilmiş; ürün kartına sonradan bağlandı.
    Aşama tekrar "Teslim Alındı"ya ilerletilirse stok İKİNCİ kez eklenmesin diye YALNIZ bu
    kayıt(lar) stock_incremented=True işaretlenir. Stok/fiyat değişmez."""
    from routes.deps import db  # lazy import
    _flag = "vera_imalat_stok_kilidi_v1"
    try:
        if ((await db.settings.find_one({"id": _flag}, {"_id": 0, "done": 1})) or {}).get("done"):
            return
        q = {"product_name": {"$regex": r"^\s*Vera\b.*Ceket", "$options": "i"}, "stock_incremented": {"$ne": True}}
        recs = await db.manufacturing.find(q, {"_id": 0, "id": 1, "code": 1, "product_name": 1, "current_stage": 1}).to_list(20)
        now = datetime.now(timezone.utc).isoformat()
        if recs:
            await db.manufacturing.update_many({"id": {"$in": [r["id"] for r in recs]}},
                                               {"$set": {"stock_incremented": True, "stock_lock_note": "Stok daha önce elle girildi (kullanıcı, 2026-09-30)", "updated_at": now}})
        await db.settings.update_one({"id": _flag}, {"$set": {"id": _flag, "done": True, "at": now,
                                     "kilitlenen": [{"kod": r.get("code"), "ad": r.get("product_name"), "asama": r.get("current_stage")} for r in recs]}}, upsert=True)
    except Exception as e:
        logger.warning(f"[scheduler] _ensure_vera_imalat_stok_kilidi failed: {e}")


async def _trendyol_delivery_tick():
    """Trendyol termin takvimi: durum değiştiyse (Pazar 12:00 / Cuma 15:00) ya da son uygulama
    başarısızsa tüm onaylı ürünlere uygular; ayrıca önceki batch sonuçlarını kontrol eder."""
    from routes.deps import db  # lazy import
    try:
        import trendyol_delivery as _TD
        try:
            await _TD.check_batches(db)
        except Exception as _ce:
            logger.info(f"[trendyol-termin] batch kontrolü atlandı: {_ce}")
        # Tek seferlik (2026-10-05): özelliği boş olduğu için atlanmış ürünler aynı gün kargoyu takvim
        # değişimini (Cuma 15:00) beklemeden alsın — bir kez tüm katalog yeniden uygulanır.
        _flag = "trendyol_delivery_reapply_v2"
        if not await db.settings.find_one({"id": _flag, "done": True}):
            await db.settings.update_one({"id": _flag}, {"$set": {"id": _flag, "done": True,
                                         "at": datetime.now(timezone.utc).isoformat()}}, upsert=True)
            await _TD.apply(db, force=True)
            return
        await _TD.apply(db)
    except Exception as e:
        logger.warning(f"[scheduler] _trendyol_delivery_tick failed: {e}")


async def _ensure_zayif_uye_sifre_temizle():
    """TEK SEFERLİK GÜVENLİK (denetim 2026-09-29): panelden şifresiz eklenen üyelere eskiden BİLİNEN
    sabit şifre ("Atilla123!") ya da BOŞ şifre atanıyordu. Panelden açılmış (created_by_admin) ve
    şifresi bu ikisinden biri olan hesapların şifresi rastgele/kullanılamaz yapılır; müşteri
    "Şifremi unuttum" ile yeni şifre alır. Oturumlar token_version ile düşürülür. Sayım dışında
    kişisel veri kaydedilmez."""
    import secrets as _sec
    from routes.deps import db, verify_password, hash_password  # lazy import
    _flag = "zayif_uye_sifre_temizle_v1"
    try:
        if ((await db.settings.find_one({"id": _flag}, {"_id": 0, "done": 1})) or {}).get("done"):
            return
        checked = fixed = 0
        async for u in db.users.find({"created_by_admin": {"$exists": True}, "is_admin": {"$ne": True}},
                                     {"_id": 0, "id": 1, "password": 1}):
            checked += 1
            h = u.get("password") or ""
            weak = False
            for cand in ("Atilla123!", ""):
                if await asyncio.to_thread(verify_password, cand, h):
                    weak = True
                    break
            if weak:
                _new = await asyncio.to_thread(hash_password, _sec.token_urlsafe(24))
                await db.users.update_one({"id": u["id"]}, {"$set": {
                    "password": _new, "password_rotated_security_at": datetime.now(timezone.utc).isoformat()},
                    "$inc": {"token_version": 1}})
                fixed += 1
        await db.settings.update_one({"id": _flag}, {"$set": {"id": _flag, "done": True, "checked": checked,
                                     "fixed": fixed, "at": datetime.now(timezone.utc).isoformat()}}, upsert=True)
        logger.warning(f"[guvenlik] zayıf üye şifresi temizliği: {fixed}/{checked} hesap")
    except Exception as e:
        logger.warning(f"[scheduler] _ensure_zayif_uye_sifre_temizle failed: {e}")


async def _ensure_body_indirim10_v3():
    """TEK SEFERLİK (Kadir isteği 2026-09-29, 2. kez): Body (5378) — Elen Yarasa Kol Basic
    (6591, 3043) HARİÇ tüm ürünlerde indirimli fiyat = etiket × 0,90 (tam TL, aşağı). Kullanıcı
    onayıyla zaten ~%15 indirimli olanlar da %10'a çekilir. Yalnız sale_price (+ feed için
    varyantta dolu sale_price) değişir; etiket / Üye Tipi fiyatı / stok değişmez."""
    import math
    from routes.deps import db  # lazy import
    from routes.integrations_common import _to_float_tr
    _flag = "body_indirim10_v3"
    _haric = {"6591", "3043"}
    try:
        if ((await db.settings.find_one({"id": _flag}, {"_id": 0, "done": 1})) or {}).get("done"):
            return
        _now = datetime.now(timezone.utc).isoformat()
        q = {"$or": [{"category_ids": "5378"}, {"category_ids": 5378},
                     {"category_id": "5378"}, {"category_id": 5378}],
             "is_deleted": {"$ne": True}}
        items = []
        async for p in db.products.find(q, {"_id": 0, "id": 1, "name": 1, "price": 1, "sale_price": 1, "variants": 1}):
            if str(p.get("id")) in _haric:
                continue
            price = _to_float_tr(p.get("price"))
            if price <= 0:
                continue
            sale = float(math.floor(price * 0.9))
            upd = {"sale_price": sale, "updated_at": _now}
            vs, touched = [], 0
            for v in (p.get("variants") or []):
                if isinstance(v, dict) and (v.get("price") not in (None, "", 0) or v.get("sale_price") not in (None, "", 0)):
                    v = dict(v)
                    adj = _to_float_tr(v.get("price_adjustment") or v.get("price_diff"))
                    v["sale_price"] = round(sale + adj, 2)
                    touched += 1
                vs.append(v)
            if touched:
                upd["variants"] = vs
            await db.products.update_one({"id": p["id"]}, {"$set": upd})
            items.append({"id": p["id"], "ad": p.get("name"), "fiyat": price,
                          "eski_indirimli": p.get("sale_price"), "yeni_indirimli": sale})
        await db.settings.update_one({"id": _flag}, {"$set": {"id": _flag, "done": True, "at": _now,
                                     "changed": len(items), "items": items}}, upsert=True)
        logger.info(f"[scheduler] Body %10 (v3, Elen hariç): {len(items)} ürün")
    except Exception as e:
        logger.warning(f"[scheduler] _ensure_body_indirim10_v3 failed: {e}")


async def _ensure_v18_uye_tipi_ve_elen_geri():
    """TEK SEFERLİK (Kadir isteği 2026-09-29):
    1) KARLILIK v18 etiket fiyatları Üye Tipi Fiyatı 1'e de yazılır (price_update_v18.apply_member_price).
    2) Body %10'dan vazgeçilen Elen Yarasa Kol Basic (Siyah 6591, Beyaz 3043): indirimli fiyat
       kaldırılır (öncesinde indirimli fiyatı yoktu). Diğer 18 Body ürünü aynen kalır."""
    from routes.deps import db  # lazy import
    _flag = "elen_body10_geri_v1"
    try:
        import price_update_v18
        r = await price_update_v18.apply_member_price(db)
        logger.info(f"[scheduler] v18 üye tipi fiyatı: {r}")
    except Exception as e:
        logger.warning(f"[scheduler] v18 üye tipi fiyatı failed: {e}")
    try:
        if ((await db.settings.find_one({"id": _flag}, {"_id": 0, "done": 1})) or {}).get("done"):
            return
        n = 0
        for pid in ("6591", "3043"):
            res = await db.products.update_one({"id": pid, "stock_code": "FCFW994865"},
                                               {"$set": {"sale_price": None,
                                                         "updated_at": datetime.now(timezone.utc).isoformat()}})
            n += res.modified_count
        await db.settings.update_one({"id": _flag}, {"$set": {"id": _flag, "done": True, "reverted": n,
                                     "at": datetime.now(timezone.utc).isoformat()}}, upsert=True)
        logger.info(f"[scheduler] Elen Body %10 geri alındı: {n}")
    except Exception as e:
        logger.warning(f"[scheduler] Elen geri alma failed: {e}")


async def _ensure_fiyat_guncelleme_v18():
    """TEK SEFERLİK (2026-09-29, Kadir isteği): KARLILIK_ANALIZI_v18 'Site Karlılık' yeni
    fiyatları siteye yazılır — yalnız fiyat; pazaryeri tabanı sabitlenir. Ayrıntı modülde."""
    from routes.deps import db  # lazy import
    try:
        import price_update_v18
        res = await price_update_v18.apply(db)
        logger.info(f"[scheduler] Site fiyat güncellemesi v18: {res}")
    except Exception as e:
        logger.warning(f"[scheduler] _ensure_fiyat_guncelleme_v18 failed: {e}")


async def _ensure_hb_toplam_onarim():
    """TEK SEFERLİK (2026-09-28): HB çok-kalemli siparişlerde kayıtlı total = İLK KALEM
    fiyatı kalmıştı (grup totalPrice'ı ilk kalemden kopyalanıyordu). Yalnız KESİN kalıp
    düzeltilir: platform=hepsiburada, ≥2 kalem, total == ilk kalem tutarı ve ≠ kalem toplamı.
    total/subtotal = Σ price×qty yapılır; eski değer kayda yazılır (geri alınabilir).
    Kesilmiş FATURAYA dokunulmaz (iptal/yeniden kesim muhasebe kararı)."""
    from routes.deps import db  # lazy import
    _flag = "hb_toplam_onarim_v1"
    try:
        if ((await db.settings.find_one({"id": _flag}, {"_id": 0, "done": 1})) or {}).get("done"):
            return
        _now = datetime.now(timezone.utc).isoformat()
        fixed = []
        async for o in db.orders.find({"platform": "hepsiburada"},
                                      {"_id": 1, "order_number": 1, "total": 1, "subtotal": 1, "items": 1}):
            its = o.get("items") or []
            if len(its) < 2:
                continue
            try:
                calc = round(sum(float(i.get("price") or 0) * int(i.get("quantity") or 1) for i in its), 2)
                first = round(float(its[0].get("price") or 0) * int(its[0].get("quantity") or 1), 2)
                tot = round(float(o.get("total") or 0), 2)
            except Exception:
                continue
            if abs(tot - first) <= 0.5 and abs(calc - tot) > 0.5:
                await db.orders.update_one({"_id": o["_id"]}, {"$set": {
                    "total": calc, "subtotal": calc}})
                fixed.append({"no": o.get("order_number"), "eski": tot, "yeni": calc})
        await db.settings.update_one({"id": _flag}, {"$set": {"id": _flag, "done": True, "at": _now,
                                     "fixed": fixed}}, upsert=True)
        logger.info(f"[scheduler] HB toplam onarımı: {len(fixed)} sipariş")
    except Exception as e:
        logger.warning(f"[scheduler] _ensure_hb_toplam_onarim failed: {e}")


async def _ensure_body_3al2_kampanya():
    """TEK SEFERLİK (Kadir isteği 2026-09-26): Body kategorisine (5378) otomatik "3 AL 2 ÖDE"
    kampanyası. Sepetteki kapsamdaki her 3 üründen EN UCUZU bedava (nth_discount motoru).
    Body ürünlerinin çoğu indirimli fiyatlı olduğundan skip_discounted=False (hepsi dahil).
    Kampanya zaten varsa (kod) veya bayrak yazılmışsa DOKUNMAZ — sonrası panelden yönetilir."""
    from routes.deps import db  # lazy import
    _flag = "kampanya_body_3al2_v1"
    try:
        _code = "AUTO-BODY-3AL2"
        if ((await db.settings.find_one({"id": _flag}, {"_id": 0, "done": 1})) or {}).get("done"):
            raise StopAsyncIteration  # v1 zaten yapıldı → yalnız v2'ye geç
        if not await db.coupons.find_one({"code": _code}, {"_id": 1}):
            _now = datetime.now(timezone.utc).isoformat()
            await db.coupons.insert_one({
                "id": str(uuid.uuid4()), "code": _code, "title": "3 Al 2 Öde — Body",
                "type": "nth_discount", "value": 0.0, "bundle_price": None,
                "min_cart_total": 0.0, "max_discount": None,
                "categories": ["5378"], "products": [], "excluded_products": [],
                "usage_limit": None, "usage_limit_per_user": None,
                "start_at": _now, "end_at": None,
                "is_active": True, "first_order_only": False, "free_shipping": False,
                "auto_apply": True, "skip_discounted": False,
                "min_quantity": 3, "buy_quantity": 3, "free_quantity": 1, "get_discount": 100.0,
                "badge_text": "3 AL 2 ÖDE",
                "priority": 0, "combinable": True, "stack_group": None,
                "combinable_with": [], "not_combinable_with": [], "payment_methods": [],
                "created_at": _now, "created_by": "sistem (Kadir isteği)",
            })
            try:
                from routes.coupons import invalidate_codes_cache
                invalidate_codes_cache()
            except Exception:
                pass
            logger.info("[scheduler] Body 3 al 2 öde kampanyası oluşturuldu")
        await db.settings.update_one({"id": _flag}, {"$set": {"id": _flag, "done": True,
                                     "at": datetime.now(timezone.utc).isoformat()}}, upsert=True)
    except StopAsyncIteration:
        pass
    except Exception as e:
        logger.warning(f"[scheduler] _ensure_body_3al2_kampanya failed: {e}")
    # v2 (denetim bulgusu): eşit öncelikte motor BÜYÜK indirimi önce uygular; Lansman %20
    # önce çalışınca kalan tutar ölçeklenip "bedava" ürün ~60 TL ücretli kalıyordu. 3 al 2 öde
    # vaadi (en ucuz ürün TAM bedava) için bu kampanya önce hesaplanır: priority 10.
    # Yalnız hâlâ 0 ise (admin sonradan değiştirdiyse dokunulmaz).
    _flag2 = "kampanya_body_3al2_v2_oncelik"
    try:
        if not ((await db.settings.find_one({"id": _flag2}, {"_id": 0, "done": 1})) or {}).get("done"):
            await db.coupons.update_one({"code": "AUTO-BODY-3AL2", "priority": {"$in": [0, None]}},
                                        {"$set": {"priority": 10}})
            await db.settings.update_one({"id": _flag2}, {"$set": {"id": _flag2, "done": True}}, upsert=True)
    except Exception as e:
        logger.warning(f"[scheduler] body 3al2 öncelik güncellenemedi: {e}")


async def _ensure_ty_iptal_tarihi_onarim():
    """TEK SEFERLİK (Kadir onaylı): 116 Trendyol iptalinin hatalı damgalanan tarihini
    kanıttaki GERÇEK iptal tarihine çevirir (48'i Eylül'den Ağustos'a döner).

    Yalnız cancelled_at HÂLÂ hatalı damgaysa ve kaynak trendyol_status_pass/cron ise
    yazılır (sonradan değişmiş kayda dokunulmaz). Eski değer ve kanıt kayda işlenir →
    geri alınabilir. Tutar/statü/stok DEĞİŞMEZ; yalnız iptal tarihi.
    """
    from routes.deps import db  # lazy import
    _flag = "ty_iptal_tarihi_onarim_v1"
    try:
        _cur = await db.settings.find_one({"id": _flag}, {"_id": 0, "done": 1})
        if (_cur or {}).get("done"):
            return
        _now = datetime.now(timezone.utc).isoformat()
        duzeltilen, atlanan, ornek_atlanan = 0, 0, []
        for _no, _eski, _yeni, _kanit in _TY_IPTAL_TARIHI_ONARIM:
            res = await db.orders.update_one(
                {"order_number": _no, "platform": "trendyol",
                 "status": {"$in": ["cancelled", "cancel_refunded"]},
                 "cancelled_at_source": {"$in": ["trendyol_status_pass", "trendyol_cron"]},
                 "cancelled_at": {"$regex": "^" + _eski}},
                {"$set": {"cancelled_at": _yeni + "+00:00",
                          "cancelled_at_source": "onarim_2026_09_24:" + _kanit,
                          "cancelled_at_fix_from": _eski,
                          "cancelled_at_fixed_at": _now}})
            if res.modified_count:
                duzeltilen += 1
            else:
                atlanan += 1
                if len(ornek_atlanan) < 10:
                    ornek_atlanan.append(_no)
        try:
            from routes.reports import invalidate_report_cache
            await invalidate_report_cache()
        except Exception:
            pass
        await db.settings.update_one(
            {"id": _flag},
            {"$set": {"id": _flag, "done": True, "duzeltilen": duzeltilen, "atlanan": atlanan,
                      "ornek_atlanan": ornek_atlanan, "liste": len(_TY_IPTAL_TARIHI_ONARIM),
                      "applied_at": _now}},
            upsert=True)
        logger.info(f"[scheduler] Trendyol iptal tarihi onarımı: {duzeltilen} düzeltildi, "
                    f"{atlanan} atlandı — tutar/statü/stok DEĞİŞMEDİ")
    except Exception as e:
        try:
            await db.settings.update_one(
                {"id": _flag}, {"$set": {"id": _flag, "done": False,
                                         "hata": f"{type(e).__name__}: {str(e)[:300]}"}},
                upsert=True)
        except Exception:
            pass
        logger.warning(f"[scheduler] _ensure_ty_iptal_tarihi_onarim failed: {e}")


async def _ensure_ty_mutabakat_fix():
    """TEK SEFERLİK (Kadir onaylı): Trendyol mutabakatında bulunan iki kayıt sorunu.

    1) 21 ESKİ SİPARİŞİN TARİHİ. Pazaryeri sipariş tarihleri YOKTU; rapor created_at'e
       (senkron anı) düştüğü için haziran/temmuz cirosunda duruyorlardı (haziran
       14.634,98 TL, temmuz 13.530,42 TL). Trendyol'un haziran/temmuz satış listelerinde
       YOKLAR; numaraları haziranın ilk siparişinden (11280487120, 1 Haz 00:32) küçük.
       Gerçek ay, Trendyol dosyalarındaki gerçek tarihli siparişlerin numara aralığından
       çıkarıldı. YALNIZ marketplace_order_date YOKSA yazılır (gerçek tarih asla ezilmez);
       eski değer ve yöntem kayda işlenir → geri alınabilir. Tutar/statü/stok DEĞİŞMEZ.
       Mayısta iptal edilmiş 6 siparişin iptal tarihi de haziranda görünüyordu (senkron
       anı); Trendyol'un haziran–eylül iptal listelerinin HİÇBİRİNDE yoklar → iptal mayısta
       olmuş. Yalnız iptal tarihi haziran veya sonrasıysa sipariş tarihine çekilir.

    2) İADE TALEBİ 11372234609 (claim 044c0d06…): bizde bütünüyle "Rejected", Trendyol'da
       bir kalem ONAYLI (8684483527852), bir kalem reddedilmiş. Talep Trendyol'dan CANLI
       yeniden çekilir ve normal senkronun kuralıyla güncellenir (manual_locked'a saygı).
    """
    from routes.deps import db  # lazy import
    _flag = "ty_mutabakat_fix_v1"
    try:
        _cur = await db.settings.find_one({"id": _flag}, {"_id": 0, "done": 1})
        if (_cur or {}).get("done"):
            return
        _now = datetime.now(timezone.utc).isoformat()
        rapor = {"tarih": [], "iptal_tarihi": [], "talep": {}}
        _bos = {"$or": [{"marketplace_order_date": {"$exists": False}},
                        {"marketplace_order_date": {"$in": [None, ""]}}]}
        for _no, _utc, _kesin, _mayis_iptal in _TY_ESKI_SIPARIS_TARIHLERI:
            _q = {"order_number": _no, "platform": "trendyol", **_bos}
            _eski = await db.orders.find_one(_q, {"_id": 0, "created_at": 1, "cancelled_at": 1,
                                                   "status": 1})
            if not _eski:
                rapor["tarih"].append({"no": _no, "sonuc": "atlandı (tarih zaten dolu ya da yok)"})
                continue
            _set = {"marketplace_order_date": _utc,
                    "order_date_source": "trendyol_numara_araligi",
                    "order_date_ay_kesin": bool(_kesin),
                    "order_date_fix_from": None,
                    "order_date_fixed_at": _now}
            r = await db.orders.update_many(_q, {"$set": _set})
            rapor["tarih"].append({"no": _no, "yeni": _utc, "ay_kesin": bool(_kesin),
                                   "eski_created_at": str(_eski.get("created_at") or ""),
                                   "belge": r.modified_count})
            if _mayis_iptal:
                _ca = str(_eski.get("cancelled_at") or "")
                if _ca >= "2026-06-01":
                    r2 = await db.orders.update_many(
                        {"order_number": _no, "platform": "trendyol",
                         "cancelled_at": {"$gte": "2026-06-01"}},
                        {"$set": {"cancelled_at": _utc, "cancelled_at_fix_from": _ca,
                                  "cancelled_at_source": "trendyol_mutabakat_mayis"}})
                    rapor["iptal_tarihi"].append({"no": _no, "eski": _ca, "yeni": _utc,
                                                  "belge": r2.modified_count})

        # 2) Talep: Trendyol'dan canlı yeniden çek
        try:
            from routes.integrations_trendyol import get_trendyol_config
            from routes.integrations_common import _derive_claim_status
            from trendyol_client import TrendyolClient
            import httpx as _httpx
            _cid, _onum = "044c0d06-02f3-4c6b-8933-900c768ee9d0", "11372234609"
            cfg = await get_trendyol_config()
            cli = TrendyolClient(supplier_id=cfg["supplier_id"], api_key=cfg["api_key"],
                                 api_secret=cfg["api_secret"], mode=cfg["mode"])
            url = f"{cli.base_url}/order/sellers/{cli.supplier_id}/claims"
            async with _httpx.AsyncClient(timeout=25.0) as hc:
                r = await hc.get(url, headers=cli._get_headers(),
                                 params={"page": 0, "size": 20, "orderNumber": _onum})
                canli = [c for c in ((r.json().get("content") or []) if r.status_code == 200 else [])
                         if str(c.get("id") or c.get("claimId") or "") == _cid]
            if not canli:
                rapor["talep"] = {"sonuc": "Trendyol'da bulunamadı — DEĞİŞİKLİK YOK",
                                  "http": r.status_code}
            else:
                cl = canli[0]
                ex = await db.trendyol_claims.find_one(
                    {"claim_id": _cid}, {"_id": 0, "claim_status": 1, "manual_locked": 1,
                                         "return_approved_at": 1})
                _yeni = _derive_claim_status(cl)
                _set = {"raw_data": cl, "updated_at": _now,
                        "claim_status_fix_from": (ex or {}).get("claim_status")}
                if not (ex or {}).get("manual_locked"):
                    _set["claim_status"] = _yeni
                    if _yeni == "Accepted" and not (ex or {}).get("return_approved_at"):
                        _lm = cl.get("lastModifiedDate")
                        try:
                            _set["return_approved_at"] = datetime.fromtimestamp(
                                int(_lm) / 1000, tz=timezone.utc).isoformat() if _lm else _now
                        except Exception:
                            _set["return_approved_at"] = _now
                await db.trendyol_claims.update_one({"claim_id": _cid}, {"$set": _set})
                _kalem = [((ci.get("claimItemStatus") or {}).get("name"))
                          for ln in (cl.get("items") or []) for ci in (ln.get("claimItems") or [])]
                rapor["talep"] = {"sonuc": "güncellendi", "eski": (ex or {}).get("claim_status"),
                                  "yeni": _set.get("claim_status", "(manuel kilitli — değişmedi)"),
                                  "kalem_statuleri": _kalem}
        except Exception as _te:
            rapor["talep"] = {"hata": f"{type(_te).__name__}: {str(_te)[:200]}"}

        await db.settings.update_one(
            {"id": _flag},
            {"$set": {"id": _flag, "done": True, "applied_at": _now,
                      "tarih_duzeltilen": sum(1 for x in rapor["tarih"] if x.get("belge")),
                      "iptal_tarihi_duzeltilen": len(rapor["iptal_tarihi"]),
                      "rapor": rapor}}, upsert=True)
        logger.info(f"[scheduler] Trendyol mutabakat düzeltmesi uygulandı: {rapor['talep']}")
    except Exception as e:
        try:
            await db.settings.update_one(
                {"id": _flag}, {"$set": {"id": _flag, "done": False,
                                         "hata": f"{type(e).__name__}: {str(e)[:300]}"}},
                upsert=True)
        except Exception:
            pass
        logger.warning(f"[scheduler] _ensure_ty_mutabakat_fix failed: {e}")


async def _ensure_trendyol_date_utc_fix():
    """TEK SEFERLİK: geçmiş Trendyol siparişlerinin tarihini GERÇEK UTC'ye çevir.

    Trendyol'un ms-epoch'u Türkiye saatini taşıyor; eski kayıtlar bu değeri UTC
    etiketiyle sakladı. Raporlar UTC→TR çevirirken 3 saat DAHA eklediği için
    21:00 sonrası siparişler ertesi güne — ayın son günündeyse ERTESİ AYA — yazıldı.
    (Kanıt: Trendyol'un TR 21:09 dediği sipariş bizde 21:08:57+00:00 duruyordu.)

    Yapılan: marketplace_order_date ve (yalnız ondan türetilmişse) created_at
    3 saat GERİ alınır. İdempotent — ty_date_utc_fixed damgası bir daha uygulanmasını
    engeller. Sipariş numarası, tutar, statü, kalemler, STOK DEĞİŞMEZ; yalnız tarih.
    """
    from routes.deps import db  # lazy import
    _flag = "ty_date_utc_fix_v1"
    try:
        _cur = await db.settings.find_one({"id": _flag}, {"_id": 0, "done": 1})
        if (_cur or {}).get("done"):
            return
        from pymongo import UpdateOne
        _OFF = timedelta(hours=3)

        def _geri(v):
            try:
                d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            except Exception:
                return None
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return (d - _OFF).isoformat()

        ops, toplam, ay_degisen = [], 0, 0
        async for o in db.orders.find(
                {"platform": "trendyol", "ty_date_utc_fixed": {"$ne": True},
                 "marketplace_order_date": {"$exists": True, "$nin": ["", None]}},
                {"_id": 1, "marketplace_order_date": 1, "created_at": 1}):
            _mod = str(o.get("marketplace_order_date") or "")
            _yeni = _geri(_mod)
            if not _yeni:
                continue
            _set = {"marketplace_order_date": _yeni, "ty_date_utc_fixed": True,
                    "ty_date_utc_fix_from": _mod}
            # created_at YALNIZ sipariş tarihinden türetilmişse kaydırılır; senkron
            # anıyla doldurulmuş olanlara DOKUNULMAZ (yanlış kaydırma olmasın).
            if str(o.get("created_at") or "") == _mod:
                _set["created_at"] = _yeni
            if _mod[:7] != _yeni[:7]:
                ay_degisen += 1
            ops.append(UpdateOne({"_id": o["_id"], "ty_date_utc_fixed": {"$ne": True}},
                                 {"$set": _set}))
            if len(ops) >= 500:
                r = await db.orders.bulk_write(ops, ordered=False)
                toplam += r.modified_count
                ops = []
        if ops:
            r = await db.orders.bulk_write(ops, ordered=False)
            toplam += r.modified_count
        await db.settings.update_one(
            {"id": _flag},
            {"$set": {"id": _flag, "done": True, "duzeltilen": toplam,
                      "ayi_degisen": ay_degisen,
                      "applied_at": datetime.now(timezone.utc).isoformat()}},
            upsert=True)
        logger.info(f"[scheduler] Trendyol sipariş tarihleri UTC'ye çevrildi: {toplam} "
                    f"kayıt ({ay_degisen} tanesinin AYI değişti) — tutar/statü/stok DEĞİŞMEDİ")
    except Exception as e:
        try:
            await db.settings.update_one(
                {"id": _flag}, {"$set": {"id": _flag, "done": False,
                                         "hata": f"{type(e).__name__}: {str(e)[:300]}"}},
                upsert=True)
        except Exception:
            pass
        logger.warning(f"[scheduler] _ensure_trendyol_date_utc_fix failed: {e}")


async def _ensure_cancelled_at_backfill():
    """TEK SEFERLİK: geçmiş iptallerin tarihini KANITTAN çıkar ve DONDUR.

    Sorun: rapor iptali "iptalin kesinleştiği ay"a yazıyor; tarih `cancelled_at`
    yoksa `updated_at`'ten okunuyordu. `updated_at` sipariş her senkronlandığında
    (2 dk) ileri kaydığı için GEÇMİŞ AYLARIN iptal rakamı her gün değişiyordu.

    Tarih şu sırayla, EN GÜVENİLİR KANITTAN seçilir:
      1) status_history içindeki 'cancelled' kaydının 'at' değeri (en eskisi)
      2) siparişin iptal/stok-iade hareketinin created_at'i (sistem o an işledi)
      3) refund_paid_at / expired_at gibi ilgili damgalar
      4) son çare: updated_at (kaynağı 'updated_at_fallback' diye İŞARETLENİR —
         hangi kayıtların tahmini olduğu sonradan görülebilsin)
    Hangi yoldan geldiği cancelled_at_source alanına yazılır.

    Sipariş STATÜSÜNE, tutarına veya stoğuna DOKUNMAZ — yalnız tarih alanı ekler.
    """
    from routes.deps import db  # lazy import
    _flag = "cancelled_at_backfill_v1"
    try:
        _cur = await db.settings.find_one({"id": _flag}, {"_id": 0, "done": 1})
        if (_cur or {}).get("done"):
            return
        _ST = ["cancelled", "cancel_refunded"]
        kaynak = {}
        yazildi = 0
        _batch = []
        async for o in db.orders.find(
                {"status": {"$in": _ST}, "cancelled_at": {"$exists": False}},
                {"_id": 0, "id": 1, "order_number": 1, "status_history": 1,
                 "refund_paid_at": 1, "expired_at": 1, "updated_at": 1}):
            _batch.append(o)
        # Hareket defterinden kanıt (tek seferde, sipariş sipariş sorgu atmadan)
        _ids = [str(o.get("id")) for o in _batch if o.get("id")]
        _mv = {}
        _RT = ["order_cancelled", "auto_cancel_expired", "havale_auto_cancel"]
        for i in range(0, len(_ids), 4000):
            async for m in db.stock_movements.find(
                    {"order_id": {"$in": _ids[i:i + 4000]}, "type": {"$in": _RT}},
                    {"_id": 0, "order_id": 1, "created_at": 1}):
                _k = str(m.get("order_id"))
                _c = str(m.get("created_at") or "")
                if _c and (_k not in _mv or _c < _mv[_k]):
                    _mv[_k] = _c        # en ERKEN hareket = iptalin işlendiği an
        for o in _batch:
            _when, _src = "", ""
            for h in (o.get("status_history") or []):
                if str((h or {}).get("status") or "") in _ST:
                    _at = str((h or {}).get("at") or "")
                    if _at and (not _when or _at < _when):
                        _when, _src = _at, "status_history"
            if not _when:
                _c = _mv.get(str(o.get("id")))
                if _c:
                    _when, _src = _c, "stock_movement"
            if not _when:
                for _f in ("refund_paid_at", "expired_at"):
                    if o.get(_f):
                        _when, _src = str(o[_f]), _f
                        break
            if not _when:
                _when, _src = str(o.get("updated_at") or ""), "updated_at_fallback"
            if not _when:
                continue
            try:
                r = await db.orders.update_one(
                    {"id": o.get("id"), "cancelled_at": {"$exists": False}},
                    {"$set": {"cancelled_at": _when, "cancelled_at_source": _src}})
                if r.modified_count:
                    yazildi += 1
                    kaynak[_src] = kaynak.get(_src, 0) + 1
            except Exception:
                pass
        # Kısmi iptaller: partial_cancel_at yoksa mevcut updated_at ile DONDUR
        # (daha iyi kanıt yok; en azından bir daha kaymaz).
        _kismi = 0
        try:
            async for o in db.orders.find(
                    {"partial_cancel_amount": {"$gt": 0},
                     "partial_cancel_at": {"$exists": False}},
                    {"_id": 0, "id": 1, "updated_at": 1}):
                r = await db.orders.update_one(
                    {"id": o.get("id"), "partial_cancel_at": {"$exists": False}},
                    {"$set": {"partial_cancel_at": str(o.get("updated_at") or ""),
                              "partial_cancel_at_source": "updated_at_fallback"}})
                _kismi += 1 if r.modified_count else 0
        except Exception as _ke:
            logger.warning(f"[iptal-tarihi] kısmi iptal dondurma: {_ke}")
        await db.settings.update_one(
            {"id": _flag},
            {"$set": {"id": _flag, "done": True, "tam_iptal": yazildi,
                      "kismi_iptal": _kismi, "kaynak_dagilimi": kaynak,
                      "applied_at": datetime.now(timezone.utc).isoformat()}},
            upsert=True)
        logger.info(f"[scheduler] iptal tarihi donduruldu: {yazildi} tam + {_kismi} kısmi "
                    f"({kaynak}) — statü/tutar/stok DEĞİŞMEDİ")
    except Exception as e:
        try:
            await db.settings.update_one(
                {"id": _flag}, {"$set": {"id": _flag, "done": False,
                                         "hata": f"{type(e).__name__}: {str(e)[:300]}"}},
                upsert=True)
        except Exception:
            pass
        logger.warning(f"[scheduler] _ensure_cancelled_at_backfill failed: {e}")


async def _ensure_restock_delta_backfill():
    """TEK SEFERLİK · YALNIZ DEFTER: eski pazaryeri iade satırlarına 'delta' yaz.

    Pazaryeri iade (claim) geri-stok hareketleri deftere 'qty' alan adıyla yazılmıştı;
    sistemdeki tüm okuyucular ise 'delta' okur (_restock_authoritative, reaktivasyon
    yeniden-düşümü, iade-reddi geri alma, raporlar). Bu yüzden o satırlar "0 adet"
    görünüyor, aynı sipariş İKİNCİ kez geri stoklanabiliyordu.

    HİÇBİR STOK SAYISINI DEĞİŞTİRMEZ — yalnız hareket kaydına delta=qty ekler (qty
    korunur). Yalnız type='return_restock' satırlarına dokunur.

    DAYANIKLILIK: önce tek aggregation-pipeline güncellemesi denenir; sunucu sürümü
    desteklemez ya da başka bir nedenle hata verirse belge belge Python tarafında
    yapılır. Sonuç (başarı VEYA hata) settings'e YAZILIR — aksi halde hata yutulup
    "çalıştı mı?" sorusu cevapsız kalıyordu. Yalnız done=True ise tekrar çalışmaz.
    """
    from routes.deps import db  # lazy import
    _flag = "restock_delta_backfill_v1"
    try:
        _cur = await db.settings.find_one({"id": _flag}, {"_id": 0, "done": 1})
        if (_cur or {}).get("done"):
            return
        Q = {"type": "return_restock", "items.qty": {"$exists": True},
             "items.delta": {"$exists": False}}
        n, yol, hata = 0, "", ""
        try:
            res = await db.stock_movements.update_many(Q, [{"$set": {"items": {"$map": {
                "input": {"$ifNull": ["$items", []]}, "as": "it",
                "in": {"$cond": [
                    {"$and": [
                        {"$ne": [{"$ifNull": ["$$it.qty", None]}, None]},
                        {"$eq": [{"$ifNull": ["$$it.delta", None]}, None]},
                    ]},
                    {"$mergeObjects": ["$$it", {"delta": "$$it.qty"}]},
                    "$$it",
                ]}}}}}])
            n, yol = res.modified_count, "pipeline"
        except Exception as _pe:
            hata = f"pipeline: {type(_pe).__name__}: {str(_pe)[:200]}"
            yol = "python"
            async for mv in db.stock_movements.find(Q, {"_id": 1, "items": 1}):
                yeni, degisti = [], False
                for it in (mv.get("items") or []):
                    if isinstance(it, dict) and it.get("delta") is None and it.get("qty") is not None:
                        it = {**it, "delta": it["qty"]}
                        degisti = True
                    yeni.append(it)
                if degisti:
                    await db.stock_movements.update_one(
                        {"_id": mv["_id"]}, {"$set": {"items": yeni}})
                    n += 1
        await db.settings.update_one(
            {"id": _flag},
            {"$set": {"id": _flag, "done": True, "yol": yol, "hareket": n,
                      "pipeline_hatasi": hata,
                      "applied_at": datetime.now(timezone.utc).isoformat()}},
            upsert=True)
        logger.info(f"[scheduler] iade hareketlerine delta yazıldı: {n} kayıt ({yol}) "
                    f"— stok DEĞİŞMEDİ, yalnız defter")
    except Exception as e:
        # Hata da KAYDEDİLİR (done=False) ki bir sonraki açılışta tekrar denensin
        # ve "neden çalışmadı" sorusu görünür olsun.
        try:
            await db.settings.update_one(
                {"id": _flag},
                {"$set": {"id": _flag, "done": False,
                          "hata": f"{type(e).__name__}: {str(e)[:300]}",
                          "denendi_at": datetime.now(timezone.utc).isoformat()}},
                upsert=True)
        except Exception:
            pass
        logger.warning(f"[scheduler] _ensure_restock_delta_backfill failed: {e}")


async def _ensure_consignment_removed():
    """Tek seferlik: KONSİNYE STOK alanını veritabanından da tamamen kaldır.

    Kullanıcı kararı ("konsinye stoğu tamamen sil sistemden"). Alan Ticimax Excel'inden
    gelmişti, HİÇBİR hesaplamada kullanılmıyordu (ölü alan) — koddan çıkarıldı, burada
    ürün belgelerinden de silinir. Bayrak (settings.consignment_removed_v1) bir kez uygular.
    Yalnız bu iki alanı $unset eder; başka hiçbir veriye dokunmaz.
    """
    from routes.deps import db  # lazy import
    try:
        if await db.settings.find_one({"id": "consignment_removed_v1"}):
            return
        r1 = await db.products.update_many(
            {"consignment_stock": {"$exists": True}}, {"$unset": {"consignment_stock": ""}})
        r2 = await db.products.update_many(
            {"ticimax_fields.KONSINYESTOKADEDI": {"$exists": True}},
            {"$unset": {"ticimax_fields.KONSINYESTOKADEDI": ""}})
        await db.settings.update_one(
            {"id": "consignment_removed_v1"},
            {"$set": {"id": "consignment_removed_v1",
                      "applied_at": datetime.now(timezone.utc).isoformat(),
                      "products": r1.modified_count, "ticimax_fields": r2.modified_count}},
            upsert=True)
        logger.info(f"[scheduler] Konsinye stok alanı silindi "
                    f"(ürün: {r1.modified_count}, ticimax_fields: {r2.modified_count})")
    except Exception as e:
        logger.warning(f"[scheduler] _ensure_consignment_removed failed: {e}")


async def _ensure_all_orders_2min():
    """Tek seferlik: TÜM pazaryeri hesaplarında sipariş çekme periyodu 2 dk.

    Kullanıcı kararı ("sipariş çekmeyi 2 dk yap tüm pazaryerlerinde"). Amazon zaten
    scheduler'da sabit 2 dk. Bayrak (settings.all_orders_2min_v1) bir kez uygular;
    sonradan Admin → Otomasyon panelinden değiştirilirse tekrar EZİLMEZ.
    """
    from routes.deps import db  # lazy import
    try:
        if await db.settings.find_one({"id": "all_orders_2min_v1"}):
            return
        res = await db.marketplace_accounts.update_many(
            {}, {"$set": {"auto_sync.orders_enabled": True,
                          "auto_sync.orders_interval_min": 2}})
        await db.settings.update_one(
            {"id": "all_orders_2min_v1"},
            {"$set": {"id": "all_orders_2min_v1",
                      "applied_at": datetime.now(timezone.utc).isoformat()}},
            upsert=True)
        logger.info(f"[scheduler] Sipariş çekme 2 dk'ya ayarlandı ({res.modified_count} pazaryeri hesabı)")
    except Exception as e:
        logger.warning(f"[scheduler] _ensure_all_orders_2min failed: {e}")


async def _ensure_panel_session_24h():
    """Tek seferlik (kullanıcı isteği 2026-10-06: "panele girenlerin oturumu aynı cihazda 24 saat
    sonlanmasın"): kayıtlı panel boşta-kalma süresi 24 saatten KISAYSA 1440 dk yapılır. 0 (kapalı)
    zaten daha serbesttir, dokunulmaz. Bayrak (settings.panel_session_24h_v1) bir kez uygular;
    sonradan İşletme Kuralları'ndan değiştirilirse tekrar EZİLMEZ."""
    from routes.deps import db  # lazy import
    try:
        if await db.settings.find_one({"id": "panel_session_24h_v1"}):
            return
        doc = await db.settings.find_one({"id": "business_rules"}, {"_id": 0, "values": 1}) or {}
        cur = (doc.get("values") or {}).get("panel.auto_logout_minutes")
        changed = False
        try:
            cur_f = float(cur) if cur is not None else None
        except (TypeError, ValueError):
            cur_f = None
        if cur_f is not None and 0 < cur_f < 1440:
            # Anahtar noktalı ("panel.auto_logout_minutes") → $set yolu iç içe alan yaratır; kuralı
            # panelin kullandığı kaydedici yazar (values sözlüğü bütün olarak güncellenir + önbellek).
            import business_rules as _br
            await _br.save_rules(db, {"panel.auto_logout_minutes": 1440})
            changed = True
        await db.settings.update_one(
            {"id": "panel_session_24h_v1"},
            {"$set": {"id": "panel_session_24h_v1", "previous": cur, "changed": changed,
                      "applied_at": datetime.now(timezone.utc).isoformat()}},
            upsert=True)
        logger.info(f"[scheduler] Panel boşta-kalma süresi 24 saat (önceki: {cur}, değişti: {changed})")
    except Exception as e:
        logger.warning(f"[scheduler] _ensure_panel_session_24h failed: {e}")


async def _ensure_provider_secrets_encrypted():
    """Denetim #6: bildirim sağlayıcı sırlarının (İYS şifre/appkey eş adları dahil) düz metin kalan
    eski kayıtlarını at-rest şifreler. İdempotent (zaten şifreliye dokunmaz); değer değişmez."""
    from routes.deps import db  # lazy import
    try:
        from notification_service import encrypt_legacy_provider_secrets
        n = await encrypt_legacy_provider_secrets(db)
        if n:
            logger.warning(f"[scheduler] {n} düz metin sağlayıcı sırrı şifrelendi (denetim #6)")
        # Denetim #26: eski kayıtta token'lı XML feed adresi DÜZ duruyorsa token'sız görünüme indir.
        xf = await db.settings.find_one({"id": "xml_feed"}, {"_id": 0, "xml_url": 1}) or {}
        if xf.get("xml_url"):
            from security.feed_integrity import safe_source as _safe_src
            await db.settings.update_one({"id": "xml_feed"},
                                         {"$set": {"xml_url_display": _safe_src(xf["xml_url"])},
                                          "$unset": {"xml_url": ""}})
    except Exception as e:
        logger.warning(f"[scheduler] _ensure_provider_secrets_encrypted failed: {type(e).__name__}")


async def _ensure_hb_2min_sync():
    """Tek seferlik: Hepsiburada hesabını 2 dk'da bir STOK + SİPARİŞ senkronuna ayarlar.
    settings.hb_sync_2min_v1 bayrağıyla yalnızca bir kez uygulanır; sonradan
    Admin → Otomasyon panelinden serbestçe değiştirilebilir (bayrak tekrar yazmaz)."""
    from routes.deps import db  # lazy import
    try:
        flag = await db.settings.find_one({"id": "hb_sync_2min_v1"})
        if flag:
            return
        await db.marketplace_accounts.update_one(
            {"key": "hepsiburada"},
            {"$set": {
                "enabled": True,
                "auto_sync.products_enabled": True,
                "auto_sync.orders_enabled": True,
                "auto_sync.products_interval_min": 2,
                "auto_sync.orders_interval_min": 2,
            }},
        )
        await db.settings.update_one(
            {"id": "hb_sync_2min_v1"},
            {"$set": {"id": "hb_sync_2min_v1", "applied_at": datetime.now(timezone.utc).isoformat()}},
            upsert=True,
        )
        logger.info("[scheduler] Hepsiburada senkron 2 dk'ya ayarlandı (stok + sipariş)")
    except Exception as e:
        logger.warning(f"[scheduler] _ensure_hb_2min_sync failed: {e}")


async def _run_trendyol_auto_products_sync():
    """Scheduler tarafından Trendyol fiyat/stok senkronunu tetikler.
    HTTPException'ı yutar, log bırakır. Trendyol konfigürasyonu yoksa sessizce atlar.
    """
    from routes.deps import db
    from routes.marketplace_hub import log_integration_event
    try:
        from routes.integrations import _sync_inventory_to_trendyol, get_trendyol_config
        cfg = await get_trendyol_config()
        if not cfg.get("is_active"):
            return
        # PERFORMANS: tam belge yerine hafif projeksiyon (görsel/açıklama/Ticimax blob'ları hariç).
        products = await db.products.find({"is_active": True, "is_deleted": {"$ne": True}}, _STOCK_SYNC_PROJ).to_list(length=None)
        res = await _sync_inventory_to_trendyol(products)
        await log_integration_event(
            marketplace="trendyol", action="stock_update",
            status=("success" if res.get("success") else "failed"),
            direction="outbound",
            message=f"[cron] Trendyol stok/fiyat senkronu: {res.get('message', '')}"
        )
    except Exception as e:
        try:
            await log_integration_event(
                marketplace="trendyol", action="stock_update", status="failed",
                direction="outbound",
                message=f"[cron] Trendyol ürün senkron hatası: {e}"
            )
        except Exception:
            pass


async def _update_existing_trendyol_order(_db, existing, data, number, restock_source,
                                          t_order=None):
    """Mevcut Trendyol siparişini günceller: status DIŞI alanları (isim/adres/kalem)
    tazeler VE terminal pazaryeri durumlarını (iptal/iade) YANSITIR.

    Eksik iptal kök nedeni: statussuz genel çekiş TÜM durumları döndürür (Cancelled
    dahil) ama eskiden status hariç tutulduğu için satıcı/stok iptalleri (claim
    ÜRETMEZ) 'confirmed' kalıp İptaller'e hiç düşmüyordu. Artık terminal durum
    yansıtılır; confirmed→cancelled geçişinde idempotent stok iadesi yapılır
    (status pass ile AYNI guard: stock_movements type=order_cancelled).

    Döner: flipped_to_cancelled (bool).
    """
    _set = {k: v for k, v in data.items() if k != "status"}
    _new_status = data.get("status")
    _prev_status = existing.get("status")
    if _new_status in ("cancelled", "returned") and _prev_status != _new_status:
        _set["status"] = _new_status
        if _new_status == "cancelled":
            _set.setdefault("cancel_source", "trendyol")
    else:
        # Kargoya göre İLERİ durum (Kargoya Verildi / Teslim Edildi) — yalnız açık durumlardan.
        try:
            from routes.integrations_trendyol import ty_cargo_status_set
            _set.update(ty_cargo_status_set(existing, data))
        except Exception as _ce:
            logger.warning(f"[trendyol cargo status] {number}: {_ce}")
    _upd_doc = {"$set": _set}
    try:  # KVKK: mühürlü eşleme yazılırken eski düz TCKN (trendyol_identity_number) kalmasın
        from security.pii import legacy_plain_unset as _pii_unset
        if _pii_unset(data):
            _upd_doc["$unset"] = _pii_unset(data)
    except Exception:
        pass
    await _db.orders.update_one({"_id": existing["_id"]}, _upd_doc)
    _flipped = (_new_status == "cancelled" and _prev_status != "cancelled")
    if _flipped and _prev_status != "cancel_refunded":
        # İPTAL TARİHİNİ DONDUR (bir kez) — raporun "hangi ay iptal edildi" cevabı
        # updated_at'e düşmesin; updated_at her senkronda ileri kayıyor.
        # YALNIZ GERÇEK GEÇİŞTE (önceki statü iptal/iptal-iade değilken). Tarih: Trendyol
        # paketinin GERÇEK iptal zamanı (packageHistories/lastModifiedDate), yoksa şimdi.
        try:
            from routes.orders import stamp_cancelled_at
            _ct, _cts = "", ""
            if t_order:
                try:
                    from routes.integrations_trendyol import _ty_cancel_time_iso
                    _ct, _cts = _ty_cancel_time_iso(t_order)
                except Exception:
                    _ct, _cts = "", ""
            await stamp_cancelled_at(order_id=existing.get("id"), order_number=number,
                                     when=_ct,
                                     source=(f"trendyol_cron:{_cts}" if _ct else "trendyol_cron"))
        except Exception:
            pass
    if _flipped and existing.get("id"):
        try:
            from routes.orders import _stock_delta_for_order, _RESTORE_MOVE_TYPES
            # B4: guard'ı TÜM restore hareketlerine genişlet. Yalnız 'order_cancelled' aramak,
            # sipariş önceden kısmi iade (return_restock/order_returned) ile geri stoklanmışsa
            # bunu göremeyip TÜM siparişi İKİNCİ kez +stokluyordu.
            # B10: guard'ı order_id VE order_number ile al — mükerrer sipariş dokümanı olsa bile
            # herhangi biri restock edilmişse tekrar +stok yapma (hayalet stok döngüsü fix'i).
            _already = await _db.stock_movements.find_one(
                {"$or": [{"order_id": existing.get("id")}, {"order_number": number}],
                 "type": {"$in": _RESTORE_MOVE_TYPES}}, {"_id": 1}
            )
            # FAZ 2: hiç düşülmemiş siparişe stok EKLEME (hayalet stok).
            from routes.orders import _order_was_deducted, _log_restock_skip
            if not _already and not await _order_was_deducted(existing):
                logger.warning(f"[stok] hayalet iade engellendi (trendyol cron): "
                               f"sipariş {number} stoktan hiç düşülmemiş")
                await _log_restock_skip(existing, "order_cancelled", "trendyol_cron_cancel")
                _already = True
            if not _already:
                _moves = await _stock_delta_for_order(existing, +1)
                await _db.stock_movements.insert_one({
                    "id": str(uuid.uuid4()), "type": "order_cancelled",
                    "order_id": existing.get("id"), "order_number": number,
                    "items": _moves, "source": restock_source,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                })
        except Exception as _re:
            logger.error(f"[cron] iptal restock {number}: {_re}")
    return _flipped


async def _reconcile_trendyol_terminal(client, days_back=30, slice_days=15):
    """STATUSSUZ uzlaştırma: statussuz çekiş tüm durumları getirir; terminal duruma
    (iptal/iade) geçmiş ama Atilla'da güncellenmemiş siparişleri İptaller/İadeler'e
    yansıtır/ekler. Satıcı/stok iptalleri (claim üretmeyen, status=Cancelled list
    sorgusunda güvenilir gelmeyen) buradan GARANTİ yakalanır. Volume için tarih
    dilimlerine bölünür. TEK SEFERLİK geniş backfill için days_back büyük verilir."""
    from datetime import datetime as _dt, timedelta as _td
    from routes.deps import db as _db, generate_id
    from routes.integrations import map_trendyol_order, _ms_to_iso
    import asyncio as _aio
    now_ = _dt.now()
    flipped = 0
    inserted = 0
    elapsed = 0
    slice_end = now_
    while elapsed < days_back:
        this_slice = min(slice_days, days_back - elapsed)
        slice_start = slice_end - _td(days=this_slice)
        start_ms = int(slice_start.timestamp() * 1000)
        end_ms = int(slice_end.timestamp() * 1000)
        page = 0
        while page < 50:
            try:
                resp = await client.get_orders(start_date_ms=start_ms, end_date_ms=end_ms, size=200, page=page)
            except Exception as _qe:
                logger.error(f"[cron] terminal reconcile sorgu hatası: {_qe}")
                break
            chunk = resp.get("content", []) or []
            for t_order in chunk:
                try:
                    number = str(t_order.get("orderNumber"))
                    data = map_trendyol_order(t_order)
                    existing = await _db.orders.find_one({"order_number": number, "platform": "trendyol"})
                    if existing:
                        if await _update_existing_trendyol_order(_db, existing, data, number, "trendyol_terminal_reconcile",
                                                                 t_order=t_order):
                            flipped += 1
                    elif data.get("status") in ("cancelled", "returned"):
                        # Hiç içe aktarılmamış ESKİ iptal/iade → İptaller'e indir.
                        # Stok DÜŞÜLMEZ (hiç rezerve edilmemişti). created_at = gerçek
                        # sipariş tarihi (orderDate) → İptaller'de tarih sıralaması doğru.
                        data["id"] = generate_id()
                        data["created_at"] = _ms_to_iso(t_order.get("orderDate")) or datetime.now(timezone.utc).isoformat()
                        await _db.orders.insert_one(data)
                        inserted += 1
                except Exception as _e:
                    logger.error(f"[cron] terminal reconcile {t_order.get('orderNumber')}: {_e}")
            total_pages = resp.get("totalPages") or 0
            page += 1
            if not chunk or page >= total_pages:
                break
        slice_end = slice_start
        elapsed += this_slice
        await _aio.sleep(0.3)  # Trendyol rate limit + sunucuyu yormama
    if flipped or inserted:
        logger.info(f"[cron] terminal uzlaştırma {days_back}g: {flipped} iptal yansıtıldı / {inserted} eklendi")
    return flipped, inserted


async def _run_trendyol_auto_orders_pull():
    """Scheduler tarafından Trendyol sipariş çekmeyi tetikler."""
    from routes.marketplace_hub import log_integration_event
    try:
        from routes.integrations import get_trendyol_config
        cfg = await get_trendyol_config()
        if not cfg.get("is_active"):
            return
        # Doğrudan route fonksiyonunu çağırmıyoruz (require_admin için);
        # onun yerine mantığı burada çoğaltmadan, küçük bir internal job tetikliyoruz.
        import sys, os
        from datetime import datetime as _dt, timedelta as _td
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from trendyol_client import TrendyolClient
        from routes.deps import db as _db, generate_id
        from routes.integrations import map_trendyol_order, _sync_trendyol_status_passes, _ms_to_iso

        client = TrendyolClient(
            supplier_id=cfg["supplier_id"],
            api_key=cfg["api_key"],
            api_secret=cfg["api_secret"],
            mode=cfg["mode"],
        )
        now_ = _dt.now()
        start = now_ - _td(days=14)
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(now_.timestamp() * 1000)
        # Trendyol tek sayfada en fazla 200 paket dondurur; TUM sayfalari dolas.
        # (Sayfalama yokken 200'den fazla siparis olunca 201+ hep atlaniyordu.)
        content = []
        _page = 0
        _MAX_PAGES = 50
        while _page < _MAX_PAGES:
            _resp = await client.get_orders(
                start_date_ms=start_ms, end_date_ms=end_ms, size=200, page=_page
            )
            _chunk = _resp.get("content", []) or []
            content.extend(_chunk)
            _total_pages = _resp.get("totalPages") or 0
            _page += 1
            if not _chunk or _page >= _total_pages:
                break
        imported = 0
        updated = 0
        for t_order in content:
            try:
                number = str(t_order.get("orderNumber"))
                existing = await _db.orders.find_one({"order_number": number, "platform": "trendyol"})
                data = map_trendyol_order(t_order)
                if existing:
                    await _update_existing_trendyol_order(_db, existing, data, number, "trendyol_auto_pull_cancel",
                                                          t_order=t_order)
                    updated += 1
                else:
                    data["id"] = generate_id()
                    # RC4 DENETİM FIX: created_at = GERÇEK sipariş tarihi (orderDate) — diğer 5
                    # Trendyol insert yoluyla (manuel import/backfill/status-sweep) TUTARLI. Eskiden
                    # cron 'now' (senkron anı) yazıyordu → ay sınırındaki siparişler yanlış aya düşüp
                    # aylık pazaryeri adedi Trendyol paneliyle tutmuyordu.
                    data["created_at"] = _ms_to_iso(t_order.get("orderDate")) or datetime.now(timezone.utc).isoformat()
                    await _db.orders.insert_one(data)
                    imported += 1
                    # Zaten iptal/iade durumunda gelen YENİ sipariş için stok DÜŞÜLMEZ
                    # (hiç rezerve edilmemişti → -1 yanlış olurdu).
                    if data.get("status") not in ("cancelled", "returned"):
                        from routes.integrations import _decrement_stock_for_imported_order
                        await _decrement_stock_for_imported_order(data, "trendyol")
            except Exception as _ex:
                logger.error(f"[cron] Trendyol order import hata: {_ex}")
        try:
            await _sync_trendyol_status_passes(client, start_ms, end_ms)
        except Exception as _ex2:
            logger.error(f"[cron] Trendyol status pass hata: {_ex2}")
        await log_integration_event(
            marketplace="trendyol", action="order_pull", status="success",
            direction="inbound",
            message=f"[cron] Trendyol sipariş çekildi: +{imported} yeni / {updated} güncellendi"
        )
    except Exception as e:
        try:
            await log_integration_event(
                marketplace="trendyol", action="order_pull", status="failed",
                direction="inbound",
                message=f"[cron] Trendyol sipariş çekme hatası: {e}"
            )
        except Exception:
            pass


async def _run_trendyol_cancel_pass():
    """HAFİF + SIK iptal taraması (5 dk). Yalnızca Cancelled, son 14 gün, dar pencere →
    yeni iptaller İptaller'e hızlı düşer ama sunucuyu yormaz. Eski/geç iptaller ve
    iade/teslim-edilemedi ayrı SAATLİK geniş tarama (_run_trendyol_status_wide_pass) ile
    kapanır; derin geçmiş manuel backfill butonuyla çekilir."""
    try:
        from routes.integrations import get_trendyol_config
        cfg = await get_trendyol_config()
        if not cfg.get("is_active"):
            return
        from datetime import datetime as _dt, timedelta as _td
        from trendyol_client import TrendyolClient
        from routes.integrations import _sync_trendyol_status_passes
        client = TrendyolClient(
            supplier_id=cfg["supplier_id"], api_key=cfg["api_key"],
            api_secret=cfg["api_secret"], mode=cfg["mode"],
        )
        now_ = _dt.now()
        start_ms = int((now_ - _td(days=14)).timestamp() * 1000)
        end_ms = int(now_.timestamp() * 1000)
        # Sadece Cancelled + dar 14g pencere (widen_cancel=False) → hafif.
        await _sync_trendyol_status_passes(client, start_ms, end_ms, widen_cancel=False, statuses=("Cancelled",))
    except Exception as e:
        logger.error(f"[cron] Trendyol hızlı iptal taraması hatası: {e}")


async def _run_trendyol_status_wide_pass():
    """GENİŞ durum taraması — SAATTE BİR (sık değil → hafif). Cancelled 45 güne kadar
    (geç iptaller), Returned/UnDelivered 30 gün. Sık 5 dk'lık taramanın kaçırdığı
    eski/geç durum değişikliklerini kapatır."""
    try:
        from routes.integrations import get_trendyol_config
        cfg = await get_trendyol_config()
        if not cfg.get("is_active"):
            return
        from datetime import datetime as _dt, timedelta as _td
        from trendyol_client import TrendyolClient
        from routes.integrations import _sync_trendyol_status_passes
        client = TrendyolClient(
            supplier_id=cfg["supplier_id"], api_key=cfg["api_key"],
            api_secret=cfg["api_secret"], mode=cfg["mode"],
        )
        now_ = _dt.now()
        start_ms = int((now_ - _td(days=30)).timestamp() * 1000)
        end_ms = int(now_.timestamp() * 1000)
        await _sync_trendyol_status_passes(client, start_ms, end_ms)  # 3 durum, Cancelled 45g widen
    except Exception as e:
        logger.error(f"[cron] Trendyol geniş durum taraması hatası: {e}")


async def _run_trendyol_deep_backfill_once():
    """TEK SEFERLİK derin uzlaştırma — geçmişteki TÜM Trendyol iptallerini (satıcı/stok
    dahil) sisteme taşır: 365 güne kadar STATUSSUZ tarama, terminal duruma geçmiş ama
    Atilla'da güncellenmemiş siparişleri İptaller'e indirir. Yeni flag
    (trendyol_terminal_backfill) ile YALNIZCA BİR KEZ çalışır; sonra no-op. Sonrasında
    günlük iptalleri 5 dk'lık anlık auto pull (14 gün, terminal flip) yakalar."""
    try:
        from routes.deps import db
        flag = await db.settings.find_one({"id": "trendyol_terminal_backfill"}, {"_id": 0})
        if flag and flag.get("done"):
            return
        from routes.integrations import get_trendyol_config, sync_trendyol_claims
        cfg = await get_trendyol_config()
        if not cfg.get("is_active"):
            return
        from trendyol_client import TrendyolClient
        client = TrendyolClient(
            supplier_id=cfg["supplier_id"], api_key=cfg["api_key"],
            api_secret=cfg["api_secret"], mode=cfg["mode"],
        )
        await db.settings.update_one(
            {"id": "trendyol_terminal_backfill"},
            {"$set": {"id": "trendyol_terminal_backfill", "running": True,
                      "started_at": datetime.now(timezone.utc).isoformat()}},
            upsert=True,
        )
        # STATUSSUZ 365 günlük uzlaştırma (15 günlük dilimler) → satıcı/stok iptalleri
        # dahil TÜM takılı iptaller bir kerede İptaller'e iner.
        flipped, inserted = await _reconcile_trendyol_terminal(client, days_back=365, slice_days=15)
        # Müşteri iptal/iade sebeplerini de tazele (claim → cancel_reason).
        try:
            await sync_trendyol_claims(days_back=180, current_user={"is_admin": True})
        except Exception as _cl:
            logger.error(f"[terminal backfill claims] {_cl}")
        await db.settings.update_one(
            {"id": "trendyol_terminal_backfill"},
            {"$set": {"id": "trendyol_terminal_backfill", "done": True, "running": False,
                      "flipped": flipped, "inserted": inserted,
                      "finished_at": datetime.now(timezone.utc).isoformat()}},
            upsert=True,
        )
        logger.info(f"[scheduler] Trendyol TEK SEFERLİK terminal backfill tamamlandı: "
                    f"{flipped} iptal yansıtıldı / {inserted} eklendi")
    except Exception as e:
        logger.error(f"[cron] Trendyol terminal backfill hatası: {e}")


async def _run_trendyol_open_claims_refresh():
    """AÇIK iade kovalarını (talep/kargoda/aksiyon) TY canlı verisiyle eşitler — dakikada bir."""
    try:
        import sys, os
        sys.path.insert(0, os.path.dirname(__file__))
        from routes.integrations_trendyol import _refresh_open_claims_core
        await _refresh_open_claims_core()
    except Exception as e:
        logger.error(f"[cron] açık iade canlı eşitleme hatası: {e}")


async def _run_trendyol_claims_sync():
    """Trendyol iade/iptal (claims) senkronu — periyodik. Müşterinin Trendyol'da seçtiği
    GERÇEK iptal sebebini çeker ve CANCEL claim'leri eşleşen iptal siparişlerine bağlar
    (İptaller'de "Trendyol iptali" yerine gerçek sebep görünür)."""
    try:
        from routes.integrations import get_trendyol_config
        cfg = await get_trendyol_config()
        if not cfg.get("is_active"):
            return
        # Çekirdek senkron (require_admin bypass) — periyodik kısa pencere; durum
        # geçişleri (Created→Accepted/Rejected) her turda canlı tazelenir.
        from routes.integrations import _sync_trendyol_claims_core
        await _sync_trendyol_claims_core(days_back=60)
    except Exception as e:
        logger.error(f"[cron] Trendyol claims senkron hatası: {e}")


async def _run_hepsiburada_claims_sync():
    """Hepsiburada iade (claims) senkronu — periyodik. HB iade talepleri Trendyol'la
    ortak iade/GP ekranına (db.trendyol_claims, platform=hepsiburada) yazılır."""
    try:
        from routes.integrations_hepsiburada import _sync_hepsiburada_claims_core
        await _sync_hepsiburada_claims_core(days_back=60)
    except Exception as e:
        logger.error(f"[cron] Hepsiburada claims senkron hatası: {e}")


async def _run_amazon_claims_sync():
    """Amazon iade (returns) senkronu — periyodik. Amazon iadeleri Trendyol/HB ile ortak
    iade/GP ekranına (db.trendyol_claims, platform=amazon) yazılır."""
    try:
        from routes.amazon_claims import _sync_amazon_claims_core
        await _sync_amazon_claims_core(days_back=60)
    except Exception as e:
        logger.error(f"[cron] Amazon claims senkron hatası: {e}")


async def _run_passive_marketplace_zero_sweep():
    """PASİF / SİLİNMİŞ ürünlerin pazaryeri stoğunu 0'a çeker (30 dk'da bir + açılışta).

    Kök neden (2026-09-24, "Nerel Kuşaklı Elbise"): periyodik stok senkronu YALNIZ aktif
    ürünleri gönderiyordu; pasife alınan/silinen ürüne 0 yalnız tek tek pasife alınınca
    (ürün düzenleme kancası) gidiyordu. Silme, toplu işlem, Excel vb. yollarda 0 HİÇ
    gitmedi → Trendyol ürünü en son bildiği stokla (96) satmaya devam etti.

    Emniyet: aynı barkod AKTİF bir üründe de varsa (kopya/çöp kopyası) o barkoda
    DOKUNULMAZ — satıştaki ürün sıfırlanmasın. DB stoğu DEĞİŞMEZ; yalnız pazaryeri.
    Trendyol: her turda toplu (dış sistem stoğu geri yazsa da düzelir).
    Hepsiburada/Temu: ürün başına; ürün son 0 gönderiminden beri değişmediyse tekrarlanmaz.
    Sonuç settings.passive_zero_sweep_last → /health."""
    from routes.deps import db
    try:
        act_bcs = set()
        async for p in db.products.find({"is_active": True, "is_deleted": {"$ne": True}},
                                        {"_id": 0, "barcode": 1, "variants.barcode": 1}):
            # Stok gönderimiyle AYNI kural: varyantlı üründe pazaryerine yalnız VARYANT barkodu
            # gider; ana kayıttaki 'barcode' (kopyalamadan kalma) hiçbir ilanı beslemez →
            # koruma kümesine yalnız varyantsız üründe girer. (2026-09-24: kopya ürünlerin ana
            # barkodu, silinmiş ürünün XS barkoduyla aynıydı → 4 ilan sıfırlanamıyordu.)
            if p.get("barcode") and not (p.get("variants") or []):
                act_bcs.add(str(p["barcode"]).strip())
            for v in (p.get("variants") or []):
                if v.get("barcode"):
                    act_bcs.add(str(v["barcode"]).strip())
        adaylar, korunan = [], 0
        async for p in db.products.find({"$or": [{"is_active": False}, {"is_deleted": True}]},
                                        _LIGHT_PRODUCT_PROJ):
            q = dict(p)
            vs = []
            for v in (p.get("variants") or []):
                bc = str(v.get("barcode") or "").strip()
                if not bc:
                    continue
                if bc in act_bcs:
                    korunan += 1
                    continue
                vs.append(v)
            q["variants"] = vs
            pbc = str(p.get("barcode") or "").strip()
            if pbc and pbc in act_bcs:
                q["barcode"] = ""
                korunan += 1
            if not vs and not (q.get("barcode") or ""):
                continue
            # _sync_inventory_to_trendyol varyant yoksa ürün barkodunu kullanır; varyantlı
            # üründe tüm varyantlar aktif kopyada ise liste boş kalır → yukarıda atlandı.
            adaylar.append(q)
        sonuc = {"at": datetime.now(timezone.utc).isoformat(), "aday_urun": len(adaylar),
                 "aktif_kopyasi_oldugu_icin_korunan_barkod": korunan}
        try:
            from routes.integrations_trendyol import _sync_inventory_to_trendyol, get_trendyol_config
            if (await get_trendyol_config()).get("is_active") and adaylar:
                r = await _sync_inventory_to_trendyol(adaylar, force_quantity=0)
                sonuc["trendyol"] = {"ok": r.get("success"), "mesaj": str(r.get("message"))[:300],
                                     "batch": r.get("batch_id")}
        except Exception as e:
            sonuc["trendyol"] = {"hata": f"{type(e).__name__}: {str(e)[:300]}"}
        hb_n, hb_err = 0, 0
        try:
            from routes.integrations_common import on_product_deactivated
            for q in adaylar:
                _u = str(q.get("updated_at") or "")
                _z = str(q.get("mp_zero_pushed_at") or "")
                if _z and _z >= _u:
                    continue
                r = await on_product_deactivated(q, platforms={"hepsiburada", "temu"})
                if "error" in (r.get("hepsiburada") or {}):
                    hb_err += 1
                else:
                    hb_n += 1
                    await db.products.update_one(
                        {"id": q.get("id")},
                        {"$set": {"mp_zero_pushed_at": datetime.now(timezone.utc).isoformat()}})
        except Exception as e:
            sonuc["hb_temu_hata"] = f"{type(e).__name__}: {str(e)[:300]}"
        sonuc["hb_temu"] = {"gonderilen_urun": hb_n, "hatali": hb_err}
        # AMAZON: pasif/silinmiş ürünün SellerSKU'ları (stokkodu-renk-beden) → quantity 0.
        # Yalnız daha önce stoklu gönderilmiş SKU (amazon_sku_state.qty > 0). AKTİF bir ürünün
        # ürettiği SKU ile çakışan (kopya ürün) SKU'ya DOKUNULMAZ. AMAZON_ALLOW_WRITE=0 → dry-run.
        try:
            from routes.amazon_spapi import (_get_config as _az_cfg, _amazon_push_stock_price,
                                             ALLOW_WRITE as _AZ_W, _amazon_seller_sku,
                                             _resolve_amazon_pt_and_defaults)
            _c = await _az_cfg()
            if _c and _c.get("refresh_token_enc") and _c.get("selling_partner_id"):
                _akt_sku = set()
                async for ap in db.products.find({"is_active": True, "is_deleted": {"$ne": True}},
                                                 _LIGHT_PRODUCT_PROJ):
                    for v in (ap.get("variants") or []):
                        try:
                            _k = _amazon_seller_sku(v, ap)
                        except Exception:
                            _k = None
                        if _k:
                            _akt_sku.add(str(_k))
                _ids = [q.get("id") for q in adaylar if q.get("id")]
                _harici = {}
                async for m in db.amazon_sku_map.find({"matched": True, "product_id": {"$in": _ids}},
                                                      {"_id": 0, "sku": 1, "product_id": 1, "product_type": 1}):
                    if m.get("sku"):
                        _harici.setdefault(m["product_id"], []).append((m["sku"], m.get("product_type")))
                az_ok = az_err = az_skip = az_korunan = 0
                for q in adaylar:
                    _hedef = []
                    for v in (q.get("variants") or []):
                        try:
                            _k = _amazon_seller_sku(v, q)
                        except Exception:
                            _k = None
                        if _k:
                            _hedef.append((str(_k), None))
                    _hedef += _harici.get(q.get("id"), [])
                    _pt = None
                    for sku, mpt in _hedef:
                        if sku in _akt_sku:
                            az_korunan += 1
                            continue
                        st = await db.amazon_sku_state.find_one({"sku": sku}, {"_id": 0, "qty": 1})
                        if not st or int(st.get("qty") or 0) <= 0:
                            az_skip += 1
                            continue
                        if _pt is None:
                            try:
                                _pt = (await _resolve_amazon_pt_and_defaults(q))[0] or "PRODUCT"
                            except Exception:
                                _pt = "PRODUCT"
                        try:
                            r = await _amazon_push_stock_price(sku, 0, None, product_type=(mpt or _pt))
                        except Exception:
                            r = {}
                        if r.get("ok") and _AZ_W:
                            az_ok += 1
                            await db.amazon_sku_state.update_one(
                                {"sku": sku}, {"$set": {"qty": 0, "updated_at": datetime.now(timezone.utc).isoformat()}})
                        elif r.get("ok"):
                            az_ok += 1   # dry-run: gönderilmedi, sayıldı
                        else:
                            az_err += 1
                sonuc["amazon"] = {"sifirlanan_sku": az_ok, "hatali": az_err, "zaten_0_veya_listede_yok": az_skip,
                                   "aktif_urunle_ortak_korunan": az_korunan, "canli_yazma": bool(_AZ_W)}
            else:
                sonuc["amazon"] = {"atlandi": "Amazon bağlı değil"}
        except Exception as e:
            sonuc["amazon"] = {"hata": f"{type(e).__name__}: {str(e)[:300]}"}
        # Hepsiburada YETİM ilanlar: aktif ürünlerimizin ürettiği merchantSku kümesinde
        # olmayan ve stoğu > 0 olan ilanlar → availableStock 0 (fiyat gönderilmez).
        try:
            from routes.integrations_hepsiburada import hb_yetim_ilanlar, _hb_push_stock_price
            from routes.category_mapping import _get_hb_client
            yr = await hb_yetim_ilanlar()
            ys = yr.get("yetimler") or []
            if ys:
                client, err = await _get_hb_client()
                if err:
                    sonuc["hb_yetim"] = {"hata": err}
                else:
                    items = [{"merchantSku": y["merchantSku"], "availableStock": 0,
                              **({"hepsiburadaSku": y["_hepsiburadaSku"]} if y.get("_hepsiburadaSku") else {})}
                             for y in ys]
                    r = await _hb_push_stock_price(client, items, do_price=False, do_stock=True)
                    sonuc["hb_yetim"] = {"sifirlanan": len(items), "stock_upload_id": (r or {}).get("stock_upload_id"),
                                         "hatalar": (r or {}).get("errors") or [],
                                         "ornek": [y["merchantSku"] for y in ys[:20]]}
            else:
                sonuc["hb_yetim"] = {"sifirlanan": 0, "hb_ilan": yr.get("hb_ilan"), "hata": yr.get("hata")}
        except Exception as e:
            sonuc["hb_yetim"] = {"hata": f"{type(e).__name__}: {str(e)[:300]}"}
        await db.settings.update_one({"id": "passive_zero_sweep_last"},
                                     {"$set": {"id": "passive_zero_sweep_last", **sonuc}}, upsert=True)
        logger.warning(f"[cron] Pasif/silinmiş ürün pazaryeri sıfırlama: {sonuc}")
    except Exception as e:
        logger.error(f"[cron] Pasif/silinmiş sıfırlama hatası: {e}")


async def _run_trendyol_orphan_zero():
    """Trendyol'da olup sistemde olmayan/çöpteki ürünlerin Trendyol stoğunu 0'a çeker (günlük,
    çifte doğrulama + toplu sıfırlama emniyeti). Sonuç settings.trendyol_orphan_zero_last → /health."""
    try:
        from routes.deps import db
        from routes.integrations_trendyol import _trendyol_orphan_zero_core
        res = await _trendyol_orphan_zero_core(dry_run=False)
        await db.settings.update_one({"id": "trendyol_orphan_zero_last"},
                                     {"$set": {"id": "trendyol_orphan_zero_last", "trigger": "scheduler", **res}}, upsert=True)
        logger.warning(f"[cron] Trendyol hayalet sıfırlama: tarandı={res.get('scanned')} onaylı={res.get('confirmed')} gönderildi={res.get('pushed')} emniyet={res.get('guard_tripped')}")
    except Exception as e:
        logger.error(f"[cron] Trendyol hayalet sıfırlama hatası: {e}")


async def _run_trendyol_claims_deep_backfill_once():
    """TEK SEFERLİK derin claims backfill — geçmişteki TÜM Trendyol iadelerini (3 yıl)
    çeker; eski onaylı/reddedilen claim'ler de sekme sayılarına yansısın. Ayrı flag
    (trendyol_claims_deep_backfill) ile YALNIZCA BİR KEZ çalışır; sonra no-op."""
    try:
        from routes.deps import db
        flag = await db.settings.find_one({"id": "trendyol_claims_deep_backfill"}, {"_id": 0})
        if flag and flag.get("done"):
            return
        from routes.integrations import get_trendyol_config, _sync_trendyol_claims_core
        cfg = await get_trendyol_config()
        if not cfg.get("is_active"):
            return
        await db.settings.update_one(
            {"id": "trendyol_claims_deep_backfill"},
            {"$set": {"id": "trendyol_claims_deep_backfill", "running": True,
                      "started_at": datetime.now(timezone.utc).isoformat()}},
            upsert=True,
        )
        res = await _sync_trendyol_claims_core(days_back=1095)
        await db.settings.update_one(
            {"id": "trendyol_claims_deep_backfill"},
            {"$set": {"id": "trendyol_claims_deep_backfill", "done": True, "running": False,
                      "total_synced": res.get("total_synced", 0),
                      "finished_at": datetime.now(timezone.utc).isoformat()}},
            upsert=True,
        )
        logger.info(f"[scheduler] Trendyol TEK SEFERLİK claims derin backfill tamamlandı: "
                    f"{res.get('total_synced', 0)} kayıt")
    except Exception as e:
        logger.error(f"[cron] Trendyol claims derin backfill hatası: {e}")


async def _run_report_ledger():
    try:
        from routes.report_ledger import run_ledger_and_reconcile
        res = await run_ledger_and_reconcile(full=False)
        logger.info(f"[scheduler] rapor havuzu+denetçi: {res}")
    except Exception as e:
        logger.warning(f"[scheduler] rapor havuzu hatası: {e}")


async def _run_report_ledger_full():
    try:
        from routes.report_ledger import run_ledger_and_reconcile
        res = await run_ledger_and_reconcile(full=True)
        logger.info(f"[scheduler] rapor havuzu (tam)+denetçi: {res}")
    except Exception as e:
        logger.warning(f"[scheduler] rapor havuzu (tam) hatası: {e}")


async def _hb_cancel_pass(client, days: int = 2) -> dict:
    """HB iptal listesi → yereldeki AÇIK siparişi iptal et (yalnız TÜM kalemler iptalse; kısmi iptal
    açık-sipariş akışında 'partial_cancelled' ile işlenir). Stok idempotent iade edilir, iptal tarihi
    bir kez damgalanır. Zaten iptal/iade olan siparişe dokunulmaz."""
    import asyncio as _aio
    from routes.integrations import _hb_orders_from_response, map_hepsiburada_order
    from routes.deps import db as _db
    from routes.orders import _restock_order_once, stamp_cancelled_at, _log_order_event
    now_ = datetime.now(timezone.utc)
    seen, cancelled = 0, []
    for d in range(days):
        end = now_ - timedelta(days=d)
        begin = end - timedelta(days=1)
        b, e = begin.strftime("%Y-%m-%d %H:%M"), end.strftime("%Y-%m-%d %H:%M")
        off = 0
        for _pg in range(4):
            resp = await _aio.to_thread(client.get_cancelled_orders, off, 50, b, e)
            groups = _hb_orders_from_response(resp) or []
            for g in groups:
                if not isinstance(g, dict):
                    continue
                seen += 1
                try:
                    data = map_hepsiburada_order(g)
                except Exception:
                    continue
                number = data.get("order_number")
                if not number:
                    continue
                ex = await _db.orders.find_one({"order_number": number, "platform": "hepsiburada"}, {"_id": 0})
                if not ex or ex.get("status") in ("cancelled", "returned", "refunded", "cancel_refunded"):
                    continue
                q_cancel = sum(int(i.get("quantity") or 1) for i in (data.get("items") or []))
                q_order = sum(int(i.get("quantity") or 1) for i in (ex.get("items") or []))
                if q_order and q_cancel < q_order:
                    continue   # kısmi iptal → tam iptal YAPMA
                r = await _db.orders.update_one(
                    {"id": ex["id"], "status": {"$nin": ["cancelled", "returned", "refunded", "cancel_refunded"]}},
                    {"$set": {"status": "cancelled", "cancel_source": "hepsiburada_cron",
                              "marketplace_status": "Cancelled",
                              "updated_at": datetime.now(timezone.utc).isoformat()}})
                if not r.modified_count:
                    continue
                try:
                    await stamp_cancelled_at(order_id=ex["id"], order_number=number, source="hepsiburada_cron")
                except Exception:
                    pass
                try:
                    await _restock_order_once(ex, "order_cancelled")
                except Exception as _re:
                    logger.warning(f"[stok] HB iptal stok iadesi başarısız ({number}): {_re}")
                try:
                    await _log_order_event(ex["id"], "status", "Hepsiburada'da iptal edildi (otomatik)", None,
                                           {"from": ex.get("status"), "to": "cancelled"})
                except Exception:
                    pass
                cancelled.append(number)
            if len(groups) < 50:
                break
            off += 50
    return {"seen": seen, "cancelled": cancelled}


async def _run_hepsiburada_auto_orders_pull():
    """Scheduler tarafından Hepsiburada (OMS) sipariş çekmeyi tetikler.
    OMS kimliği yoksa _get_hb_client hata döndürür → sessizce no-op olur
    (Trendyol akışıyla aynı: tarih aralığı tara, upsert, yeni siparişte stok düş)."""
    from routes.marketplace_hub import log_integration_event
    try:
        import asyncio as _aio
        from datetime import datetime as _dt, timedelta as _td
        from routes.category_mapping import _get_hb_client
        from routes.integrations import (
            _hb_orders_from_response, _hb_enrich_items, map_hepsiburada_order,
            _hb_created_at, _decrement_stock_for_imported_order,
        )
        from routes.deps import db as _db, generate_id

        client, err = await _get_hb_client()
        if err:
            # Kimlik (özellikle OMS) yoksa gürültü yapma — sessiz geç.
            return

        # TARİHSİZ çağrı: HB OMS tüm AÇIK (Open/Unpacked) siparişleri döner — yeni
        # sipariş 2 dk'lık turda buradan yakalanır. (Tarihli sorgu HB'de yalnız
        # 24 saatlik pencere kabul eder ve format hatasında HTTP 400 üretiyordu.)
        try:
            resp = await _aio.to_thread(client.get_orders, None, None, 0, 200)
        except Exception as e:
            await log_integration_event(
                marketplace="hepsiburada", action="order_pull", status="failed",
                direction="inbound", message=f"[cron] HB sipariş çekme hatası: {e}")
            return

        grouped = _hb_orders_from_response(resp)
        # SAĞLIKLI ama SESSİZ tur: HB'ye hatasız ulaşıldı ve yanıt TANINAN biçimde (liste ya da
        # {items|data|orders|content: [...]}) → kalp atışı (Amazon'daki gibi). Açık-sipariş listesi
        # sakin saatlerde BOŞ döner; kalp atışı yoksa tazelik izleyicisi "Entegrasyon durdu" der.
        # Hata (yukarıda return) ya da tanınmayan yanıt biçimi kalp atışı YAZMAZ.
        if isinstance(resp, list) or (isinstance(resp, dict) and any(
                isinstance(resp.get(_k), list) for _k in ("items", "data", "orders", "content"))):
            await note_integration_heartbeat("hepsiburada_orders")
        imported = updated = 0
        for g in grouped:
            try:
                data = await _hb_enrich_items(map_hepsiburada_order(g))
                number = data["order_number"]
                existing = await _db.orders.find_one({"order_number": number, "platform": "hepsiburada"})
                if existing:
                    _update = dict(data)
                    if (existing.get("status") in ("cancelled", "returned", "refunded")
                            and data.get("status") not in ("cancelled", "returned", "refunded")):
                        _update.pop("status", None)
                    # Açık-sipariş listesi kargo alanlarını taşımaz — BOŞ gelen kargo alanı
                    # paket ucundan yazılmış mevcut takip no'yu EZMESİN.
                    for _ck in ("cargo_tracking_number", "cargo_provider_name", "cargo_tracking_link"):
                        if not _update.get(_ck):
                            _update.pop(_ck, None)
                    await _db.orders.update_one(
                        {"_id": existing["_id"]},
                        {"$set": _update})
                    updated += 1
                else:
                    data["id"] = generate_id()
                    data["created_at"] = _hb_created_at(data)
                    await _db.orders.insert_one(data)
                    imported += 1
                    # Zaten iptal/iade gelen YENİ sipariş için stok düşülmez (hiç rezerve edilmemişti).
                    if data.get("status") not in ("cancelled", "returned"):
                        await _decrement_stock_for_imported_order(data, "hepsiburada")
            except Exception as e:
                await log_integration_event(
                    marketplace="hepsiburada", action="import_order", status="error",
                    direction="inbound", message=f"[cron] HB sipariş aktarım hatası: {e}")
        if imported or updated:
            await log_integration_event(
                marketplace="hepsiburada", action="order_pull", status="success",
                direction="inbound",
                message=f"[cron] HB otomatik çekim: {imported} yeni, {updated} güncellendi")

        # İPTALLER (canlı olay 2026-10-16: müşteri HB'de iptal etti, panel/rapor "onaylandı" kaldı).
        # Açık-sipariş listesi iptal edilen siparişi yalnız DÜŞÜRÜR — iptal bilgisi hiç gelmiyordu
        # (yalnız elle 'geçmişi çek' iptal ucunu tarıyordu). Her 10 dk'da son 2 günün HB iptal listesi
        # çekilir; siparişin TÜM kalemleri iptal edildiyse sipariş iptal edilir + stok iade edilir.
        global _HB_LAST_CANCEL_SYNC
        try:
            _HB_LAST_CANCEL_SYNC
        except NameError:
            _HB_LAST_CANCEL_SYNC = None
        _nowc = datetime.now(timezone.utc)
        if (_HB_LAST_CANCEL_SYNC is None) or (_nowc - _HB_LAST_CANCEL_SYNC) >= timedelta(minutes=10):
            _HB_LAST_CANCEL_SYNC = _nowc
            try:
                _cres = await _aio.wait_for(_hb_cancel_pass(client), timeout=120)
                if _cres.get("cancelled"):
                    logger.info(f"[scheduler] HB iptal senkron: {_cres}")
            except Exception as _cx:
                logger.warning(f"[scheduler] HB iptal senkron hatası: {_cx}")

        # KARGO TAKİP NO — açık-sipariş listesi kargolananları DÜŞÜRÜR; takip barkodu paket
        # uçlarından gelir. Her 10 dk'da bir (2 dk'lık her turda değil) son 3 günün paketleri
        # taranır ve eşleşen siparişlere yazılır (integrations_hepsiburada.hb_sync_cargo_tracking).
        global _HB_LAST_CARGO_SYNC
        try:
            _HB_LAST_CARGO_SYNC
        except NameError:
            _HB_LAST_CARGO_SYNC = None
        global _HB_LAST_CARGO_DEEP
        try:
            _HB_LAST_CARGO_DEEP
        except NameError:
            _HB_LAST_CARGO_DEEP = None
        _now = datetime.now(timezone.utc)
        if (_HB_LAST_CARGO_SYNC is None) or (_now - _HB_LAST_CARGO_SYNC) >= timedelta(minutes=10):
            _HB_LAST_CARGO_SYNC = _now
            # Günde 1 kez DERİN tarama (30 gün + sipariş-detayı yedeği) — kaçan/eski siparişler için.
            _deep = (_HB_LAST_CARGO_DEEP is None) or (_now - _HB_LAST_CARGO_DEEP) >= timedelta(hours=24)
            try:
                from routes.integrations_hepsiburada import hb_sync_cargo_tracking
                if _deep:
                    _HB_LAST_CARGO_DEEP = _now
                    _cs = await _aio.wait_for(hb_sync_cargo_tracking(client, days=30, detail_fallback_limit=120), timeout=600)
                else:
                    _cs = await _aio.wait_for(hb_sync_cargo_tracking(client, days=3), timeout=150)
                if _cs.get("orders_updated") or _cs.get("detail_updated") or _cs.get("errors"):
                    logger.info(f"[scheduler] HB kargo takip senkron{' (derin)' if _deep else ''}: {_cs}")
            except Exception as _ce:
                logger.warning(f"[scheduler] HB kargo takip senkron hatası: {_ce}")
    except Exception as e:
        logger.exception(f"[scheduler] hepsiburada auto orders pull failed: {e}")


async def _update_existing_amazon_order(_db, existing, new_status, status_raw, o, number):
    """Mevcut Amazon siparişini günceller — YALNIZ durum + pazaryeri meta alanları (kalem/adres
    EZİLMEZ; onlar ilk insert'te PII ile yazıldı). İptal edilmiş sipariş geri açılmaz.
    confirmed/shipped→cancelled geçişinde, sipariş DAHA ÖNCE stok DÜŞÜLMÜŞSE (MFN) idempotent
    stok iadesi yapılır; FBA'da (hiç düşülmedi) iade EDİLMEZ (hayalet stok önlenir)."""
    _prev = existing.get("status")
    _set = {
        "marketplace_status": status_raw,
        "marketplace_last_modified": o.get("LastUpdateDate", "") or "",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if _prev == "cancelled":
        # Terminal iptal önceliklidir — durumu değiştirme, yalnız meta tazele.
        await _db.orders.update_one({"_id": existing["_id"]}, {"$set": _set})
        return False
    _flip_cancel = (new_status == "cancelled" and _prev != "cancelled")
    if new_status and new_status != _prev:
        _set["status"] = new_status
        if new_status == "cancelled":
            _set["cancel_source"] = "amazon"
    await _db.orders.update_one({"_id": existing["_id"]}, {"$set": _set})
    if _flip_cancel:
        try:
            from routes.orders import stamp_cancelled_at
            await stamp_cancelled_at(order_id=existing.get("id"), order_number=number,
                                     source="amazon_cron")
        except Exception:
            pass
    if _flip_cancel and existing.get("id"):
        try:
            from routes.orders import _stock_delta_for_order, _RESTORE_MOVE_TYPES
            _already = await _db.stock_movements.find_one(
                {"$or": [{"order_id": existing.get("id")}, {"order_number": number}],
                 "type": {"$in": _RESTORE_MOVE_TYPES}}, {"_id": 1})
            # Yalnız stok DÜŞÜLMÜŞSE (MFN, order_imported hareketi var) geri ekle.
            _decremented = await _db.stock_movements.find_one(
                {"order_id": existing.get("id"), "type": "order_imported"}, {"_id": 1})
            if not _already and _decremented:
                _moves = await _stock_delta_for_order(existing, +1)
                await _db.stock_movements.insert_one({
                    "id": str(uuid.uuid4()), "type": "order_cancelled",
                    "order_id": existing.get("id"), "order_number": number,
                    "items": _moves, "source": "amazon_auto_pull_cancel",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                })
        except Exception as _re:
            logger.error(f"[cron] Amazon iptal restock {number}: {_re}")
    return _flip_cancel


async def _run_amazon_auto_orders_pull(lookback_days: int = 7):
    """Scheduler / manuel tetik — Amazon SP-API siparişlerini panele (db.orders) çeker.
    Trendyol/Hepsiburada akışıyla AYNI: son `lookback_days` gün LastUpdatedAfter penceresini
    tara, YENİ siparişi (PII + kalemlerle) insert et, mevcut siparişte iptal/kargo durumunu
    yansıt. YENİ MFN siparişte stok düşülür; FBA'da (Amazon deposu) düşülmez.
    Amazon yapılandırılmamışsa sessizce no-op. Döner: {imported, updated, skipped, errors}."""
    import asyncio as _aio
    from datetime import datetime as _dt, timedelta as _td
    from routes.marketplace_hub import log_integration_event
    summary = {"imported": 0, "updated": 0, "skipped": 0, "errors": 0}
    try:
        from routes.amazon_spapi import (
            _get_config, get_valid_access_token, _spapi_get,
            map_amazon_order, _fetch_amazon_order_items, _fetch_amazon_order_full,
            _amz_status_of, RESTRICTED_ALLOWED, _amazon_enrich_items,
        )
        cfg = await _get_config()
        if not cfg or not cfg.get("refresh_token_enc"):
            return summary  # bağlı değil → sessiz
        from routes.deps import db as _db, generate_id
        from routes.integrations import _decrement_stock_for_imported_order

        _, _, marketplace_id = await get_valid_access_token()
        updated_after = (_dt.now(timezone.utc) - _td(days=max(1, int(lookback_days)))
                         ).strftime("%Y-%m-%dT%H:%M:%SZ")
        # LastUpdatedAfter → hem YENİ hem DURUMU DEĞİŞEN (iptal/kargo) siparişleri getirir.
        orders_raw, next_token, _pages, _fetch_ok = [], None, 0, True
        while _pages < 30:
            if next_token:
                params = {"MarketplaceIds": marketplace_id, "NextToken": next_token}
            else:
                params = {"MarketplaceIds": marketplace_id, "LastUpdatedAfter": updated_after}
            res = await _spapi_get("/orders/v0/orders", params)
            if not res["ok"]:
                await log_integration_event(
                    marketplace="amazon", action="order_pull", status="failed",
                    direction="inbound",
                    message=f"[cron] Amazon getOrders HTTP {res['status']}: {res.get('data')}")
                _fetch_ok = False
                break
            payload = res["data"].get("payload") or {}
            orders_raw.extend(payload.get("Orders") or [])
            next_token = payload.get("NextToken")
            _pages += 1
            if not next_token:
                break
            await _aio.sleep(0.7)  # getOrders rate limiti (düşük) — sayfalar arası nefes

        for o in orders_raw:
            try:
                oid = str(o.get("AmazonOrderId") or "")
                if not oid:
                    continue
                status_raw = o.get("OrderStatus") or ""
                new_status = _amz_status_of(status_raw)
                existing = await _db.orders.find_one({"order_number": oid, "platform": "amazon"})
                if existing:
                    # (a) PII backfill: adres/isim eksik geldiyse (Restricted sonradan açıldı ya da
                    #     geçici hata) SINIRLI denemeyle tamamla. Placeholder ("Amazon Müşterisi")
                    #     eski siparişler de dahil — needs_pii_refresh bayrağı olmasa bile.
                    # (b) Ürün eşleştirme: kalemlerde resim yoksa TEK SEFER Atilla ürünüyle eşle.
                    _ship_fn = (existing.get("shipping_address") or {}).get("first_name")
                    _need_pii = (RESTRICTED_ALLOWED and int(existing.get("pii_attempts") or 0) < 5 and
                                 (existing.get("needs_pii_refresh") or _ship_fn in ("", "Amazon", None)))
                    # H3: needs_items_refresh (ilk import'ta kalem çekilememiş) veya resimsiz
                    # kalem → yeniden çek. Boş kalem listesinde any(...) tetiklenmediğinden
                    # needs_items_refresh bayrağı açıkça kontrol edilir.
                    _need_enrich = (not existing.get("items_enriched") and
                                    (existing.get("needs_items_refresh") or
                                     any(not (it or {}).get("image") for it in (existing.get("items") or []))))
                    if _need_pii or _need_enrich:
                        _upd, _items_src = {}, existing.get("items")
                        # H3: kalemler ilk turda çekilememişse şimdi tazele.
                        if existing.get("needs_items_refresh") or not _items_src:
                            _fetched = await _fetch_amazon_order_items(oid)
                            if _fetched:
                                _items_src = _fetched
                                _upd["needs_items_refresh"] = False
                        if _need_pii:
                            _full = await _fetch_amazon_order_full(oid)
                            if (_full or {}).get("ShippingAddress"):
                                _fresh = map_amazon_order(_full, await _fetch_amazon_order_items(oid))
                                _upd["shipping_address"] = _fresh["shipping_address"]
                                _upd["billing_address"] = _fresh["billing_address"]
                                _upd["needs_pii_refresh"] = False
                                _items_src = _fresh["items"] or existing.get("items")
                            else:
                                await _db.orders.update_one({"_id": existing["_id"]}, {"$inc": {"pii_attempts": 1}})
                        if _need_enrich or _need_pii:
                            _tmp = await _amazon_enrich_items({"items": _items_src or []})
                            _upd["items"] = _tmp["items"]
                            # H3: kalem hâlâ boşsa enriched sayma → sonraki tur tekrar dener.
                            _upd["items_enriched"] = bool(_tmp.get("items"))
                            if not _tmp.get("items"):
                                _upd["needs_items_refresh"] = True
                        if _upd:
                            await _db.orders.update_one({"_id": existing["_id"]}, {"$set": _upd})
                        # Geçmişe dönük stok düşümü: ilk import'ta eşleşme YOKTU → stok düşmemişti.
                        # Şimdi eşleşen kalem varsa (MFN + iptal/iade değil) TEK SEFER güvenle düş.
                        # Çift-düşüm koruması: order_imported hareketi DOLU (moves var) ise atlanır;
                        # BOŞ ise (hiç düşmemiş) silinip gerçek barkodlarla yeniden düşülür.
                        _new_items = _upd.get("items")
                        if (_new_items and existing.get("status") not in ("cancelled", "returned")
                                and existing.get("fulfillment_channel") != "AFN"
                                and any((it or {}).get("matched") for it in _new_items)):
                            try:
                                _mv = await _db.stock_movements.find_one(
                                    {"order_id": existing.get("id"), "type": "order_imported"},
                                    {"_id": 1, "moves": 1})
                                if not (_mv and _mv.get("moves")):
                                    if _mv:
                                        await _db.stock_movements.delete_one({"_id": _mv["_id"]})
                                    _ord_for_stock = {**existing, "items": _new_items,
                                                      "id": existing.get("id"), "order_number": oid}
                                    await _decrement_stock_for_imported_order(_ord_for_stock, "amazon")
                            except Exception as _se:
                                logger.error(f"[cron] Amazon retro stok düşüm {oid}: {_se}")
                        await _aio.sleep(0.3)
                    await _update_existing_amazon_order(_db, existing, new_status, status_raw, o, oid)
                    summary["updated"] += 1
                    continue
                # YENİ sipariş → PII (RDT) + kalemleri çek. PII gelmese bile sipariş DAİMA düşer
                # (kabuk); adres sonraki turlarda needs_pii_refresh ile tamamlanır → hiç kaybolmaz.
                full = await _fetch_amazon_order_full(oid)
                items = await _fetch_amazon_order_items(oid)
                data = map_amazon_order(full or o, items)
                # Ürün eşleştirme: Amazon SellerSKU → Atilla ürünü (görsel + gerçek product_id +
                # barkod + beden/renk). Trendyol/HB ile AYNI eşleyici; eşleşen kalemde resim gelir.
                data = await _amazon_enrich_items(data)
                # DENETİM H3: getOrderItems rate-limit/timeout ile BOŞ dönerse eskiden
                # items_enriched=True yazılıp bir daha çekilmiyordu → kalıcı kabuk sipariş,
                # stok HİÇ düşmüyordu (oversell + eksik COGS). Boşsa enriched sayma, refresh iste.
                data["items_enriched"] = bool(items)
                if not items:
                    data["needs_items_refresh"] = True
                data["id"] = generate_id()
                # created_at = GERÇEK sipariş tarihi (PurchaseDate) — aylık pazaryeri sayımı doğru.
                data["created_at"] = data.get("marketplace_order_date") or datetime.now(timezone.utc).isoformat()
                if RESTRICTED_ALLOWED and not (full or {}).get("ShippingAddress"):
                    data["needs_pii_refresh"] = True
                    data["pii_attempts"] = 1
                await _db.orders.insert_one(data)
                summary["imported"] += 1
                # Stok: yalnız MFN (satıcı kargolar) + iptal/iade DEĞİLSE düş. FBA stoğu Amazon'da.
                if data.get("status") not in ("cancelled", "returned") and data.get("fulfillment_channel") != "AFN":
                    await _decrement_stock_for_imported_order(data, "amazon")
                await _aio.sleep(0.4)  # per-sipariş getOrder/RDT/getOrderItems rate limiti
            except Exception as _ex:
                summary["errors"] += 1
                logger.error(f"[cron] Amazon order import {o.get('AmazonOrderId')}: {_ex}")

        if _fetch_ok:
            await note_integration_heartbeat("amazon_orders")
        if summary["imported"] or summary["updated"] or summary["skipped"]:
            await log_integration_event(
                marketplace="amazon", action="order_pull", status="success",
                direction="inbound",
                message=(f"[cron] Amazon: +{summary['imported']} yeni / {summary['updated']} güncellendi"
                         f" / {summary['skipped']} ertelendi / {summary['errors']} hata"))
        return summary
    except Exception as e:
        try:
            await log_integration_event(
                marketplace="amazon", action="order_pull", status="failed",
                direction="inbound", message=f"[cron] Amazon sipariş çekme hatası: {e}")
        except Exception:
            pass
        logger.exception(f"[scheduler] amazon auto orders pull failed: {e}")
        return summary


async def _run_amazon_auto_stock_sync(barcodes=None, stock_codes=None, force=False, manual=False,
                                      mapped_only=False):
    """Scheduler / manuel — Amazon stok+fiyat CANLI push (Trendyol/HB ile simetrik).
    Amazon SellerSKU = Atilla variant.stock_code (yoksa barcode). Otomatik turda YALNIZ stoğu/
    fiyatı DEĞİŞEN varyantı yollar (amazon_sku_state değişiklik tespiti). Manuel tetik `barcodes`/
    `stock_codes` filtresi verirse yalnız o varyantları, `force=True` ile değişiklik tespitini
    ATLAYARAK yollar. AMAZON_ALLOW_WRITE=0 iken dry-run. Döner: özet dict."""
    import asyncio as _aio
    from routes.marketplace_hub import log_integration_event
    summary = {"mode": "live", "pushed": 0, "skipped": 0, "failed": 0, "deferred": 0,
               "quarantined": 0, "remaining": 0,
               "candidates": 0, "dry_run": False}
    try:
        from routes.amazon_spapi import (_get_config, _amazon_push_stock_price, ALLOW_WRITE,
                                         _amazon_markup, _amazon_price_of, _amazon_seller_sku,
                                         _resolve_amazon_pt_and_defaults)
        from routes.amazon_helpers import safe_amazon_failure, amazon_retry_policy
        _pt_cache = {}  # stok-sync #3: ürün başına gerçek productType (sabit "PRODUCT" değil)
        cfg = await _get_config()
        if not cfg or not cfg.get("refresh_token_enc") or not cfg.get("selling_partner_id"):
            summary["error"] = "Amazon bağlı değil (OAuth/refresh token yok)."
            return summary
        from routes.deps import db as _db
        # PERFORMANS: her dakika TÜM aktif ürünler tam belge (görseller, açıklamalar, Ticimax
        # blob'ları) olarak çekiliyordu → sürekli DB/CPU yükü. Stok/fiyat push için gereksiz ağır
        # alanlar HARİÇ tutulur (kalan alanlar fiyat/SKU/varyant/kategori/özellik mantığına yeter).
        products = await _db.products.find({"is_active": True, "is_deleted": {"$ne": True}}, _STOCK_SYNC_PROJ).to_list(length=None)
        markup = await _amazon_markup()  # marjlı fiyat (listeleme ile AYNI)

        _bset = {str(x).strip() for x in (barcodes or []) if str(x).strip()}
        _sset = {str(x).strip() for x in (stock_codes or []) if str(x).strip()}
        _filtered = bool(_bset or _sset)

        def _sku_of(v, p):
            return _amazon_seller_sku(v, p)  # listeleme ile AYNI SellerSKU (stok_kodu-renk-beden)

        def _in_target(v, p):
            if not _filtered:
                return True
            # Panelde kullanıcı çoğunlukla varyantın ham stock_code'u yerine aile stok
            # kodunu veya Amazon SellerSKU'sunu bilir. Dördünü de kabul et; böylece
            # hedefli gönderim yanlışlıkla boş kalıp operatörü "tüm katalog" akışına
            # itmez.
            try:
                from routes.integrations_common import _resolve_stock_code as _resolve_sc
                family_code = str(_resolve_sc(p) or "").strip()
            except Exception:
                family_code = str(p.get("stock_code") or p.get("sku") or "").strip()
            seller_sku = str(_sku_of(v, p) or "").strip()
            return (str(v.get("barcode") or "").strip() in _bset
                    or str(v.get("stock_code") or "").strip() in _sset
                    or family_code in _sset
                    or seller_sku in _sset)

        # HEDEF LİSTESİ: (1) bizim ürettiğimiz SKU'lar (stokkodu-renk-beden), (2) Amazon'da bu
        # platform dışında açılmış ve KATALOG EŞLEŞTİRMESİ ile eşlenen harici SKU'lar
        # (db.amazon_sku_map, matched=True) — aynı varyantın stok/fiyatı oraya da gider.
        targets = []  # (sku, product, variant, product_type|None)
        _seen_sku = set()
        if not mapped_only:
            for p in products:
                for v in (p.get("variants") or []):
                    sk = _sku_of(v, p)
                    if sk and _in_target(v, p) and sk not in _seen_sku:
                        _seen_sku.add(sk)
                        targets.append((sk, p, v, None))
        try:
            _pmap = {p.get("id"): p for p in products}
            async for m in _db.amazon_sku_map.find({"matched": True}, {"_id": 0, "sku": 1, "product_id": 1,
                                                                        "variant_barcode": 1, "variant_size": 1,
                                                                        "product_type": 1}):
                sk = m.get("sku")
                p = _pmap.get(m.get("product_id"))
                if not sk or not p or sk in _seen_sku:
                    continue
                v = None
                for vv in (p.get("variants") or []):
                    if (m.get("variant_barcode") and str(vv.get("barcode") or "") == m["variant_barcode"]) or \
                       (not m.get("variant_barcode") and str(vv.get("size") or "") == (m.get("variant_size") or "")):
                        v = vv
                        break
                if v is None or not _in_target(v, p):
                    continue
                _seen_sku.add(sk)
                targets.append((sk, p, v, m.get("product_type") or None))
        except Exception as _me:
            logger.warning(f"[amazon] eşleşmiş harici SKU'lar okunamadı: {_me}")
        summary["candidates"] = len(targets)

        if not ALLOW_WRITE:
            summary["dry_run"] = True
            summary["mode"] = "dry_run"
            summary["message"] = (f"DRY-RUN: {summary['candidates']} SKU gönderilmeye hazır. "
                                  f"Canlı göndermek için AMAZON_ALLOW_WRITE=1.")
            await log_integration_event(
                marketplace="amazon", action="stock_sync", status="success", direction="outbound",
                message=f"[amazon] stok/fiyat DRY-RUN: {summary['candidates']} SKU hazır.")
            return summary

        pushed = skipped = failed = deferred = quarantined = remaining = 0
        # TÜM AKTİF ÜRÜNLERİ GÖNDER (kullanıcı kararı): stock.amazon_full_sync açıkken
        # "değişmedi → atla" kısa devresi DEVRE DIŞI kalır; katalog sürekli yeniden
        # gönderilerek Amazon'daki stok sapması kendi kendine onarılır.
        # Amazon SP-API hız sınırı yüzünden tek turda hepsini göndermek mümkün değil:
        # her tur stock.amazon_sync_batch kadar SKU gider ve KALDIĞI YERDEN devam eder
        # (imleç settings.amazon_sync_cursor). Böylece katalog birkaç turda tam taranır.
        _full_sync = False
        _CAP_RULE = 200
        try:
            from business_rules import get_rule as _gr
            _full_sync = bool(await _gr(_db, "stock.amazon_full_sync", True))
            _CAP_RULE = max(10, int(await _gr(_db, "stock.amazon_sync_batch", 120)))
        except Exception:
            _full_sync, _CAP_RULE = True, 120
        _CAP = 1000000 if (manual or _filtered) else _CAP_RULE
        _force = force or _filtered
        # İmleç: otomatik turda listeyi kaldığı yerden döndür (round-robin) — aksi halde
        # hep baştaki SKU'lar gönderilir, kuyruktakilere hiç sıra gelmezdi.
        if _full_sync and not (manual or _filtered) and targets:
            try:
                _cdoc = await _db.settings.find_one({"id": "amazon_sync_cursor"}, {"_id": 0, "n": 1})
                _start = int((_cdoc or {}).get("n") or 0) % len(targets)
            except Exception:
                _start = 0
            targets = targets[_start:] + targets[:_start]
            summary["cursor_start"] = _start
        for (sku, p, v, _map_pt) in targets:
            pr = _amazon_price_of(p, markup)  # marjlı satış fiyatı
            if True:
                try:
                    qty = int(v.get("stock") or 0)
                except Exception:
                    qty = 0
                st = await _db.amazon_sku_state.find_one({"sku": sku}, {"_id": 0})
                if not _force:
                    # Basarisiz SKU'yu her dakikada sonsuza dek dovme: kademeli backoff,
                    # 8. ardışık hatadan sonra 24 saat karantina. Manuel force bunu atlayabilir.
                    _now_s = datetime.now(timezone.utc).isoformat()
                    if st and str(st.get("next_retry_at") or "") > _now_s:
                        deferred += 1
                        if st.get("quarantined"):
                            quarantined += 1
                        continue
                    # stock.amazon_full_sync AÇIKKEN bu kısa devre uygulanmaz: değişmemiş
                    # SKU da yeniden gönderilir (Amazon'daki sapmayı onarmanın tek yolu).
                    if (not _full_sync) and st and st.get("qty") == qty and st.get("price") == pr:
                        skipped += 1
                        continue
                if pushed >= _CAP:
                    remaining += 1
                    continue
                _push_error = None
                try:
                    _pid = p.get("id")
                    if _pid not in _pt_cache:
                        try:
                            _pt, _, _ = await _resolve_amazon_pt_and_defaults(p)
                            _pt_cache[_pid] = _pt or "PRODUCT"
                        except Exception:
                            _pt_cache[_pid] = "PRODUCT"
                    # Harici (eşleştirilmiş) SKU'da Amazon'un kendi productType'ı öncelikli
                    res = await _amazon_push_stock_price(sku, qty, pr, product_type=(_map_pt or _pt_cache[_pid]))
                except Exception as _pe:
                    res = {}
                    _push_error = _pe
                if res.get("ok"):
                    await _db.amazon_sku_state.update_one(
                        {"sku": sku},
                        {"$set": {"sku": sku, "qty": qty, "price": pr,
                                  "updated_at": datetime.now(timezone.utc).isoformat(),
                                  "failure_count": 0, "quarantined": False},
                         "$unset": {"last_error_code": "", "last_error_message": "",
                                    "last_error_http": "", "last_failed_at": "", "next_retry_at": ""}},
                        upsert=True)
                    pushed += 1
                else:
                    failed += 1
                    _detail = safe_amazon_failure(res, _push_error)
                    _failure_count = int((st or {}).get("failure_count") or 0) + 1
                    _delay, _is_quarantined = amazon_retry_policy(_failure_count)
                    _failed_at = datetime.now(timezone.utc)
                    _next_retry = (_failed_at + timedelta(seconds=_delay)).isoformat()
                    await _db.amazon_sku_state.update_one(
                        {"sku": sku},
                        {"$set": {"sku": sku, "failure_count": _failure_count,
                                  "last_error_code": _detail["code"],
                                  "last_error_message": _detail["message"],
                                  "last_error_http": _detail.get("http"),
                                  "last_failed_at": _failed_at.isoformat(),
                                  "next_retry_at": _next_retry,
                                  "quarantined": _is_quarantined}},
                        upsert=True)
                    # Log hacmini sinirla: ilk iki hata, 4. hata ve karantinaya giris.
                    if _failure_count in (1, 2, 4, 8):
                        await log_integration_event(
                            marketplace="amazon", action="stock_sync", status="failed",
                            direction="outbound", ref_id=sku,
                            message=(f"Amazon stok SKU hatasi [{_detail['code']}]: "
                                     f"{_detail['message']} (deneme {_failure_count}, "
                                     f"{'karantina' if _is_quarantined else f'{_delay}sn backoff'})"))
                    logger.warning("[amazon] stok push basarisiz sku=%s code=%s attempt=%s",
                                   sku, _detail["code"], _failure_count)
                await _aio.sleep(0.25)  # Listings PATCH 5 rps — güvenli aralık
        # İmleci ilerlet: bir sonraki tur bu turda gönderilenlerin ARDINDAN devam etsin.
        # Böylece katalog birkaç turda baştan sona taranır, hep aynı SKU'lar gitmez.
        if _full_sync and not (manual or _filtered) and targets and pushed:
            try:
                _n = (int(summary.get("cursor_start") or 0) + pushed) % len(targets)
                await _db.settings.update_one(
                    {"id": "amazon_sync_cursor"},
                    {"$set": {"id": "amazon_sync_cursor", "n": _n,
                              "at": datetime.now(timezone.utc).isoformat()}}, upsert=True)
            except Exception:
                pass
        summary.update({"pushed": pushed, "skipped": skipped, "failed": failed,
                        "deferred": deferred, "quarantined": quarantined, "remaining": remaining,
                        "full_sync": _full_sync, "batch": _CAP if _CAP < 1000000 else None})
        summary["message"] = (f"{pushed} gönderildi / {skipped} değişmedi / {failed} hata"
                              + (f" / {deferred} backoff'ta ({quarantined} karantina)" if deferred else "")
                              + (f" / {remaining} sonraki tura" if remaining else ""))
        if pushed or not failed:
            await note_integration_heartbeat("amazon_stock")
        if pushed or failed or remaining:
            await log_integration_event(
                marketplace="amazon", action="stock_sync",
                status="success" if not failed else "partial", direction="outbound",
                message=f"[amazon] stok/fiyat CANLI: {summary['message']}")
        return summary
    except Exception as e:
        logger.exception(f"[scheduler] amazon stock sync failed: {e}")
        summary["error"] = str(e)
        return summary


async def _run_hepsiburada_auto_stock_sync():
    """Scheduler — Hepsiburada stok/fiyat senkronu (Trendyol akışıyla simetrik).
    Tüm aktif ürünlerin güncel stok+fiyatını HB listing'ine gönderir.
    HB kimliği yoksa _get_hb_client hata döndürür → sessizce no-op."""
    from routes.deps import db
    from routes.marketplace_hub import log_integration_event
    try:
        from routes.category_mapping import _get_hb_client
        from routes.integrations import (
            _hb_markup, _hb_price_source, _hb_sku_source,
            _hb_listing_items_from_product, _hb_push_stock_price,
        )
        client, err = await _get_hb_client()
        if err:
            return  # kimlik yoksa sessiz geç
        products = await db.products.find({"is_active": True, "is_deleted": {"$ne": True}}, _STOCK_SYNC_PROJ).to_list(length=None)
        markup = await _hb_markup()
        price_source = await _hb_price_source()
        # ⚠️ KRİTİK: merchantSku kaynağı import ile AYNI olmalı. Aksi halde stok/fiyat
        # gönderimi import'taki SKU ile eşleşmez → HB listing'i bulamaz → fiyat/stok
        # sessizce hiç uygulanmaz ("aktardım ama fiyat/stok gitmedi"). sku_source'u oku.
        sku_source = await _hb_sku_source()
        items = []
        for prod in products:
            items.extend(_hb_listing_items_from_product(prod, markup, price_source, sku_source))
        if not items:
            return
        res = await _hb_push_stock_price(client, items, True, True)
        errs = res.get("errors") if isinstance(res, dict) else None
        await log_integration_event(
            marketplace="hepsiburada", action="stock_update",
            status=("success" if not errs else "failed"), direction="outbound",
            message=f"[cron] HB stok/fiyat senkronu: {len(items)} kalem"
                    + (f" — {'; '.join(errs)}" if errs else ""))
    except Exception as e:
        try:
            await log_integration_event(
                marketplace="hepsiburada", action="stock_update", status="failed",
                direction="outbound", message=f"[cron] HB stok senkron hatasi: {e}")
        except Exception:
            pass


async def _run_hb_invoice_autoheal():
    """KALICI ÇÖZÜM: HB'ye yüklenmemiş faturaları periyodik yeniden gönder (kendi-kendini
    iyileştirme). Anlık gönderim başarısız olsa bile bu döngü kurtarır. İdempotent, hafif."""
    try:
        from routes.orders import autoheal_hb_invoices
        # hours=None → TÜM eski yüklenmemiş HB faturaları taranır (uploaded!=True olduğu için
        # yüklenenler her turda kümeden düşer; 'eskileri yükle' kalıcı kapanır).
        res = await autoheal_hb_invoices(hours=None, limit=500)
        logger.info(f"[scheduler][hb-invoice] tarandı={res.get('checked')} yüklendi={res.get('uploaded')} "
                    f"{('hata=' + str(res.get('error'))) if res.get('error') else ''}")
    except Exception as e:
        logger.warning(f"[scheduler][hb-invoice] autoheal hatası: {e}")


# Y21: Fire-and-forget senkron task'ları için kilit + referans havuzu.
# Önceden create_task referanssız çağrılıyordu → (a) 2 dk aralıkta >2 dk süren pull ardılıyla
# ÇAKIŞIP çift sipariş insert + çift stok düşümü yapabiliyor, (b) referans tutulmadığı için GC
# task'ı yarıda öldürebiliyordu. Artık aynı iş bitmeden ikincisi başlamaz ve _last_*_sync
# zaman damgası spawn'da değil TAMAMLANINCA yazılır.
_RUNNING_SYNCS: set = set()
_SYNC_TASKS: set = set()


def _spawn_guarded_sync(coro_factory, lock_key: str, account_key: str, stamp_field: str):
    """Kilitli, referanslı arka plan senkron başlatır. Zaten çalışıyorsa atlar."""
    if lock_key in _RUNNING_SYNCS:
        logger.info(f"[scheduler] {lock_key} zaten çalışıyor — bu tur atlandı (çakışma önlendi)")
        return
    _RUNNING_SYNCS.add(lock_key)

    async def _wrapper():
        # Fencing: tick ile görev başlangıcı arasında liderlik el değiştirdiyse dış yazma YAPMA
        # (damga da yazılmaz — asıl lider zamanında çalıştırsın).
        try:
            _lead_ok = await _still_leader()
        except Exception:
            _lead_ok = True
        if not _lead_ok:
            _RUNNING_SYNCS.discard(lock_key)
            return
        try:
            await coro_factory()
        except Exception as _e:
            logger.exception(f"[scheduler] {lock_key} senkron hata: {_e}")
        finally:
            try:
                from routes.deps import db as _db
                from datetime import datetime as _dt, timezone as _tz
                await _db.marketplace_accounts.update_one(
                    {"key": account_key}, {"$set": {stamp_field: _dt.now(_tz.utc).isoformat()}}
                )
            except Exception:
                pass
            _RUNNING_SYNCS.discard(lock_key)

    t = asyncio.create_task(_wrapper())
    _SYNC_TASKS.add(t)
    t.add_done_callback(_SYNC_TASKS.discard)


async def _marketplace_sync_tick():
    """
    Her dk'da bir çalışır; her marketplace_account'un auto_sync ayarlarına
    bakar, periyoda gelmişse ürün push ve sipariş pull tetikler.

    TASARIM:
      - `auto_sync.products_interval_min` dk geçtiyse → ürün push tetikle
      - `auto_sync.orders_interval_min` dk geçtiyse → sipariş pull tetikle
      - `marketplace_accounts.{_last_products_sync, _last_orders_sync}` alanlarına
        son çalışma zamanı yazılır (next check için kıyas).
      - Gerçek çağrı internal HTTP olarak yapılmaz; direkt servis fonksiyonu
        çağrılır. Şu an her pazaryeri için ortak "dummy push" ile log düşer;
        mevcut integrations.py servisleri ileride buraya bağlanacak.
      - Tüm deneme sonuçları integration_logs'a düşer (ayrı bir marker ile).
    """
    from routes.deps import db
    from routes.marketplace_hub import log_integration_event

    now = datetime.now(timezone.utc)
    try:
        async for acc in db.marketplace_accounts.find({"enabled": True}, {"_id": 0}):
            key = acc.get("key")
            sync = acc.get("auto_sync") or {}

            # --- Ürünler ---------------------------------------------------
            if sync.get("products_enabled"):
                last = acc.get("_last_products_sync")
                try:
                    last_dt = datetime.fromisoformat(last) if last else None
                except Exception:
                    last_dt = None
                interval = max(1, int(sync.get("products_interval_min") or 3))
                due = (not last_dt) or (now - last_dt) >= timedelta(minutes=interval)
                if due:
                    await log_integration_event(
                        marketplace=key, action="product_push", status="queued",
                        direction="outbound",
                        message=f"[cron] Otomatik ürün senkron tetiklendi (her {interval} dk)"
                    )
                    # Trendyol için gerçek push'u arka planda kuyruğa al (Y21: kilitli + damga bitişte)
                    if key == "trendyol":
                        _spawn_guarded_sync(_run_trendyol_auto_products_sync,
                                             f"products:{key}", key, "_last_products_sync")
                    elif key == "hepsiburada":
                        _spawn_guarded_sync(_run_hepsiburada_auto_stock_sync,
                                             f"products:{key}", key, "_last_products_sync")

            # --- Siparişler ------------------------------------------------
            if sync.get("orders_enabled"):
                last = acc.get("_last_orders_sync")
                try:
                    last_dt = datetime.fromisoformat(last) if last else None
                except Exception:
                    last_dt = None
                # Kullanıcı kararı: TÜM pazaryerlerinde sipariş çekme 2 dk.
                interval = max(1, int(sync.get("orders_interval_min") or 2))
                due = (not last_dt) or (now - last_dt) >= timedelta(minutes=interval)
                if due:
                    lookback = int(sync.get("orders_lookback_hours") or 100)
                    await log_integration_event(
                        marketplace=key, action="order_pull", status="queued",
                        direction="inbound",
                        message=f"[cron] Otomatik sipariş çek tetiklendi (her {interval} dk, son {lookback} saat)"
                    )
                    if key == "trendyol":
                        _spawn_guarded_sync(_run_trendyol_auto_orders_pull,
                                             f"orders:{key}", key, "_last_orders_sync")
                    elif key == "hepsiburada":
                        _spawn_guarded_sync(_run_hepsiburada_auto_orders_pull,
                                             f"orders:{key}", key, "_last_orders_sync")
    except Exception as e:
        logger.exception(f"[scheduler] marketplace sync tick failed: {e}")


async def _send_abandoned_cart_reminders():
    """Terkedilmiş sepet e-postası — yalnız e-posta ticari ileti izni olan ÜYELERE, sepet
    12 saat (İşletme Kuralları) önce güncellenmiş ve o saatten beri sipariş yoksa.
    Tüm kurallar abandoned_cart_mail.run içinde (izin / üye / sipariş / tekrar aralığı).

    ESKİ DAVRANIŞ KALDIRILDI: günde bir, izin kontrolü YAPMADAN istemcinin yazdığı her sepet
    e-postasına jenerik mail gidiyordu (İYS/KVKK açısından ticari ileti izni şart)."""
    from routes.deps import db
    try:
        import abandoned_cart_mail
        stats = await abandoned_cart_mail.run(db)
        if stats.get("candidates"):
            logger.info(f"[scheduler] Abandoned cart e-mail: {stats}")
    except Exception as e:
        logger.exception(f"[scheduler] abandoned cart e-mail failed: {e}")


async def _stock_alert_recipients(db) -> list:
    """Stok uyarılarının gideceği adres(ler). Beyaz etiket: firma-özel değer koddan gelmez.

    Öncelik: İşletme Kuralları `stock.alert_email` → firma iletişim e-postası
    (company.contact_email) → admin kullanıcılar. Virgülle birden çok adres yazılabilir.
    """
    try:
        from business_rules import get_rule as _gr
        raw = str(await _gr(db, "stock.alert_email", "") or "").strip()
    except Exception:
        raw = ""
    if not raw:
        try:
            from company import get_company
            raw = str((await get_company(db)).get("contact_email") or "").strip()
        except Exception:
            raw = ""
    if raw:
        return [x.strip() for x in raw.replace(";", ",").split(",") if x.strip()]
    try:
        admins = await db.users.find(
            {"is_admin": True, "is_active": {"$ne": False}}, {"_id": 0, "email": 1}
        ).to_list(50)
        return list({a.get("email") for a in admins if a.get("email")})
    except Exception:
        return []


async def _send_daily_stock_alert(threshold: int = None):
    """Her gün, stoğu `threshold` veya altına düşmüş ürün-varyant kombinasyonlarını
    bulup admin kullanıcılara (is_admin=True) özet e-posta gönderir. Eşik verilmezse
    İşletme Kuralları'ndan okunur (stock.low_stock_alert_threshold, varsayılan 3).

    2026-07-02: notification_service.py'de 'stock_alert' bildirim tipi tanımlıydı
    ('Stok Uyarısı (Admin)') ama hiçbir yerde tetiklenmiyordu. Sorgu mantığı
    routes/bulk_ops.py:GET /stock-alerts ile aynı (kopyalandı, davranış korunuyor).
    """
    from routes.deps import db
    if threshold is None:
        try:
            from business_rules import get_rule as _get_rule
            threshold = int(await _get_rule(db, "stock.low_stock_alert_threshold", 3))
        except Exception:
            threshold = 3
    try:
        from routes.catalog_extras import _send_email_via_resend  # lazy
    except Exception as e:
        logger.warning(f"[scheduler] stock alert mail skip (import): {e}")
        return
    from email_smtp import get_smtp_config, is_configured
    if not is_configured(await get_smtp_config(db)):
        return  # E-posta (SMTP/Zoho) yapılandırılmamış → sessizce atla
    try:
        alerts = []
        async for p in db.products.find(
            {"status": {"$ne": "archived"}},
            {"_id": 0, "id": 1, "name": 1, "stock_code": 1, "variants": 1, "stock": 1},
        ):
            variants = p.get("variants") or []
            if variants:
                for v in variants:
                    s = v.get("stock")
                    if s is not None and s <= threshold:
                        alerts.append({
                            "name": p.get("name", ""),
                            "variant": f"{v.get('size','')} {v.get('color','')}".strip(),
                            "stock_code": v.get("stock_code") or p.get("stock_code"),
                            "stock": s,
                        })
            else:
                s = p.get("stock")
                if s is not None and s <= threshold:
                    alerts.append({
                        "name": p.get("name", ""), "variant": "—",
                        "stock_code": p.get("stock_code"), "stock": s,
                    })
        if not alerts:
            return
        # ALICI (beyaz etiket): İşletme Kuralları'ndaki stock.alert_email → boşsa firma
        # iletişim e-postası (company/tenant ayarı) → o da yoksa admin kullanıcılar.
        # Firma-özel adres KODDA DEĞİL, ayarda durur.
        recipients = await _stock_alert_recipients(db)
        if not recipients:
            return
        rows = "".join(
            f"<tr><td>{a['name']}</td><td>{a['variant']}</td>"
            f"<td>{a['stock_code'] or ''}</td><td>{a['stock']}</td></tr>"
            for a in alerts[:200]  # e-posta boyutu için sınırla
        )
        subject = f"Stok Uyarısı: {len(alerts)} ürün-varyant eşik altında (≤{threshold})"
        html = (
            f"<h2>Düşük Stok Uyarısı</h2>"
            f"<p>{len(alerts)} ürün-varyant kombinasyonu {threshold} veya altında stoğa sahip.</p>"
            f"<table border='1' cellpadding='6' style='border-collapse:collapse'>"
            f"<tr><th>Ürün</th><th>Varyant</th><th>Stok Kodu</th><th>Stok</th></tr>{rows}</table>"
            f"<p style='font-size:12px;color:#888;margin-top:24px'>Bu e-posta otomatik gönderilmiştir.</p>"
        )
        ok, failed, errs = await _send_email_via_resend(recipients, subject, html)
        logger.info(f"[scheduler] Stock alert: {len(alerts)} items, sent={ok} failed={failed} errs={errs[:1]}")
    except Exception as e:
        logger.exception(f"[scheduler] stock alert failed: {e}")




async def _ticimax_sync_stock():
    """Ticimax SelectUrun ile canlı stok senkronu — 2 saatte bir tetiklenir.
    routes/ticimax_stock_sync içindeki gerçek implementasyonu çağırır.
    """
    try:
        import sys, os
        sys.path.insert(0, os.path.dirname(__file__))
        from routes.ticimax_stock_sync import sync_ticimax_stock  # type: ignore
        result = await sync_ticimax_stock(
            max_products=2000, aktif=None, page_size=50,
            current_user={"role": "admin", "id": "scheduler"},
        )
        logger.info(f"[scheduler][ticimax_stock] {result.get('message','done')}")
    except Exception as e:
        logger.exception(f"[scheduler] ticimax_stock_sync failed: {e}")


DEFAULT_PII_RETENTION_DAYS = 30
PII_RETENTION_PLATFORMS = ["amazon"]  # varsayılan kapsam: Amazon SP-API kaynaklı siparişler


# ─────────────────────────────────────────────────────────────────────────────
# KVKK — TCKN/VKN at-rest şifreleme: tek seferlik BACKFILL + GECE GÜVENLİK AĞI
# ─────────────────────────────────────────────────────────────────────────────
PII_BACKFILL_FLAG = "pii_tax_id_backfill_v1"
PII_BACKFILL_BATCH = 200
# (koleksiyon, tür) — tür security.pii._KIND_PATHS anahtarı (bilinen yollar)
_PII_ENC_TARGETS = (("orders", "order"), ("orders_deleted", "order"),
                    ("addresses", "address"), ("vendors", "vendor"))
# Log/olay kayıtları: şifrelenmez, MASKELENİR (teşhis kaydı gerçek kimlik no'ya ihtiyaç duymaz).
_PII_LOG_TARGETS = (
    ("integration_logs", ("details", "payload_in", "payload_out", "payload"),
     {"$or": [{"details.raw": {"$exists": True}}, {"payload_in": {"$ne": None}},
              {"payload_out": {"$ne": None}}, {"payload": {"$ne": None}}]}),
    ("order_events", ("meta",), {"meta": {"$ne": None}}),
)


def _pii_get_path(doc, path: str):
    cur = doc
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _pii_cursor_out(v):
    return str(v) if v is not None else None


def _pii_cursor_in(v):
    if v is None:
        return None
    try:
        from bson import ObjectId
        if isinstance(v, str) and ObjectId.is_valid(v):
            return ObjectId(v)
    except Exception:
        pass
    return v


async def encrypt_existing_tax_ids(nightly: bool = False, batch_size: int = PII_BACKFILL_BATCH) -> dict:
    """KVKK: veritabanındaki DÜZ TCKN/VKN değerlerini şifreler (security/pii.py düzeni).

    • Tek seferlik backfill (nightly=False): settings.{id: pii_tax_id_backfill_v1}.done ise atlanır.
      200'lük partiler, koleksiyon başına _id imleci settings'e yazılır → yarıda kesilirse (deploy,
      liderlik kaybı) KALDIĞI YERDEN sürer. Her değer için şifreli değer ÇÖZÜLÜP orijinale eşit
      olduğu DOĞRULANMADAN düz değer silinmez; güncelleme, düz değer hâlâ aynıysa uygulanır
      (eşzamanlı yazmayı ezmez). Sayımlar integration_logs'a "pii_backfill" olarak yazılır.
    • Gece güvenlik ağı (nightly=True): aynı tarama her gece koşar; atlanmış bir yazma yolunun
      bıraktığı düz değeri bulursa şifreler ve koleksiyon/yol ile UYARI loglar. Ayrıca birincil
      hash anahtarı değiştiyse (PII_HASH_SECRET eklendi/rotasyon) eski hash'leri yeniler; son 2 günün
      entegrasyon/olay loglarındaki ham TCKN/VKN'yi maskeler (backfill'de tüm loglar).
    Kullanıcı verisi DEĞİŞMEZ: yalnız saklama biçimi değişir (aynı değer, şifreli)."""
    from routes.deps import db  # lazy import
    from routes.integrations_common import log_integration_event
    from security import pii

    mode = "nightly" if nightly else "backfill"
    state = await db.settings.find_one({"id": PII_BACKFILL_FLAG}, {"_id": 0}) or {}
    if not nightly and state.get("done"):
        return {"skipped": True, "reason": "already_done", "mode": mode}
    cursors = {} if nightly else dict(state.get("cursors") or {})
    report = {"mode": mode, "encrypted": {}, "masked_values_dropped": {}, "verify_failed": {},
              "rehashed": {}, "logs_scrubbed": {}, "paths": {}}
    aborted = False
    now_iso = datetime.now(timezone.utc).isoformat()

    # 1) Şifreleme (orders / orders_deleted / addresses / vendors)
    for coll_name, kind in _PII_ENC_TARGETS:
        if aborted:
            break
        coll = db[coll_name]
        containers, tops = pii.kind_paths(kind)
        paths = list(pii.iter_plain_paths(kind))
        proj = {"_id": 1, **{c: 1 for c in containers}, **{t: 1 for t in tops}}
        plain_q = {"$or": [{p: {"$exists": True, "$nin": ["", None]}} for p in paths]}
        last = _pii_cursor_in(cursors.get(coll_name))
        n_enc = n_drop = n_fail = 0
        path_counts: dict = {}
        while True:
            q = plain_q if last is None else {"$and": [plain_q, {"_id": {"$gt": last}}]}
            batch = await coll.find(q, proj).sort("_id", 1).limit(batch_size).to_list(batch_size)
            if not batch:
                break
            if not await _still_leader():  # fencing: liderlik kaybedildiyse yazma yapma
                aborted = True
                break
            for d in batch:
                last = d["_id"]
                found = pii.doc_has_plain_tax_ids(d, kind)
                if not found:
                    continue
                filt = {"_id": d["_id"]}
                set_ops, unset_ops = {}, {}
                ok = True
                for p in found:
                    orig = _pii_get_path(d, p)
                    filt[p] = orig              # yalnız düz değer hâlâ aynıysa uygula
                    unset_ops[p] = ""
                    if pii.is_masked(orig):     # ekran artığı (maske) — gerçek veri değil
                        n_drop += 1
                        continue
                    try:
                        sealed = pii.seal_tax_id(orig)
                    except Exception:
                        sealed = {}
                    # DOĞRULA: şifre çözümü == orijinal olmadan düz değer SİLİNMEZ
                    if (not sealed or pii.reveal_tax_id({"v_enc": sealed["enc"]}, "v") != str(orig).strip()):
                        ok = False
                        break
                    set_ops[f"{p}_enc"] = sealed["enc"]
                    set_ops[f"{p}_hash"] = sealed["hash"]
                    set_ops[f"{p}_last2"] = sealed["last2"]
                    path_counts[p] = path_counts.get(p, 0) + 1
                if not ok:
                    n_fail += 1
                    logger.error(f"[pii] {coll_name} kayıt şifrelenemedi/doğrulanamadı — düz değer korunuyor")
                    continue
                upd = {"$unset": unset_ops}
                if set_ops:
                    upd["$set"] = set_ops
                res = await coll.update_one(filt, upd)
                if getattr(res, "modified_count", 0):
                    n_enc += 1
            if not nightly:
                cursors[coll_name] = _pii_cursor_out(last)
                await db.settings.update_one(
                    {"id": PII_BACKFILL_FLAG},
                    {"$set": {f"cursors.{coll_name}": cursors[coll_name], "updated_at": now_iso}},
                    upsert=True)
        report["encrypted"][coll_name] = n_enc
        if n_drop:
            report["masked_values_dropped"][coll_name] = n_drop
        if n_fail:
            report["verify_failed"][coll_name] = n_fail
        if path_counts:
            report["paths"][coll_name] = path_counts
            if nightly:
                # GÜVENLİK AĞI: buraya düz değer düşmemeliydi → atlanmış bir yazma yolu var.
                logger.warning(f"[pii] GECE AĞI: {coll_name} koleksiyonunda düz TCKN/VKN bulundu ve şifrelendi "
                               f"(yollar: {sorted(path_counts)}) — bu yolu yazan kod mühürlemiyor!")

    # 2) Hash anahtarı yenileme (birincil anahtar değiştiyse eski hash'ler; arama zaten tüm anahtarlarla)
    primary = pii.primary_hash_kid()
    if primary and not aborted:
        for coll_name, kind in _PII_ENC_TARGETS:
            coll = db[coll_name]
            paths = list(pii.iter_plain_paths(kind))
            stale = {"$regex": f"^(?!{primary}:)"}
            q = {"$or": [{f"{p}_hash": stale} for p in paths]}
            n_re = 0
            last = None
            while True:
                qq = q if last is None else {"$and": [q, {"_id": {"$gt": last}}]}
                batch = await coll.find(qq).sort("_id", 1).limit(batch_size).to_list(batch_size)
                if not batch:
                    break
                if not await _still_leader():
                    aborted = True
                    break
                for d in batch:
                    last = d["_id"]
                    s_ops = {}
                    for p in paths:
                        h = _pii_get_path(d, f"{p}_hash")
                        if isinstance(h, str) and h and not h.startswith(f"{primary}:"):
                            parent, _, leaf = p.rpartition(".")
                            cont = _pii_get_path(d, parent) if parent else d
                            plain = pii.reveal_tax_id(cont if isinstance(cont, dict) else {}, leaf)
                            if plain:
                                s_ops[f"{p}_hash"] = pii.tax_id_hash(plain)
                    if s_ops:
                        await coll.update_one({"_id": d["_id"]}, {"$set": s_ops})
                        n_re += 1
            if n_re:
                report["rehashed"][coll_name] = n_re

    # 3) Log/olay kayıtlarındaki ham TCKN/VKN → maskele
    if not aborted:
        since = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        for coll_name, fields, base_q in _PII_LOG_TARGETS:
            coll = db[coll_name]
            q = {"$and": [base_q, {"created_at": {"$gte": since}}]} if nightly else base_q
            n_sc = 0
            last = None
            while True:
                qq = q if last is None else {"$and": [q, {"_id": {"$gt": last}}]}
                batch = await coll.find(qq, {"_id": 1, **{f: 1 for f in fields}}).sort("_id", 1) \
                    .limit(batch_size).to_list(batch_size)
                if not batch:
                    break
                if not await _still_leader():
                    aborted = True
                    break
                for d in batch:
                    last = d["_id"]
                    s_ops = {f: pii.scrub_tax_ids(d.get(f)) for f in fields
                             if d.get(f) is not None and pii.log_has_plain_tax_ids(d.get(f))}
                    if s_ops:
                        await coll.update_one({"_id": d["_id"]}, {"$set": s_ops})
                        n_sc += 1
            if n_sc:
                report["logs_scrubbed"][coll_name] = n_sc
                if nightly:
                    logger.warning(f"[pii] GECE AĞI: {coll_name} içinde maskelenmemiş TCKN/VKN bulundu ({n_sc} kayıt)")
            if aborted:
                break

    report["aborted"] = aborted
    total_enc = sum(report["encrypted"].values())
    stragglers = nightly and (total_enc or sum(report["logs_scrubbed"].values()))
    if not nightly and not aborted:
        await db.settings.update_one(
            {"id": PII_BACKFILL_FLAG},
            {"$set": {"done": True, "done_at": datetime.now(timezone.utc).isoformat(),
                      "encrypted": report["encrypted"], "verify_failed": report["verify_failed"]}},
            upsert=True)
    if nightly:
        await db.settings.update_one(
            {"id": PII_BACKFILL_FLAG},
            {"$set": {"last_nightly_at": datetime.now(timezone.utc).isoformat(),
                      "last_nightly_stragglers": bool(stragglers)}}, upsert=True)
    try:
        _msg = (f"TCKN/VKN {'gece ağı' if nightly else 'backfill'}: şifrelenen={report['encrypted']} "
                f"doğrulama_hatası={report['verify_failed']} hash_yenilenen={report['rehashed']} "
                f"log_maskelenen={report['logs_scrubbed']}" + (" (yarıda kaldı, sürecek)" if aborted else ""))
        await log_integration_event(
            "pii_backfill", "nightly_net" if nightly else "encrypt_tax_ids", "collection", "tax_ids",
            ("warning" if (stragglers or report["verify_failed"]) else ("partial" if aborted else "success")),
            _msg, {k: v for k, v in report.items()})
    except Exception as _le:
        logger.warning(f"[pii] backfill log yazılamadı: {_le}")
    logger.info(f"[pii] {mode} bitti: {report}")
    return report


async def encrypt_existing_tax_ids_nightly() -> dict:
    """Gece güvenlik ağı (kaçan düz TCKN/VKN'yi yakalar)."""
    return await encrypt_existing_tax_ids(nightly=True)



# ─────────────────────────────────────────────────────────────────────────────
# KVKK — TELEFON / ADRES / IBAN / TC at-rest şifreleme (security/contact_pii.py):
# tek seferlik BACKFILL + GECE GÜVENLİK AĞI (TCKN/VKN işinin aynısı, ayrı bayrak)
# ─────────────────────────────────────────────────────────────────────────────
CONTACT_BACKFILL_FLAG = "pii_contact_backfill_v1"
# (koleksiyon, tür, ek filtre) — personel (is_admin/is_super_admin) kullanıcı kayıtlarına DOKUNULMAZ
# (personel telefonu MFA'da mfa_phone_enc ile ayrıca şifreli tutulur).
_CONTACT_ENC_TARGETS = (
    ("influencers", "influencer", {}),
    ("influencer_pr", "influencer_pr", {}),
    ("addresses", "address", {}),
    ("users", "user", {"is_admin": {"$ne": True}, "is_super_admin": {"$ne": True}}),
)


def _contact_seal_ops(d: dict, kind: str, path_counts: dict):
    """Bir belge için (filtre, güncelleme, hata?) üretir. Her yol için şifreli değer ÇÖZÜLÜP orijinale
    eşit olduğu DOĞRULANMADAN düz değer silinmez; filtre düz değerin hâlâ aynı olmasını şart koşar
    (eşzamanlı kullanıcı yazımını ezmez)."""
    from security import contact_pii as cp
    filt = {"_id": d["_id"]}
    set_ops, unset_ops = {}, {}
    kinds = dict(cp.iter_plain_query_paths(kind))
    dropped = 0
    for p in cp.plain_paths(d, kind):
        orig = _pii_get_path(d, p)
        k = kinds.get(p)
        if not k:
            continue
        filt[p] = orig
        unset_ops[p] = ""
        if cp.is_masked_for(k, orig):     # ekran artığı (maske) — gerçek veri değil, düşülür
            dropped += 1
            continue
        try:
            sealed = cp.seal_value(k, orig)
        except Exception:
            sealed = {}
        enc = sealed.get("_enc")
        if not enc or cp.open_field({"v_enc": enc}, "v") != str(orig).strip():
            return None, None, True, dropped
        for suf, val in sealed.items():
            set_ops[f"{p}{suf}"] = val
        path_counts[p] = path_counts.get(p, 0) + 1
    if kind == "address" and "address" in unset_ops and set_ops.get("address_enc"):
        set_ops["address_hash"] = cp.address_hash(d.get("address"), d.get("city"), d.get("district"))
    if not unset_ops:
        return None, None, False, dropped
    upd = {"$unset": unset_ops}
    if set_ops:
        upd["$set"] = set_ops
    return filt, upd, False, dropped


async def encrypt_existing_contact_pii(nightly: bool = False, batch_size: int = PII_BACKFILL_BATCH) -> dict:
    """KVKK: influencer / PR kaydı / adres defteri / üye profilindeki DÜZ telefon, adres, IBAN, TC
    değerlerini şifreler.

    • Tek seferlik backfill (nightly=False): settings.{id: pii_contact_backfill_v1}.done ise atlanır;
      200'lük partiler, koleksiyon başına _id imleci settings'e yazılır (yarıda kalırsa SÜRER).
      Doğrulama: şifreli değer çözülüp orijinale eşit değilse düz değer SİLİNMEZ (verify_failed sayılır).
      Güncelleme düz değer hâlâ aynıysa uygulanır (eşzamanlı yazmayı ezmez) → idempotent.
    • Gece güvenlik ağı (nightly=True): aynı tarama her gece; atlanmış bir yazma yolunun bıraktığı düz
      değeri şifreler ve koleksiyon/yol ile UYARI loglar. Birincil hash anahtarı değiştiyse telefon
      hash'lerini (ve adres defteri address_hash'ini) yeniler.
    Kullanıcı verisi DEĞİŞMEZ: yalnız saklama biçimi değişir (aynı değer, şifreli)."""
    from routes.deps import db
    from routes.integrations_common import log_integration_event
    from security import contact_pii as cp

    mode = "nightly" if nightly else "backfill"
    state = await db.settings.find_one({"id": CONTACT_BACKFILL_FLAG}, {"_id": 0}) or {}
    if not nightly and state.get("done"):
        return {"skipped": True, "reason": "already_done", "mode": mode}
    cursors = {} if nightly else dict(state.get("cursors") or {})
    report = {"mode": mode, "encrypted": {}, "masked_values_dropped": {}, "verify_failed": {},
              "rehashed": {}, "paths": {}}
    aborted = False
    now_iso = datetime.now(timezone.utc).isoformat()

    for coll_name, kind, extra_q in _CONTACT_ENC_TARGETS:
        if aborted:
            break
        coll = db[coll_name]
        qpaths = [p for p, _k in cp.iter_plain_query_paths(kind)]
        tops, conts = cp.kind_spec(kind)
        proj = {"_id": 1, **{t: 1 for t in tops}, **{c: 1 for c in conts}}
        if kind == "address":
            proj.update({"city": 1, "district": 1})
        if kind == "user":
            proj.update({"is_admin": 1, "is_super_admin": 1})
        plain_q = {"$or": [{p: {"$exists": True, "$nin": ["", None]}} for p in qpaths]}
        if extra_q:
            plain_q = {"$and": [plain_q, extra_q]}
        last = _pii_cursor_in(cursors.get(coll_name))
        n_enc = n_drop = n_fail = 0
        path_counts: dict = {}
        while True:
            q = plain_q if last is None else {"$and": [plain_q, {"_id": {"$gt": last}}]}
            batch = await coll.find(q, proj).sort("_id", 1).limit(batch_size).to_list(batch_size)
            if not batch:
                break
            if not await _still_leader():  # fencing: liderlik kaybedildiyse yazma yapma
                aborted = True
                break
            for d in batch:
                last = d["_id"]
                if kind == "user" and cp.is_staff_doc(d):
                    continue
                # dict/list değerli "phone"/"adres" (bozuk şekil) yollar plain_paths'te zaten yok
                filt, upd, failed, dropped = _contact_seal_ops(d, kind, path_counts)
                n_drop += dropped
                if failed:
                    n_fail += 1
                    logger.error(f"[pii-contact] {coll_name} kayıt şifrelenemedi/doğrulanamadı — düz değer korunuyor")
                    continue
                if not upd:
                    continue
                res = await coll.update_one(filt, upd)
                if getattr(res, "modified_count", 0):
                    n_enc += 1
            if not nightly:
                cursors[coll_name] = _pii_cursor_out(last)
                await db.settings.update_one(
                    {"id": CONTACT_BACKFILL_FLAG},
                    {"$set": {f"cursors.{coll_name}": cursors[coll_name], "updated_at": now_iso}},
                    upsert=True)
        report["encrypted"][coll_name] = n_enc
        if n_drop:
            report["masked_values_dropped"][coll_name] = n_drop
        if n_fail:
            report["verify_failed"][coll_name] = n_fail
        if path_counts:
            report["paths"][coll_name] = path_counts
            if nightly:
                logger.warning(f"[pii-contact] GECE AĞI: {coll_name} koleksiyonunda düz telefon/adres bulundu ve "
                               f"şifrelendi (yollar: {sorted(path_counts)}) — bu yolu yazan kod mühürlemiyor!")

    # Birincil anahtar değiştiyse telefon hash'leri + adres defteri address_hash yenilenir
    # (arama zaten tüm anahtarlarla yapılır; bu yalnız eski anahtar kaldırılabilsin diye).
    primary = cp.primary_kid()
    if primary and not aborted:
        stale = {"$regex": f"^(?!{primary}:)"}
        for coll_name, kind, extra_q in _CONTACT_ENC_TARGETS:
            coll = db[coll_name]
            hpaths = [p for p, k in cp.iter_plain_query_paths(kind) if k == "phone"]
            ors = [{f"{p}_hash": stale} for p in hpaths]
            if kind == "address":
                ors.append({"address_hash": stale})
            if not ors:
                continue
            q = {"$or": ors}
            n_re = 0
            last = None
            while True:
                qq = q if last is None else {"$and": [q, {"_id": {"$gt": last}}]}
                batch = await coll.find(qq).sort("_id", 1).limit(batch_size).to_list(batch_size)
                if not batch:
                    break
                if not await _still_leader():
                    aborted = True
                    break
                for d in batch:
                    last = d["_id"]
                    s_ops = {}
                    for p in hpaths:
                        h = _pii_get_path(d, f"{p}_hash")
                        if isinstance(h, str) and h and not h.startswith(f"{primary}:"):
                            parent, _, leaf = p.rpartition(".")
                            cont = _pii_get_path(d, parent) if parent else d
                            plain = cp.open_field(cont if isinstance(cont, dict) else {}, leaf)
                            if plain:
                                s_ops[f"{p}_hash"] = cp.phone_hash(plain)
                    ah = d.get("address_hash")
                    if kind == "address" and isinstance(ah, str) and ah and not ah.startswith(f"{primary}:"):
                        line = cp.open_field(d, "address")
                        if line:
                            s_ops["address_hash"] = cp.address_hash(line, d.get("city"), d.get("district"))
                    if s_ops:
                        await coll.update_one({"_id": d["_id"]}, {"$set": s_ops})
                        n_re += 1
                if aborted:
                    break
            if n_re:
                report["rehashed"][coll_name] = n_re
            if aborted:
                break

    report["aborted"] = aborted
    total_enc = sum(report["encrypted"].values())
    stragglers = nightly and total_enc
    if not nightly and not aborted:
        await db.settings.update_one(
            {"id": CONTACT_BACKFILL_FLAG},
            {"$set": {"done": True, "done_at": datetime.now(timezone.utc).isoformat(),
                      "encrypted": report["encrypted"], "verify_failed": report["verify_failed"]}},
            upsert=True)
    if nightly:
        await db.settings.update_one(
            {"id": CONTACT_BACKFILL_FLAG},
            {"$set": {"last_nightly_at": datetime.now(timezone.utc).isoformat(),
                      "last_nightly_stragglers": bool(stragglers)}}, upsert=True)
    try:
        _msg = (f"Telefon/adres {'gece ağı' if nightly else 'backfill'}: şifrelenen={report['encrypted']} "
                f"doğrulama_hatası={report['verify_failed']} hash_yenilenen={report['rehashed']}"
                + (" (yarıda kaldı, sürecek)" if aborted else ""))
        await log_integration_event(
            "pii_backfill", "nightly_contact_net" if nightly else "encrypt_contact_pii", "collection", "contact",
            ("warning" if (stragglers or report["verify_failed"]) else ("partial" if aborted else "success")),
            _msg, {k: v for k, v in report.items()})
    except Exception as _le:
        logger.warning(f"[pii-contact] backfill log yazılamadı: {_le}")
    logger.info(f"[pii-contact] {mode} bitti: {report}")
    return report


async def encrypt_existing_contact_pii_nightly() -> dict:
    """Gece güvenlik ağı (kaçan düz telefon/adres/IBAN/TC'yi yakalar)."""
    return await encrypt_existing_contact_pii(nightly=True)


# ─────────────────────────────────────────────────────────────────────────────
# KVKK — SİPARİŞ İLETİŞİM VERİSİ SAKLAMA SÜRESİ (veri minimizasyonu)
# ─────────────────────────────────────────────────────────────────────────────
ORDER_PII_RETENTION_FLAG = "order_pii_retention_v1"
ORDER_PII_RETENTION_FLOOR_DAYS = 365      # yanlış ayara karşı taban (ters ibraz/itiraz kanıtı en az 1 yıl)
# Kapanmış (teslim edilmiş / iptal / iade sonuçlanmış) siparişler. Açık iade/itiraz süreçleri hariç.
_ORDER_PII_CLOSED_STATUSES = ["delivered", "completed", "cancelled", "cancel_refunded", "refunded",
                              "returned", "partial_refunded", "payment_failed", "failed"]
_ORDER_PII_OPEN_RETURN = ["requested", "pending", "approved", "in_transit", "received", "created"]
_ORDER_PII_CONTAINERS = ("shipping_address", "billing_address", "invoice_address", "delivery_address",
                         "customer", "buyer", "shipmentAddress", "invoiceAddress", "return_request")


def minimize_order_contact_doc(doc: dict) -> dict:
    """Siparişin müşteri iletişim verisini EN AZA indiren $set gövdesi: açık adres satırları / posta kodu
    SİLİNİR, telefon yalnız son 2 hane ('+90 *** *** ** 33'); il/ilçe, ad-soyad, e-posta, fatura VKN/TCKN
    (şifreli — VUK saklama), unvan/vergi dairesi, tutar/kalem/fatura no KORUNUR (muhasebe; e-faturanın
    kendisi entegratörde saklanır). Saf fonksiyon (test edilir)."""
    from security import contact_pii as cp
    out: dict = {}

    def _walk(c: dict, prefix: str, depth: int = 0):
        for k, v in list(c.items()):
            path = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict):
                if depth < 2:
                    _walk(v, path, depth + 1)
                continue
            if isinstance(v, list):
                continue
            if cp.is_phone_key(k) and cp._is_present(v) and not cp.is_masked(v):
                out[path] = cp.mask_phone(v)
            elif cp.is_address_key(k) and cp._is_present(v):
                out[path] = ""
    for cname in _ORDER_PII_CONTAINERS:
        sub = doc.get(cname)
        if isinstance(sub, dict):
            _walk(sub, cname, 1)
    for k, v in doc.items():
        if isinstance(v, (dict, list)):
            continue
        if cp.is_phone_key(k) and cp._is_present(v) and not cp.is_masked(v):
            out[k] = cp.mask_phone(v)
        elif cp.is_address_key(k) and cp._is_present(v):
            out[k] = ""
    return out


async def minimize_old_order_contact_pii(batch_size: int = 200, force_dry_run: bool = False) -> dict:
    """KVKK md.4 (ölçülülük) / md.7 — saklama süresi dolan KAPANMIŞ siparişlerde (site + pazaryeri, silinen
    siparişler dahil) müşteri telefonu ve açık adresi en aza indirilir (bkz. minimize_order_contact_doc).

    • Süre: İşletme Kuralı privacy.order_pii_retention_days (vars. 730, taban 365; yalnız süper admin).
      Kapatma: privacy.order_pii_minimize_enabled=false.
    • İLK ÇALIŞMA KURU (dry-run): hiçbir kayıt değişmez; aday sayısı süper adminlere e-postayla raporlanır
      (kişisel veri içermez). Sonraki gece çalışmaları ETKİN uygular.
    • Açık iade/itiraz süreci (return_request açık, ters ibraz kaydı) olan sipariş ATLANIR.
    • KVKK mezar taşı / silme ile zaten koparılmış-anonimleştirilmiş kayıtlar (kvkk_* işaretli, pii_redacted)
      ATLANIR — silme işinin sonucu değiştirilmez.
    • İdempotent: contact_minimized=True işaretli kayıt yeniden işlenmez. Liderlik kaybında durur."""
    from routes.deps import db
    from business_rules import get_rule
    report = {"dry_run": False, "candidates": {}, "minimized": {}, "skipped_open": 0, "days": None}
    try:
        if not bool(await get_rule(db, "privacy.order_pii_minimize_enabled", True)):
            return {"skipped": True, "reason": "disabled"}
        try:
            days = int(await get_rule(db, "privacy.order_pii_retention_days", 730))
        except (TypeError, ValueError):
            days = 730
        days = max(ORDER_PII_RETENTION_FLOOR_DAYS, days)
        report["days"] = days
        state = await db.settings.find_one({"id": ORDER_PII_RETENTION_FLAG}, {"_id": 0}) or {}
        dry = force_dry_run or not state.get("dry_run_done")
        report["dry_run"] = dry
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        base_q = {
            "status": {"$in": _ORDER_PII_CLOSED_STATUSES},
            "contact_minimized": {"$ne": True},
            "pii_redacted": {"$ne": True},
            "kvkk_detached": {"$ne": True},
            "kvkk_anonymized": {"$ne": True},
            "return_request.status": {"$nin": _ORDER_PII_OPEN_RETURN},
            "chargeback_open": {"$ne": True},
            "$and": [
                {"$or": [{"delivered_at": {"$lt": cutoff}},
                         {"delivered_at": {"$in": [None, ""]}, "cancelled_at": {"$lt": cutoff}},
                         {"delivered_at": {"$in": [None, ""]}, "cancelled_at": {"$in": [None, ""]},
                          "created_at": {"$lt": cutoff}}]},
            ],
        }
        open_cb = set()
        try:
            async for cb in db.chargebacks.find({"status": "open"}, {"_id": 0, "order_id": 1}):
                if cb.get("order_id"):
                    open_cb.add(cb["order_id"])
        except Exception:
            pass
        now_iso = datetime.now(timezone.utc).isoformat()
        for coll_name in ("orders", "orders_deleted"):
            coll = db[coll_name]
            n_cand = n_done = 0
            last = None
            while True:
                q = base_q if last is None else {"$and": [base_q, {"_id": {"$gt": last}}]}
                batch = await coll.find(q).sort("_id", 1).limit(batch_size).to_list(batch_size)
                if not batch:
                    break
                if not dry and not await _still_leader():
                    break
                for d in batch:
                    last = d["_id"]
                    if d.get("id") in open_cb:
                        report["skipped_open"] += 1
                        continue
                    ops = minimize_order_contact_doc(d)
                    n_cand += 1
                    if dry:
                        continue
                    ops.update({"contact_minimized": True, "contact_minimized_at": now_iso,
                                "contact_minimized_days": days})
                    res = await coll.update_one({"_id": d["_id"], "contact_minimized": {"$ne": True}},
                                                {"$set": ops})
                    if getattr(res, "modified_count", 0):
                        n_done += 1
                        if coll_name == "orders" and d.get("id"):
                            # iade kayıtlarındaki müşteri telefonu/adresi de aynı kurala tabi
                            try:
                                async for r in db.customer_returns.find({"order_id": d["id"]}, {"_id": 1}):
                                    rd = await db.customer_returns.find_one({"_id": r["_id"]})
                                    r_ops = minimize_order_contact_doc(rd or {})
                                    if r_ops:
                                        await db.customer_returns.update_one(
                                            {"_id": r["_id"]}, {"$set": {**r_ops, "contact_minimized": True}})
                            except Exception:
                                pass
            report["candidates"][coll_name] = n_cand
            report["minimized"][coll_name] = n_done
        # Kullanılmış / süresi dolmuş şifre sıfırlama kodları (telefon numarası taşır) 7 günden eskiyse silinir.
        if not dry:
            try:
                _old = (datetime.now(timezone.utc) - timedelta(days=7)).timestamp()
                r = await db.password_reset_otps.delete_many({"expires_at": {"$lt": _old}})
                report["otp_purged"] = int(getattr(r, "deleted_count", 0) or 0)
            except Exception:
                pass
        if dry:
            await db.settings.update_one(
                {"id": ORDER_PII_RETENTION_FLAG},
                {"$set": {"id": ORDER_PII_RETENTION_FLAG, "dry_run_done": True, "dry_run_at": now_iso,
                          "dry_run_report": report}}, upsert=True)
            try:
                from security.events import notify_super_admins
                body = (f"KVKK veri minimizasyonu — KURU ÇALIŞMA (hiçbir kayıt değişmedi).\n\n"
                        f"Saklama süresi: {days} gün (İşletme Kuralları → privacy.order_pii_retention_days)\n"
                        f"Aday sipariş: {report['candidates'].get('orders', 0)} · silinen sipariş arşivi: "
                        f"{report['candidates'].get('orders_deleted', 0)} · açık itiraz nedeniyle atlanan: "
                        f"{report['skipped_open']}\n\n"
                        "Bir sonraki gece çalışmasında bu kapanmış siparişlerde açık adres silinecek, telefon yalnız "
                        "son 2 hane kalacak (il/ilçe, ad-soyad, fatura bilgileri korunur). Durdurmak için İşletme "
                        "Kuralları → privacy.order_pii_minimize_enabled kapatılabilir (yalnız süper admin).")
                await notify_super_admins("pii_retention_dry_run", "KVKK sipariş iletişim verisi saklama — kuru çalışma raporu",
                                          body, fingerprint=f"pii_retention_dry_run:{now_iso[:10]}", level="info",
                                          meta={"days": days, "candidates": report["candidates"]})
            except Exception as _ne:
                logger.warning(f"[pii-retention] kuru çalışma raporu gönderilemedi: {type(_ne).__name__}")
        else:
            await db.settings.update_one(
                {"id": ORDER_PII_RETENTION_FLAG},
                {"$set": {"last_run_at": now_iso, "last_report": report}}, upsert=True)
        try:
            await db.audit_logs.insert_one({
                "id": str(uuid.uuid4()), "action": "order_pii_minimize" + ("_dry_run" if dry else ""),
                "category": "compliance", "details": report, "actor": "system_scheduler",
                "created_at": now_iso})
        except Exception:
            pass
        logger.info(f"[pii-retention] {'kuru' if dry else 'etkin'} çalışma: {report}")
        return report
    except Exception as e:
        logger.exception(f"[pii-retention] hata: {e}")
        return {"error": type(e).__name__, **report}


async def _pii_retention_purge():
    """Amazon DPP uyumu — PII saklama süresi (varsayılan 30 gün) dolan siparişlerde
    kişisel verileri (isim, telefon, e-posta, adres) anonimleştirir.
    Muhasebe için sipariş no/tutar/ürün korunur; sadece kişisel tanımlayıcılar silinir.
    Config: db.settings id='pii_retention' { enabled, days, platforms }.
    """
    from routes.deps import db
    try:
        cfg = await db.settings.find_one({"id": "pii_retention"}) or {}
        if cfg.get("enabled") is False:
            return
        days = int(cfg.get("days") or DEFAULT_PII_RETENTION_DAYS)
        platforms = cfg.get("platforms") or PII_RETENTION_PLATFORMS
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        query = {
            "platform": {"$in": platforms},
            "status": {"$in": ["shipped", "delivered", "completed"]},
            "pii_redacted": {"$ne": True},
            "$or": [
                {"shipped_at": {"$lt": cutoff}},
                {"delivered_at": {"$lt": cutoff}},
                {"updated_at": {"$lt": cutoff}},
            ],
        }
        redacted = 0
        async for order in db.orders.find(query, {"_id": 0, "id": 1}):
            if not await _still_leader():  # fencing: liderlik kaybedildiyse yazma yapma
                break
            await db.orders.update_one(
                {"id": order["id"]},
                {"$set": {
                    "shipping_address.first_name": "[silindi]",
                    "shipping_address.last_name": "",
                    "shipping_address.full_name": "[silindi]",
                    "shipping_address.phone": "[silindi]",
                    "shipping_address.email": "[silindi]",
                    "shipping_address.address": "[silindi]",
                    "shipping_address.address_line": "[silindi]",
                    "billing_address.first_name": "[silindi]",
                    "billing_address.phone": "[silindi]",
                    "billing_address.email": "[silindi]",
                    "billing_address.address": "[silindi]",
                    "customer_email": "[silindi]",
                    "customer_phone": "[silindi]",
                    "customer_name": "[silindi]",
                    "buyer_email": "[silindi]",
                    "user_data": None,
                    "pii_redacted": True,
                    "pii_redacted_at": datetime.now(timezone.utc).isoformat(),
                }},
            )
            redacted += 1
        if redacted:
            logger.info(f"[scheduler][pii] {redacted} siparişte PII anonimleştirildi (>{days} gün, {platforms})")
            await db.audit_logs.insert_one({
                "id": str(uuid.uuid4()),
                "action": "pii_retention_purge",
                "category": "compliance",
                "details": {"redacted": redacted, "days": days, "platforms": platforms},
                "actor": "system_scheduler",
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
    except Exception as e:
        logger.exception(f"[scheduler] pii_retention_purge failed: {e}")


# ── §7 GÜVENLİK / AUDIT / SP-API LOG RETENTION (Amazon DPP) ─────────────────────────
# TANIMLI POLİTİKA: PII'siz denetim logları MİNİMUM 12 AY (365 gün) saklanır; bu süreyi AŞAN
# kayıtlar temizlenir (bounded retention). TABAN KİLİDİ: env ile UZATILABİLİR ama 365'in ALTINA
# İNEMEZ → hiçbir log 12 aydan erken silinemez. Silme YALNIZ bu sistem işinde yapılır; admin-facing
# log-silme ucu YOKTUR (silme yetkisi sistemle sınırlı). PII redaction ayrıca _pii_retention_purge'de.
# (koleksiyon, zaman_alanı) — hepsi ISO string zaman tutar → string "<" karşılaştırması güvenli.
_RETENTION_LOG_COLLECTIONS = [
    ("spapi_call_logs", "at"),        # Amazon SP-API çağrı denetim logu (PII'siz)
    ("capi_event_logs", "created_at"),  # CAPI server-event logları
    ("audit_logs", "created_at"),       # genel compliance/audit
    ("auth_audit_logs", "created_at"),  # giriş/kimlik denetim
    ("integration_logs", "created_at"),  # pazaryeri entegrasyon logları
]


async def _security_log_retention_purge():
    """§7 — Güvenlik/audit/SP-API loglarını ≥12 AY (365 gün) saklar, aşan kayıtları temizler.
    TABAN KİLİDİ 365 gün → hiçbir log 12 aydan erken silinmez (yanlış config'e karşı korumalı)."""
    import os as _os
    from routes.deps import db
    try:
        days = max(365, int(_os.environ.get("SECURITY_LOG_RETENTION_DAYS") or 365))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        total = 0
        for coll, tfield in _RETENTION_LOG_COLLECTIONS:
            try:
                res = await db[coll].delete_many({tfield: {"$lt": cutoff}})
                total += int(getattr(res, "deleted_count", 0) or 0)
            except Exception as _ce:
                logger.warning(f"[scheduler][retention] {coll} temizlenemedi: {_ce}")
        if total:
            logger.info(f"[scheduler][retention] {total} eski log temizlendi (>{days} gün saklama)")
            await db.audit_logs.insert_one({
                "id": str(uuid.uuid4()),
                "action": "security_log_retention_purge",
                "category": "compliance",
                "details": {"deleted": total, "retention_days": days},
                "actor": "system_scheduler",
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
    except Exception as e:
        logger.exception(f"[scheduler] security_log_retention_purge failed: {e}")


def _parse_tr_dt(s: str):
    """MNG teslim tarihini ISO'ya cevirir; basarisizsa None."""
    s = (s or "").strip()
    if not s:
        return None
    for f in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
              "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, f).replace(tzinfo=timezone.utc).isoformat()
        except Exception:
            continue
    return None


async def _dhl_cargo_poll_tick():
    """Her 5 dk: site (atilla) siparislerini MNG/DHL e-Commerce API'sinden sorgula.
      - GONDERI_NO / takip linki olustuysa -> 'shipped' (Kargoya Verildi) + bildirim
      - Teslim edildiyse -> 'delivered' (Teslim Edildi) + delivered_at + bildirim
    Bildirimler Ayarlar > Siparis Durumlari (sms/email) secimine gore gonderilir.
    SOAP cagrilari thread executor'da calisir (event loop bloklanmaz).
    """
    from routes.deps import db  # lazy
    try:
        from routes.orders import _get_mng_settings
        from order_statuses import get_status_config, customer_label_for
        from notification_service import send_notification
        from mng_kargo_client import get_mng_shipment_status
    except Exception as e:
        logger.warning(f"[scheduler][dhl] import skip: {e}")
        return

    # DENETİM FIX (#10): İzleme paneli 'dhl_poll_health' dokümanını okuyor ama hiç yazılmıyordu.
    # Başlangıç/bitiş + sayaçlar (matched/processed/shipped/delivered/errors/duration) yazılır.
    _run_started = datetime.now(timezone.utc)

    async def _write_health(status: str, **extra):
        try:
            doc = {"status": status, "updated_at": datetime.now(timezone.utc).isoformat()}
            doc.update(extra)
            await db.settings.update_one({"id": "dhl_poll_health"}, {"$set": doc}, upsert=True)
        except Exception as _he:
            logger.warning(f"[scheduler][dhl] health write err: {_he}")

    s = await _get_mng_settings()
    if not s.get("is_active") or not s.get("username"):
        logger.info("[scheduler][dhl] atlandi: MNG/DHL ayarlari aktif degil ya da kullanici adi yok")
        await _write_health("inactive", last_run_at=_run_started.isoformat(),
                            last_finish_at=datetime.now(timezone.utc).isoformat(),
                            matched=0, processed=0, shipped=0, delivered=0, errors=0,
                            duration_ms=0, interval_min=5,
                            note="MNG/DHL ayarları aktif değil ya da kullanıcı adı yok")
        return
    user, pw = s["username"], s["password"]

    cutoff = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    q = {
        "platform": {"$in": ["atilla", "site", None]},
        "status": {"$in": ["confirmed", "processing", "preparing", "ready_to_ship",
                            "shipped", "in_transit", "out_for_delivery"]},
        "$or": [
            {"cargo_tracking_number": {"$nin": [None, ""]}},
            {"cargo_provider_name": {"$nin": [None, ""]}},
            {"cargo_barcode_created": True},
        ],
        "created_at": {"$gt": cutoff},
    }
    try:
        cfg = await get_status_config(db)
    except Exception:
        cfg = {"notify": {}}
    notify = cfg.get("notify") or {}

    try:
        matched = await db.orders.count_documents(q)
    except Exception:
        matched = 0
    await _write_health("running", last_run_at=_run_started.isoformat(),
                        matched=matched, interval_min=5)

    processed = 0
    n_shipped = 0
    n_delivered = 0
    n_errors = 0
    try:
        async for order in db.orders.find(q, {"_id": 0}).limit(120):
            siparis_no = str(order.get("order_number") or order.get("id") or "").strip()
            if not siparis_no:
                continue
            try:
                info = await asyncio.to_thread(
                    get_mng_shipment_status, username=user, password=pw, siparis_no=siparis_no
                )
            except Exception as e:
                logger.warning(f"[scheduler][dhl] status err {siparis_no}: {e}")
                n_errors += 1
                await asyncio.sleep(0.2)
                continue
            if not info or not info.get("ok"):
                await asyncio.sleep(0.2)
                continue

            gonderi = (info.get("gonderi_no") or "").strip()
            url = (info.get("kargo_takip_url") or "").strip()
            # DHL eCommerce: takip no varsa müşteri direkt takip deep-link'i (genel /gonderitakip yerine)
            _track_no = gonderi or order.get("cargo_tracking_number", "")
            track_link = f"https://kargotakip.dhlecommerce.com.tr/?takipNo={_track_no}" if _track_no else url
            teslim = (info.get("teslim_tarihi") or "").strip()
            statu = (info.get("kargo_statu") or "0").strip()
            aciklama = (info.get("kargo_statu_aciklama") or "")
            cur = order.get("status")

            # Y20: "teslim alın(dı)" / "şubeden teslim" = ŞUBE/KABUL hareketi, TESLİMAT DEĞİL.
            # "teslim" substring'i bunları da eşleyip siparişi ilk taramada yanlış 'delivered'
            # yapıyor, müşteriye erken "Teslim Edildi" bildirimi gidiyordu.
            _dl_aç = aciklama.lower()
            _pickup = any(k in _dl_aç for k in ("teslim alın", "teslim alin", "şubeden teslim",
                                                "subeden teslim", "şubede teslim", "subede teslim"))
            delivered = (not _pickup) and (bool(teslim) or ("teslim" in _dl_aç and "edilemedi" not in _dl_aç))
            # İLK OKUTMA tespiti — sadece kargocu/şube siparişi fiilen okuttuğunda "Kargoya Verildi".
            # Barkod oluşturulurken gönderi_no/takip url'i dolabildiği için onlar TEK BAŞINA tetik DEĞİL;
            # yalnızca MNG/DHL bir HAREKET statüsü (kargo_statu ≠ 0) ya da kabul/şube/okutma açıklaması
            # döndürdüğünde durumu çeviriyoruz. (kargo_statu: 0=İşlem Yok, 1+/100+=şubeye girdi/işleniyor)
            _alow = aciklama.lower()
            _acc_kw = ("kabul", "şube", "sube", "okut", "teslim alın", "teslim alin",
                       "işleme", "isleme", "çıkış", "cikis", "girdi", "transfer",
                       "dağıt", "dagit", "yola")
            shipped = (statu not in ("", "0")) or any(k in _alow for k in _acc_kw)
            now_iso = datetime.now(timezone.utc).isoformat()

            new_status = None
            upd = {}
            if delivered and cur != "delivered":
                new_status = "delivered"
                upd["delivered_at"] = _parse_tr_dt(teslim) or now_iso
            elif shipped and _track_no and cur in ("confirmed", "processing", "preparing", "ready_to_ship"):
                # Takip no oluşmadan "Kargoya Verildi" YAPMA — yoksa müşteriye giden SMS'teki
                # {tracking_url} no'suz genel /gonderitakip sayfasını açar. No gelene kadar bekle;
                # sonraki taramada gönderi no dolunca durum çevrilir + deep-link'li SMS gider.
                new_status = "shipped"
                upd["shipped_at"] = now_iso

            if gonderi:
                upd["cargo_gonderi_no"] = gonderi
                upd["cargo_tracking_number"] = gonderi
            if track_link:
                upd["cargo_tracking_url"] = track_link
                upd["cargo_tracking_link"] = track_link
            if aciklama:
                upd["cargo_status_text"] = aciklama
            if new_status:
                upd["status"] = new_status
            if upd:
                upd["updated_at"] = now_iso
                await db.orders.update_one({"id": order["id"]}, {"$set": upd})

            if new_status == "shipped":
                n_shipped += 1
            elif new_status == "delivered":
                n_delivered += 1

            if new_status in ("shipped", "delivered"):
                ev = "order_shipped" if new_status == "shipped" else "order_delivered"
                nz = notify.get(new_status) or {}
                ch = [c for c in ("sms", "email") if nz.get(c)]
                if ch:
                    addr = order.get("shipping_address") or {}
                    try:
                        await send_notification(
                            db, ev,
                            to_phone=addr.get("phone") or order.get("phone"),
                            to_email=addr.get("email") or order.get("email"),
                            variables={
                                "customer_name": (f"{addr.get('first_name','')} {addr.get('last_name','')}".strip()
                                                  or addr.get("full_name") or addr.get("name") or "Müşterimiz"),
                                "order_number": siparis_no,
                                "amount": f"{order.get('total', 0):.2f} TL",
                                "tracking_number": gonderi or order.get("cargo_tracking_number", ""),
                                "tracking_url": track_link or order.get("cargo_tracking_url", ""),
                                "tracking_link": track_link or order.get("cargo_tracking_url", ""),
                                "status_label": customer_label_for(new_status),
                            },
                            channels=ch,
                        )
                    except Exception as e:
                        logger.warning(f"[scheduler][dhl] notif {siparis_no}: {e}")
                logger.info(f"[scheduler][dhl] {siparis_no} -> {new_status}")

            processed += 1
            await asyncio.sleep(0.25)

        # Influencer PR gönderilerinin takip no'su AYRI saatlik işte çekilir
        # (_influencer_pr_tracking_tick) — site-sipariş taraması hata verse/limite takılsa
        # bile PR taraması etkilenmez.

        _final_status = "ok"
    except Exception as e:
        logger.exception(f"[scheduler][dhl] poll tick failed: {e}")
        n_errors += 1
        _final_status = "error"
    _fin = datetime.now(timezone.utc)
    _dur_ms = int((_fin - _run_started).total_seconds() * 1000)
    await _write_health(
        _final_status,
        last_run_at=_run_started.isoformat(),
        last_finish_at=_fin.isoformat(),
        matched=matched, processed=processed,
        shipped=n_shipped, delivered=n_delivered, errors=n_errors,
        duration_ms=_dur_ms, interval_min=5,
    )
    logger.info(f"[scheduler][dhl] tick bitti — {processed} site siparisi sorgulandi "
                f"(shipped={n_shipped} delivered={n_delivered} err={n_errors})")


async def _influencer_pr_tracking_tick():
    """Her SAAT: influencer PR gönderilerinin DHL/MNG gerçek takip no'sunu otomatik çeker
    (routes.influencers.auto_refresh_pr_tracking). Panelde 'takip çek' butonu yok; kayıt
    kargolandıktan sonra takip no oluşunca kendiliğinden görünür."""
    try:
        from routes.influencers import auto_refresh_pr_tracking
        st = await asyncio.wait_for(auto_refresh_pr_tracking(), timeout=600)
        if st.get("found") or st.get("errors"):
            logger.info(f"[scheduler][pr-track] {st}")
    except Exception as e:
        logger.exception(f"[scheduler][pr-track] tick failed: {e}")


async def _return_cargo_poll_tick():
    """Her 5 dk: müşteriden depoya gelen İADE kargolarını MNG'den sorgula.
      - Kargo hareketi (ilk okutma) -> 'return_in_transit' (İade Kargoda) + bildirim
      - Depoya teslim -> 'returned' (Teslim Alındı; Aksiyon'a düşer) + returned_at + bildirim
      - Barkod 3 gün geçerli: süre dolmuş + hâlâ kargoya verilmemiş -> 'expired'
    customer_returns.status: created -> in_transit -> received | expired
    orders.status:           return_requested -> return_in_transit -> returned
    SOAP çağrıları thread executor'da çalışır (event loop bloklanmaz).
    """
    from routes.deps import db  # lazy
    try:
        from routes.orders import _get_mng_settings
        from order_statuses import get_status_config
        from notification_service import send_notification
        from mng_kargo_client import get_mng_shipment_status
    except Exception as e:
        logger.warning(f"[scheduler][return] import skip: {e}")
        return

    s = await _get_mng_settings()
    if not s.get("is_active") or not s.get("username"):
        return
    user, pw = s["username"], s["password"]

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    cutoff = (now - timedelta(days=45)).isoformat()
    q = {
        "status": {"$in": ["created", "in_transit"]},
        "created_at": {"$gt": cutoff},
        "$or": [
            {"mng_ref": {"$nin": [None, ""]}},
            {"return_code": {"$nin": [None, ""]}},
        ],
    }
    try:
        cfg = await get_status_config(db)
    except Exception:
        cfg = {"notify": {}}
    notify = cfg.get("notify") or {}

    def _expired(vu):
        if not vu:
            return False
        try:
            d = datetime.fromisoformat(str(vu).replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
        except Exception:
            return False
        return now > d

    processed = 0
    try:
        async for rec in db.customer_returns.find(q, {"_id": 0, "barcode_png_b64": 0}).limit(120):
            ref = str(rec.get("mng_ref") or rec.get("return_code") or "").strip()
            if not ref:
                continue
            cur = rec.get("status")  # created | in_transit

            try:
                info = await asyncio.to_thread(
                    get_mng_shipment_status, username=user, password=pw, siparis_no=ref
                )
            except Exception as e:
                logger.warning(f"[scheduler][return] status err {ref}: {e}")
                await asyncio.sleep(0.2)
                continue

            moved = delivered = False
            teslim = aciklama = ""
            if info and info.get("ok"):
                teslim = (info.get("teslim_tarihi") or "").strip()
                statu = (info.get("kargo_statu") or "0").strip()
                aciklama = (info.get("kargo_statu_aciklama") or "")
                _alow = aciklama.lower()
                # Y20: şube/kabul "teslim alındı" hareketini teslimat sayma (iade akışını erken tetikler).
                _pickup2 = any(k in _alow for k in ("teslim alın", "teslim alin", "şubeden teslim",
                                                    "subeden teslim", "şubede teslim", "subede teslim"))
                delivered = (not _pickup2) and (bool(teslim) or ("teslim" in _alow and "edilemedi" not in _alow))
                _acc_kw = ("kabul", "şube", "sube", "okut", "teslim alın", "teslim alin",
                           "işleme", "isleme", "çıkış", "cikis", "girdi", "transfer",
                           "dağıt", "dagit", "yola")
                moved = (statu not in ("", "0")) or any(k in _alow for k in _acc_kw)

            new_ret = new_order = ev = None
            ostamp = {}
            if delivered:
                # B1 fix: iade kargosu depoya ULAŞTI → durum içeride 'returned' (aksiyon/inceleme)
                # olur AMA müşteriye "İadeniz Tamamlandı" SMS'i GÖNDERİLMEZ — para henüz iade
                # edilmedi (erken/yanlış bildirim olurdu). Müşteri, iade bedeli ödenince
                # 'order_refunded' ("İade Bedeliniz Ödendi") bildirimini alır (refund_pay adımı).
                new_ret, new_order, ev = "received", "returned", None
                ostamp["returned_at"] = _parse_tr_dt(teslim) or now_iso
            elif moved and cur == "created":
                new_ret, new_order, ev = "in_transit", "return_in_transit", "order_return_in_transit"
                ostamp["return_shipped_at"] = now_iso

            # Hareket yok / hâlâ created ve 3 günlük barkod süresi dolmuş -> expired
            if new_ret is None:
                if cur == "created" and _expired(rec.get("valid_until")):
                    await db.customer_returns.update_one({"id": rec["id"]},
                        {"$set": {"status": "expired", "updated_at": now_iso}})
                    logger.info(f"[scheduler][return] {ref} -> expired (barkod 3g doldu)")
                await asyncio.sleep(0.2)
                continue

            if new_ret == cur:  # idempotent
                await asyncio.sleep(0.2)
                continue

            await db.customer_returns.update_one({"id": rec["id"]},
                {"$set": {"status": new_ret, "cargo_status_text": aciklama, "updated_at": now_iso}})
            order = await db.orders.find_one(
                {"id": rec.get("order_id")},
                {"_id": 0, "shipping_address": 1, "phone": 1, "email": 1, "total": 1}) or {}
            await db.orders.update_one({"id": rec.get("order_id")}, {"$set": {
                "status": new_order, "return_request.status": new_ret,
                "updated_at": now_iso, **ostamp,
            }})

            nz = notify.get(new_order) or notify.get(new_ret) or {}
            ch = [c for c in ("sms", "email") if nz.get(c)]
            if ch and ev:
                addr = order.get("shipping_address") or {}
                try:
                    await send_notification(
                        db, ev,
                        to_phone=addr.get("phone") or order.get("phone"),
                        to_email=addr.get("email") or order.get("email"),
                        variables={
                            "customer_name": addr.get("full_name") or addr.get("first_name") or "Müşterimiz",
                            "order_number": rec.get("order_number", ""),
                            "tracking_number": ref,
                        },
                        channels=ch,
                    )
                except Exception as e:
                    logger.warning(f"[scheduler][return] notif {ref}: {e}")

            logger.info(f"[scheduler][return] {ref} {cur} -> {new_ret}")
            processed += 1
            await asyncio.sleep(0.25)
    except Exception as e:
        logger.exception(f"[scheduler][return] poll tick failed: {e}")
    if processed:
        logger.info(f"[scheduler][return] polled {processed} return cargo(s)")


async def _notify_back_in_stock():
    """Favorilenen ürünlerden stoğu 'yok' → 'var' geçenleri tespit edip, o ürünü
    favorileyen kullanıcılara 'tekrar stokta' e-postası gönderir.

    db.product_stock_flags ile her favorilenen ürünün stok durumu izlenir; bildirim
    YALNIZCA 0→var geçişinde (ve restok döngüsü başına bir kez) atılır. İlk görülmede
    (flag yok → prev=None) bildirim ATILMAZ; bu deploy sonrası toplu spam'i önler.
    Stok master'ı Atilla olduğundan (sipariş/iptal/iade/admin/pazaryeri fark etmez)
    bu periyodik kontrol tüm stok-giriş yollarını tek noktadan yakalar.
    """
    import os
    from routes.deps import db
    try:
        from notification_service import send_notification
    except Exception as e:
        logger.warning(f"[scheduler] back-in-stock skip (import): {e}")
        return
    now_iso = datetime.now(timezone.utc).isoformat()
    site = os.environ.get("SITE_URL", "https://atilla.com.tr").rstrip("/")
    try:
        # Favori yoksa da "Gelince Haber Ver" (stock_notifications) taraması ÇALIŞMALI — eskiden
        # burada erken dönülüyordu.
        fav_pids = await db.favorites.distinct("product_id") or []
        notified = 0
        for pid in fav_pids:
            if not pid:
                continue
            prod = await db.products.find_one(
                {"id": pid},
                {"_id": 0, "id": 1, "name": 1, "slug": 1, "stock": 1, "variants": 1, "is_active": 1},
            )
            if not prod:
                continue
            variants = prod.get("variants") or []
            if variants:
                total = sum(int(v.get("stock") or 0) for v in variants if isinstance(v, dict))
            else:
                total = int(prod.get("stock") or 0)
            in_stock = (total > 0) and (prod.get("is_active", True) is not False)
            flag = await db.product_stock_flags.find_one({"product_id": pid}, {"_id": 0, "in_stock": 1})
            prev = flag.get("in_stock") if flag else None
            await db.product_stock_flags.update_one(
                {"product_id": pid},
                {"$set": {"product_id": pid, "in_stock": in_stock, "updated_at": now_iso}},
                upsert=True,
            )
            # Sadece 'yok' → 'var' geçişinde bildir (ilk görülmede prev=None → atla)
            if prev is False and in_stock:
                favs = await db.favorites.find(
                    {"product_id": pid}, {"_id": 0, "user_id": 1}
                ).to_list(10000)
                uids = list({f.get("user_id") for f in favs if f.get("user_id")})
                if not uids:
                    continue
                users = await db.users.find(
                    {"id": {"$in": uids}}, {"_id": 0, "email": 1, "first_name": 1}
                ).to_list(10000)
                link = f"{site}/urun/{prod.get('slug') or prod.get('id')}"
                for u in users:
                    email = (u.get("email") or "").strip()
                    if not email or "@" not in email:
                        continue
                    try:
                        await send_notification(
                            db, "wishlist_back_in_stock",
                            to_email=email,
                            variables={
                                "customer_name": (u.get("first_name") or "").strip() or "değerli müşterimiz",
                                "product_name": prod.get("name") or "Favori ürünün",
                                "product_link": link,
                            },
                            channels=["email"],
                        )
                        notified += 1
                    except Exception as ie:
                        logger.warning(f"[scheduler] back-in-stock send failed ({email}): {ie}")

        # DENETİM FIX (#23): "Gelince Haber Ver" (db.stock_notifications) talepleri hiç
        # bildirilmiyordu (orphaned collection). notified=False kayıtları taranır; ürün+beden
        # stoğa girmişse e-posta gönderilir ve notified=True + notified_at yazılır (beden bazlı
        # eşleşme: varyant 'size' ile). Aynı ürün+beden için stok cache'lenir (tek DB okuması).
        try:
            pending = await db.stock_notifications.find(
                {"notified": {"$ne": True}}, {"_id": 0}
            ).to_list(5000)
        except Exception:
            pending = []
        _prod_cache: dict = {}
        for req in pending:
            pid = req.get("product_id")
            size = (req.get("size") or "").strip()
            email = (req.get("email") or "").strip()
            if not pid or not email or "@" not in email:
                continue
            prod = _prod_cache.get(pid)
            if prod is None:
                prod = await db.products.find_one(
                    {"id": pid}, {"_id": 0, "id": 1, "name": 1, "slug": 1, "stock": 1,
                                  "variants": 1, "is_active": 1})
                _prod_cache[pid] = prod or {}
            if not prod:
                continue
            if prod.get("is_active", True) is False:
                continue
            variants = prod.get("variants") or []
            if size and variants:
                # Beden bazlı: yalnız o bedene ait varyant(lar)ın stoğuna bak
                sz_stock = 0
                for v in variants:
                    if not isinstance(v, dict):
                        continue
                    vsize = (v.get("size") or v.get("name") or "").strip()
                    if vsize.lower() == size.lower():
                        sz_stock += int(v.get("stock") or 0)
                available = sz_stock > 0
            elif variants:
                available = sum(int(v.get("stock") or 0) for v in variants if isinstance(v, dict)) > 0
            else:
                available = int(prod.get("stock") or 0) > 0
            if not available:
                continue
            link = f"{site}/urun/{prod.get('slug') or prod.get('id')}"
            _pname = prod.get("name") or "Ürün"
            if size:
                _pname = f"{_pname} ({size} beden)"
            try:
                await send_notification(
                    db, "stock_back_in_stock",
                    to_email=email,
                    variables={
                        "customer_name": "değerli müşterimiz",
                        "product_name": _pname,
                        "product_link": link,
                    },
                    channels=["email"],
                )
                await db.stock_notifications.update_one(
                    {"id": req.get("id")},
                    {"$set": {"notified": True, "notified_at": now_iso}},
                )
                notified += 1
            except Exception as ie:
                logger.warning(f"[scheduler] stock-notify send failed ({email}): {ie}")

        if notified:
            logger.info(f"[scheduler] back-in-stock notified={notified}")
    except Exception as e:
        logger.exception(f"[scheduler] back_in_stock failed: {e}")


async def _run_award_referrals():
    """Bölüm C: davet edilenin ilk siparişi oluştuysa referans ödüllerini ver (idempotent)."""
    try:
        from routes.referrals import award_pending_referrals
        await award_pending_referrals()
    except Exception as e:
        logger.exception(f"[scheduler] referans ödül job hata: {e}")


async def _run_award_loyalty():
    """C3: ödemesi onaylanmış üye siparişlerine sadakat puanı yaz + iptal/iadede geri al (idempotent)."""
    try:
        from routes.loyalty import award_loyalty_points
        await award_loyalty_points()
    except Exception as e:
        logger.exception(f"[scheduler] sadakat puanı job hata: {e}")


async def _run_birthday_coupons():
    """Bölüm C: bugün doğum günü olan müşterilere kupon gönder (yıl-başına idempotent)."""
    try:
        from routes.referrals import send_birthday_coupons
        await send_birthday_coupons()
    except Exception as e:
        logger.exception(f"[scheduler] doğum günü job hata: {e}")


async def _run_chargeback_deadline_reminders():
    """COM11/FIN-E03: savunma son tarihi 3 gün içinde (ya da geçmiş) AÇIK ödeme itirazları için
    süper adminlere günde bir e-posta uyarısı (routes.chargebacks; itiraz başına günde en çok 1)."""
    try:
        from routes.chargebacks import remind_chargeback_deadlines
        await remind_chargeback_deadlines(3)
    except Exception as e:
        logger.warning(f"[scheduler] ödeme itirazı hatırlatma hata: {e}")


# ==================== A2.8: TEK-LİDER (distributed leader lease) ====================
# APScheduler her PROCESS'te çalışır; Railway yatay ölçeklenirse (>1 instance) tüm zamanlı
# işler HER instance'ta tekrar koşar → çift iptal / çift reconcile / çift bildirim. Mongo
# tabanlı kısa süreli lease ile aynı anda YALNIZ bir instance "lider" olur ve işleri o koşar.
# Tek-instance dağıtımda (varsayılan) davranış değişmez. Lease okunamazsa (Mongo hatası)
# tek-instance varsayımıyla iş yine koşar (regresyon yok).
def _instance_label() -> str:
    try:
        from routes.deps import instance_tag
        return instance_tag()
    except Exception:
        return "unknown"


_INSTANCE_ID = str(uuid.uuid4())
_LEASE_TTL_SEC = 120


async def _acquire_or_renew_leadership() -> bool:
    from routes.deps import db  # lazy
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    exp_iso = (now + timedelta(seconds=_LEASE_TTL_SEC)).isoformat()
    # 1) Bize aitse yenile, süresi dolmuşsa devral — tek-döküman atomik koşullu update.
    _prev = await db.scheduler_leader.find_one({"_id": "scheduler_leader"}, {"_id": 0, "holder": 1, "holder_label": 1})
    res = await db.scheduler_leader.update_one(
        {"_id": "scheduler_leader",
         "$or": [{"holder": _INSTANCE_ID}, {"expires_at": {"$lt": now_iso}}]},
        {"$set": {"holder": _INSTANCE_ID, "holder_label": _instance_label(), "expires_at": exp_iso, "renewed_at": now_iso}},
    )
    if res.matched_count > 0:
        # LİDERLİK EL DEĞİŞTİRDİ → kayıt (hangi deployment/replica devraldı). İkinci bir senkron
        # süreci varsa burada farklı etiketler dönüşümlü görünür.
        if _prev and _prev.get("holder") != _INSTANCE_ID:
            try:
                await db.scheduler_leader_log.insert_one({
                    "at": now_iso, "from": _prev.get("holder_label") or (_prev.get("holder") or "")[:8],
                    "to": _instance_label(), "to_id": _INSTANCE_ID[:8]})
            except Exception:
                pass
        return True
    # 2) Henüz kayıt yok → atomik oluştur (benzersiz _id iki liderliği engeller).
    try:
        await db.scheduler_leader.insert_one(
            {"_id": "scheduler_leader", "holder": _INSTANCE_ID,
             "expires_at": exp_iso, "renewed_at": now_iso})
        return True
    except Exception:
        return False  # başka instance kaydı oluşturdu/tutuyor


# ── LİDER LEASE FENCING (denetim 2026-10) ────────────────────────────────────────────────
# Sorun: lease (TTL 120 sn) yalnız İŞ BAŞLARKEN yenileniyordu → 2 dk'dan uzun süren bir iş
# (ya da olay döngüsü takılması) sırasında lease düşüp başka instance lider olabiliyor ve İKİ
# instance aynı anda yazıyordu (çift iptal/iade/mutabakat). Çözüm:
#   (1) _leader_heartbeat: her 30 sn lease'i YALNIZ biz tutuyorsak uzatır (lider olmayan
#       instance lease kapmaz — devralma yine iş başında _acquire_or_renew_leadership ile).
#   (2) _still_leader(): kritik yazma döngüleri her adımda "hâlâ lider miyim?" diye bakar;
#       lease başkasına geçtiyse yazmadan DURUR. Yalnız lider-sarmalı (_lead) zamanlanmış
#       çalışmada uygulanır — admin panelinden elle tetiklenen çağrılar ETKİLENMEZ.
_IN_LEADER_JOB: contextvars.ContextVar = contextvars.ContextVar("scheduler_in_leader_job", default=False)
_HEARTBEAT_SEC = 30


async def _leader_heartbeat():
    """Lease'i yalnız mevcut sahibi (bu instance) ise uzatır. Hata → sessiz (bir sonraki tur)."""
    try:
        from routes.deps import db  # lazy
        now = datetime.now(timezone.utc)
        await db.scheduler_leader.update_one(
            {"_id": "scheduler_leader", "holder": _INSTANCE_ID, "expires_at": {"$gt": now.isoformat()}},
            {"$set": {"expires_at": (now + timedelta(seconds=_LEASE_TTL_SEC)).isoformat(),
                      "renewed_at": now.isoformat()}})
    except Exception as e:
        logger.debug(f"[scheduler] lider heartbeat atlandı: {e}")


async def _still_leader() -> bool:
    """Kritik yazmadan hemen önce: bu instance hâlâ geçerli lease sahibi mi?
    - Lider-sarmalı olmayan (elle/endpoint) çağrıda → True (davranış değişmez).
    - Lease okunamazsa (Mongo hatası) → True (_lead ile aynı tek-instance varsayımı; regresyon yok).
    - Lease başka instance'ta ya da süresi dolmuşsa → False (yazma YAPILMAZ)."""
    if not _IN_LEADER_JOB.get():
        return True
    try:
        from routes.deps import db  # lazy
        doc = await db.scheduler_leader.find_one({"_id": "scheduler_leader"},
                                                 {"_id": 0, "holder": 1, "expires_at": 1})
    except Exception:
        return True
    if not doc:
        return True  # lease kaydı yok (tek-instance / ilk açılış) → eski davranış
    ok = doc.get("holder") == _INSTANCE_ID and str(doc.get("expires_at") or "") > datetime.now(timezone.utc).isoformat()
    if not ok:
        logger.warning("[scheduler] liderlik kaybedildi — kritik yazma durduruldu (fencing)")
    return ok


# ── GLOBAL DIŞA-YAZMA DURAKLATMA (outbound pause) ─────────────────────────────────────────
# Olay/geri-yükleme (restore) anında pazaryeri/kargo/fatura entegrasyonlarına HİÇBİR otomatik
# çağrı gitmesin diye zamanlanmış dış-entegrasyon işleri başlarken bu anahtar okunur.
# security_switches modülü (ayrı değişiklik) yoksa / okunamazsa → duraklatma YOK (eski davranış).
# Zamanlama sıklıkları DEĞİŞMEZ; yalnız o tur atlanır. Elle (panelden) tetiklenen işler etkilenmez.
async def _maybe_await(v):
    if asyncio.iscoroutine(v) or isinstance(v, asyncio.Future):
        return await v
    return v


async def _outbound_paused(kind: str = "marketplace") -> bool:
    try:
        if str(os.environ.get("RESTORE_MODE") or "").strip().lower() in ("1", "true", "yes", "on"):
            return True
    except Exception:
        pass
    try:
        import security_switches as _ss  # noqa: WPS433 (lazy; başka değişiklikle gelir)
    except Exception:
        return False
    async def _call_paused(fn, k):
        # security_switches.outbound_paused() argümansız (global) da olabilir, tür alan da.
        try:
            return await _maybe_await(fn(k))
        except TypeError:
            return await _maybe_await(fn())

    try:
        fn = getattr(_ss, "outbound_paused", None)
        if callable(fn):
            if await _call_paused(fn, "marketplace"):
                return True
            if kind and kind != "marketplace" and await _call_paused(fn, kind):
                return True
        for _nm in ("restore_mode_on", "restore_mode_active", "is_restore_mode", "restore_mode"):
            _f = getattr(_ss, _nm, None)
            if callable(_f):
                if await _maybe_await(_f()):
                    return True
                break
    except Exception as e:
        logger.warning(f"[scheduler] outbound-pause anahtarı okunamadı ({e}) — duraklatma yok sayıldı")
        return False
    return False


# Dış sisteme (pazaryeri / kargo / e-fatura) çağrı yapan ZAMANLANMIŞ işler → anahtar türü.
_OUTBOUND_JOB_KINDS = {
    # Pazaryerleri (Trendyol / Hepsiburada / Amazon / Temu) — çekme + gönderme
    "_marketplace_sync_tick": "marketplace",
    "_backfill_trendyol_order_dates": "marketplace",
    "weekly_trendyol_review_sync": "marketplace",
    "_trendyol_delivery_tick": "marketplace",
    "_run_trendyol_cancel_pass": "marketplace",
    "_run_trendyol_status_wide_pass": "marketplace",
    "_run_trendyol_claims_sync": "marketplace",
    "_run_trendyol_open_claims_refresh": "marketplace",
    "_run_hepsiburada_claims_sync": "marketplace",
    "_run_amazon_claims_sync": "marketplace",
    "_run_hb_invoice_autoheal": "marketplace",
    "_run_passive_marketplace_zero_sweep": "marketplace",
    "_run_trendyol_orphan_zero": "marketplace",
    "_run_trendyol_claims_deep_backfill_once": "marketplace",
    "_run_trendyol_deep_backfill_once": "marketplace",
    "_run_amazon_auto_orders_pull": "marketplace",
    "_run_amazon_auto_stock_sync": "marketplace",
    "_ensure_amazon_otomatik_kurulum": "marketplace",
    "_ensure_amazon_katalog_tazele": "marketplace",
    "_ensure_amazon_pasif_teshis": "marketplace",
    "_ensure_amazon_volanli_bluz_yeniden": "marketplace",
    "_ticimax_sync_stock": "marketplace",
    # Kargo (MNG / DHL) durum sorgulama — sipariş statüsü + müşteri bildirimi yazar
    "_dhl_cargo_poll_tick": "cargo",
    "_return_cargo_poll_tick": "cargo",
    "_influencer_pr_tracking_tick": "cargo",
    # Doğan e-fatura otomatik işleri (iptal/yeniden kesim)
    "_run_fatura_yeniden_kesim": "invoice",
    "_ensure_hb4153_fatura_yeniden": "invoice",
    "_ensure_hb4029_fatura_iptal": "invoice",
}


def _outbound_guard(fn):
    """Dış-entegrasyon işini outbound-pause anahtarına bağlar (anahtar açıksa bu tur atlanır)."""
    kind = _OUTBOUND_JOB_KINDS.get(getattr(fn, "__name__", ""))
    if not kind:
        return fn

    async def _g(*a, **k):
        if await _outbound_paused(kind):
            logger.warning(f"[scheduler] {fn.__name__}: dışa-yazma duraklatıldı ({kind}) — bu tur atlandı")
            return None
        return await fn(*a, **k)
    _g.__name__ = getattr(fn, "__name__", "job")
    _g.__qualname__ = _g.__name__
    return _g


# Kilitlenme teşhisi: şu an çalışan zamanlayıcı işleri {ad: başlangıç} + süre istatistiği.
RUNNING_JOBS: dict = {}
JOB_STATS: dict = {}


def _lead(fn):
    """İşi yalnız lider instance koşsun diye sarmalar. Lease hatasında tek-instance
    varsayımıyla koşar (regresyon önleme).

    KÖK NEDEN DÜZELTMESİ (havale 72s / influencer takip "hiç çalışmıyor"): deploy sırasında yeni
    instance ilk _LEASE_TTL_SEC (120 sn) boyunca lider olamaz; açılışta +30 sn / +2 dk'ya kurulan
    ilk çalışmalar SESSİZCE atlanıyor ve 30 dk / 1 saatlik işler bir sonraki periyoda kalıyordu
    (art arda deploy'larda hiç sıra alamıyordu). Artık lider olunamayan çalışma, lease süresi
    dolduktan sonra (TTL+30 sn) BİR KEZ yeniden denenir; o da lider değilse gerçekten başka
    instance liderdir → bırakılır."""
    async def _w(*a, _lead_retry=False, **k):
        try:
            if not await _acquire_or_renew_leadership():
                if not _lead_retry and _scheduler is not None:
                    try:
                        _scheduler.add_job(
                            _w, "date",
                            run_date=datetime.now(timezone.utc) + timedelta(seconds=_LEASE_TTL_SEC + 30),
                            id=f"{_w.__name__}__lead_retry" + (f"__{'_'.join(map(str, a))}" if a else ""),
                            replace_existing=True,
                            # Orijinal argümanlar korunur (yoksa args'lı işler TypeError ile düşüyordu)
                            args=list(a), kwargs={**k, "_lead_retry": True}, misfire_grace_time=300)
                        logger.info(f"[scheduler] {_w.__name__}: lider değil — {_LEASE_TTL_SEC + 30} sn sonra yeniden denenecek")
                    except Exception as _re:
                        logger.warning(f"[scheduler] {_w.__name__}: lider-retry kurulamadı: {_re}")
                return None
        except Exception as _e:
            logger.warning(f"[scheduler] liderlik kontrolü atlandı ({_e}) — iş yine koşuyor")
        _nm = getattr(fn, "__name__", "job")
        RUNNING_JOBS[_nm] = time.monotonic()
        _tok = _IN_LEADER_JOB.set(True)  # fencing: kritik yazmalar _still_leader() ile denetlenir
        try:
            return await fn(*a, **k)
        finally:
            try:
                _IN_LEADER_JOB.reset(_tok)
            except Exception:
                pass
            _st = RUNNING_JOBS.pop(_nm, None)
            if _st is not None:
                _d = time.monotonic() - _st
                _prev = JOB_STATS.get(_nm) or {"n": 0, "max_s": 0.0}
                JOB_STATS[_nm] = {"n": _prev["n"] + 1, "max_s": round(max(_prev["max_s"], _d), 1),
                                  "last_s": round(_d, 1)}
    _w.__name__ = getattr(fn, "__name__", "job")
    _w.__qualname__ = _w.__name__
    return _w


async def alert_critical_stock_for_rpt():
    """RPT (tekrar üretim) uyarısı — GÜNDE BİR: son 30 günün satış hızına göre kalan stoğun
    kapsaması eşiğin (varsayılan 4 hafta = 21 gün üretim + 1 hafta güvenlik payı) altına
    düşen AKTİF ürünler için admin push atar. Eşikler İşletme Kuralları'ndan ayarlanır:
      kritik stok = haftalık hız × report.reorder_cover_weeks
    Yalnız hız ≥ report.velocity_yellow_min olan (RPT'ye değer) ürünler uyarılır;
    ürün başına en fazla 7 günde bir tekrarlanır (rpt_alerted_at)."""
    from routes.deps import db
    from routes.reports import _EXCLUDED_STATUSES
    from business_rules import get_rule
    try:
        cover_weeks = float(await get_rule(db, "report.reorder_cover_weeks", 4) or 4)
        min_rate = float(await get_rule(db, "report.velocity_yellow_min", 5) or 5)
    except Exception:
        cover_weeks, min_rate = 4.0, 5.0
    now = datetime.now(timezone.utc)
    s = (now - timedelta(days=30)).isoformat()

    # Son 30 gün satışları — kalem bazında (barkod + pid)
    pipe = [
        {"$match": {"created_at": {"$gte": s}, "status": {"$nin": _EXCLUDED_STATUSES}}},
        {"$unwind": {"path": "$items", "preserveNullAndEmptyArrays": False}},
        {"$group": {"_id": {"bc": {"$toString": {"$ifNull": ["$items.barcode", ""]}},
                            "pid": {"$toString": {"$ifNull": ["$items.product_id", ""]}}},
                    "qty": {"$sum": {"$ifNull": ["$items.quantity", 1]}}}},
    ]
    rows = [r async for r in db.orders.aggregate(pipe)]
    bcs = [r["_id"]["bc"] for r in rows if r["_id"].get("bc")]
    pids = [r["_id"]["pid"] for r in rows if r["_id"].get("pid")]

    by_bc, by_id = {}, {}
    q = {"$or": []}
    if pids:
        q["$or"].append({"id": {"$in": pids}})
    if bcs:
        q["$or"] += [{"barcode": {"$in": bcs}}, {"variants.barcode": {"$in": bcs}}]
    if not q["$or"]:
        return
    async for p in db.products.find(
            {**q, "is_active": True, "is_deleted": {"$ne": True}},
            {"_id": 0, "id": 1, "name": 1, "stock": 1, "variants": 1, "barcode": 1,
             "rpt_alerted_at": 1}):
        variants = p.get("variants") or []
        stock = sum(int(v.get("stock") or 0) for v in variants) if variants else int(p.get("stock") or 0)
        info = {"id": str(p.get("id")), "name": p.get("name") or "", "stock": stock,
                "rpt_alerted_at": p.get("rpt_alerted_at")}
        by_id[info["id"]] = info
        if p.get("barcode"):
            by_bc[str(p["barcode"])] = info
        for v in variants:
            if v.get("barcode"):
                by_bc[str(v["barcode"])] = info

    # Ürün bazında 30 günlük adet topla
    qty_by_pid = {}
    for r in rows:
        info = by_bc.get(r["_id"].get("bc") or "") or by_id.get(r["_id"].get("pid") or "")
        if info:
            qty_by_pid[info["id"]] = qty_by_pid.get(info["id"], 0) + int(r.get("qty") or 0)

    critical = []
    for pid, qty30 in qty_by_pid.items():
        info = by_id.get(pid)
        if not info:
            continue
        rate = qty30 / (30.0 / 7.0)   # haftalık hız
        if rate < min_rate:
            continue                   # yavaş ürün — RPT uyarısına değmez
        threshold = rate * cover_weeks
        if info["stock"] > threshold:
            continue
        # 7 günde birden sık tekrarlama
        try:
            if info.get("rpt_alerted_at") and (now - datetime.fromisoformat(str(info["rpt_alerted_at"]))).days < 7:
                continue
        except Exception:
            pass
        critical.append({"id": pid, "name": info["name"], "stock": info["stock"],
                         "rate": round(rate, 1), "threshold": int(threshold)})

    if not critical:
        return
    critical.sort(key=lambda x: x["stock"] / max(x["rate"], 0.1))  # en acil (en az hafta kalan) önce
    lines = [f"• {c['name'][:40]}: stok {c['stock']} (hız {c['rate']}/hf, eşik ~{c['threshold']})"
             for c in critical[:4]]
    if len(critical) > 4:
        lines.append(f"…ve {len(critical) - 4} ürün daha")
    try:
        from routes.push import send_push_to_admins
        await send_push_to_admins(
            f"🧵 RPT zamanı: {len(critical)} ürün kritik stokta",
            "\n".join(lines),
            {"type": "rpt_alert"})
    except Exception as e:
        logger.warning("[rpt-alert] push gönderilemedi: %s", e)
    for c in critical:
        await db.products.update_one({"id": c["id"]}, {"$set": {"rpt_alerted_at": now.isoformat()}})
    logger.info("[rpt-alert] %d ürün için RPT uyarısı gönderildi", len(critical))


async def _run_n11_sync():
    try:
        from routes.integrations_n11 import n11_scheduled_sync
        await n11_scheduled_sync()
    except Exception as e:
        logger.warning(f"[n11] zamanlanmış senkron hatası: {e}")


async def _refresh_instagram_token():
    """SINIRSIZ Instagram feed: 60 günlük kullanıcı token'ı ~45 günde bir otomatik yeniden
    exchange edilir (fb_exchange_token → taze 60 gün). Kullanıcı bir daha dokunmaz.
    token_obtained_at 45 günden yeniyse hiçbir şey yapmaz; app_id sayısal değilse (bozuk kayıt) atlar.
    Ödeme/sipariş akışıyla ilgisi yoktur — tamamen bağımsız görev."""
    from routes.deps import db
    try:
        from security.crypto import encrypt, decrypt
    except Exception:
        def encrypt(x): return x
        def decrypt(x): return x
    try:
        s = await db.settings.find_one({"id": "instagram"}, {"_id": 0}) or {}
        tok_enc = s.get("access_token")
        app_id = str(s.get("app_id") or "").strip()
        app_secret_enc = s.get("app_secret")
        obtained = s.get("token_obtained_at")
        if not (tok_enc and app_id and app_secret_enc and obtained) or not app_id.isdigit():
            return
        try:
            _dt = datetime.fromisoformat(obtained)
            if _dt.tzinfo is None:
                _dt = _dt.replace(tzinfo=timezone.utc)
        except Exception:
            return
        if (datetime.now(timezone.utc) - _dt).days < 45:
            return  # henüz erken
        cur_token = decrypt(tok_enc)
        app_secret = decrypt(app_secret_enc)
        import httpx
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get("https://graph.facebook.com/v23.0/oauth/access_token", params={
                "grant_type": "fb_exchange_token", "client_id": app_id,
                "client_secret": app_secret, "fb_exchange_token": cur_token})
        if r.status_code == 200 and (r.json() or {}).get("access_token"):
            new_tok = r.json()["access_token"]
            await db.settings.update_one({"id": "instagram"}, {"$set": {
                "access_token": encrypt(new_tok),
                "token_obtained_at": datetime.now(timezone.utc).isoformat(),
                "token_refreshed_at": datetime.now(timezone.utc).isoformat(),
                "last_error": ""}})
            logger.info("[instagram] uzun ömürlü token otomatik yenilendi (+60 gün)")
        else:
            _t = r.text[:200] if hasattr(r, "text") else str(r.status_code)
            logger.warning(f"[instagram] token yenilenemedi: {_t}")
            await db.settings.update_one({"id": "instagram"}, {"$set": {
                "last_error": "Instagram anahtarı otomatik yenilenemedi — 'Facebook ile Bağlan' ile yeniden bağlanın. "
                              f"({_t[:150]})"}})
    except Exception as e:
        logger.warning(f"[instagram] token refresh hata: {e}")


async def _run_visual_index_refresh():
    """WhatsApp görselden-ürün-tanıma hafızasını OTOMATİK doldurur/günceller.
    Yalnız EKSİK (indekslenmemiş) aktif ürünleri işler → ilk çalışmada tümünü kurar,
    sonraki çalışmalarda yeni eklenenleri tamamlar (token/manuel tetik gerekmez)."""
    try:
        from routes.whatsapp_webhook import _run_visual_index_build
        await _run_visual_index_build(force=False)
    except Exception as e:
        logger.error(f"[cron] görsel indeks yenileme hatası: {e}")


async def _backfill_trendyol_order_dates():
    """GEÇMİŞ SIFIR-SAPMA (Kadir: Haziran'dan beri düzelt): Trendyol siparişlerine OTANTİK
    orderDate'i (marketplace_order_date) yazar — SALT TARİH. Stok/statü/kalem verisine
    DOKUNMAZ (bu yüzden reconcile değil — reconcile confirmed→cancelled geçişinde stok hareketi
    yapar). Tek seferlik (settings flag). Rapor effective-date'i (marketplace_order_date ??
    created_at) bununla saydığından, geçmiş de Trendyol'un orderDate kümesiyle oturur."""
    try:
        from routes.deps import db as _db
        _st = await _db.settings.find_one({"id": "ty_orderdate_backfill"}, {"_id": 0}) or {}
        if _st.get("done"):
            return
        from routes.integrations import get_trendyol_config, _ms_to_iso
        cfg = await get_trendyol_config()
        if not cfg.get("is_active"):
            return
        import sys, os
        from datetime import datetime as _dt
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from trendyol_client import TrendyolClient
        from routes.integrations_trendyol import _ty_fetch_orders_range
        client = TrendyolClient(supplier_id=cfg["supplier_id"], api_key=cfg["api_key"],
                                api_secret=cfg["api_secret"], mode=cfg["mode"])
        start_ms = int(_dt(2026, 6, 1).timestamp() * 1000)   # Haziran 1'den itibaren
        end_ms = int(_dt.now().timestamp() * 1000)
        by_order = await _ty_fetch_orders_range(client, start_ms, end_ms)
        _updated = 0
        for onum, d in (by_order or {}).items():
            od = _ms_to_iso(d.get("order_date"))
            if not od or not onum:
                continue
            # SALT TARİH — items/status/stok'a DOKUNMAZ. Hem platform hem marketplace alanını tolere et.
            res = await _db.orders.update_one(
                {"order_number": str(onum),
                 "$or": [{"platform": "trendyol"}, {"marketplace": "trendyol"}]},
                {"$set": {"marketplace_order_date": od}})
            if res.modified_count:
                _updated += 1
        await _db.settings.update_one(
            {"id": "ty_orderdate_backfill"},
            {"$set": {"done": True, "updated": _updated, "at": _dt.now().isoformat()},
             "$setOnInsert": {"id": "ty_orderdate_backfill"}}, upsert=True)
        logger.info(f"[backfill] Trendyol orderDate backfill (Haziran→bugün): {_updated} sipariş güncellendi")
    except Exception as _e:
        logger.error(f"[backfill] Trendyol orderDate backfill hata: {_e}")


async def _run_member_stats_refresh():
    """Üye sipariş istatistiklerini (users.cached_*) yeniden hesapla — üye listesi/segment/stats
    sayfaları bu önbellekten okur (per-üye aggregation kaldırıldı → sayfalar anında açılır)."""
    try:
        from routes.members import _refresh_member_stats
        res = await _refresh_member_stats()
        logger.info(f"[cron] Üye istatistik önbelleği güncellendi: {res}")
    except Exception as e:
        logger.warning(f"[cron] Üye istatistik refresh hata: {e}")


# ═══════════════ T17: ENTEGRASYON TAZELİK ALARMI (15 dk'da bir) ═══════════════════════════
# Pazaryeri senkronu (Trendyol/HB/Amazon sipariş çekme + stok gönderimi) ya da XML feed, kendi
# çalışma aralığının N katı (varsayılan 5×, en az 30 dk) boyunca BAŞARILI olmadıysa güvenlik/ops
# uyarısı: pencere başına BİR kez (yeniden uyarı ancak bir sonraki pencerede). Kaynak yeniden
# başarılı olunca tek "düzeldi" bildirimi. Son başarı `integration_logs`'tan (status success/partial)
# okunur; XML feed için settings.xml_feed.last_sync (yalnız başarıda yazılır) ve beklenen aralık
# (settings.integration_freshness.xml_feed_interval_hours — tanımsızsa XML izlenmez; feed elle).
# Ayarlar (admin, kod değil): settings {id:"integration_freshness", multiplier, min_minutes,
# disabled:[kaynak], xml_feed_interval_hours}. Durum: settings {id:"integration_freshness_state"}.
# Dışa-yazma duraklatıldıysa (acil anahtar / RESTORE_MODE) pazaryeri kaynakları bilinçli olarak
# susar → alarm üretilmez ("paused").
_FRESHNESS_CFG_ID = "integration_freshness"
_FRESHNESS_STATE_ID = "integration_freshness_state"
_FRESHNESS_DEFAULT_MULT = 5
_FRESHNESS_MIN_MINUTES = 30
# (anahtar, etiket, pazaryeri, log action'ları, marketplace_accounts auto_sync türü, varsayılan aralık dk)
_FRESHNESS_SOURCES = (
    ("trendyol_orders", "Trendyol sipariş çekme", "trendyol", ("order_pull",), "orders", 2),
    ("trendyol_stock", "Trendyol stok/fiyat gönderimi", "trendyol", ("stock_update",), "products", 3),
    ("hepsiburada_orders", "Hepsiburada sipariş çekme", "hepsiburada", ("order_pull",), "orders", 2),
    ("hepsiburada_stock", "Hepsiburada stok/fiyat gönderimi", "hepsiburada", ("stock_update",), "products", 3),
    ("amazon_orders", "Amazon sipariş çekme", "amazon", ("order_pull",), None, 2),
    ("amazon_stock", "Amazon stok/fiyat gönderimi", "amazon", ("stock_sync",), None, 1),
)


# Sağlıklı ama İŞSİZ tur kalp atışı: Amazon başarı logu yalnız yeni/değişen sipariş (ya da
# gönderilen SKU) olduğunda yazılır. Sipariş gelmeyen sessiz dönemde sağlıklı turlar log
# bırakmadığından tazelik alarmı YANLIŞ "durdu" diyordu. Tur sağlayıcıya ulaşıp hatasız bitince
# buraya zaman damgası yazılır (süreç içi 10 dk'da bir — Atlas yükü), alarm ikisinin yenisine bakar.
_FRESHNESS_HB_ID = "integration_heartbeats"
_FRESHNESS_HB_EVERY_S = 600
_freshness_hb_mem: dict = {}


async def note_integration_heartbeat(source: str) -> None:
    """Asla fırlatmaz."""
    now_ts = time.time()
    if now_ts - _freshness_hb_mem.get(source, 0.0) < _FRESHNESS_HB_EVERY_S:
        return
    try:
        from routes.deps import db
        await db.settings.update_one(
            {"id": _FRESHNESS_HB_ID},
            {"$set": {"id": _FRESHNESS_HB_ID,
                      f"sources.{source}": datetime.now(timezone.utc).isoformat()}},
            upsert=True)
        _freshness_hb_mem[source] = now_ts
    except Exception as e:
        logger.warning(f"[freshness] kalp atışı yazılamadı {source}: {type(e).__name__}")


async def _freshness_amazon_connected() -> bool:
    try:
        from routes.amazon_spapi import _get_config
        cfg = await _get_config()
        return bool(cfg and cfg.get("refresh_token_enc") and cfg.get("selling_partner_id"))
    except Exception:
        return False


async def check_integration_freshness() -> dict:
    """ZAMANLAYICI İŞİ (15 dk). Dönüş: {kaynak: {status, age_min, threshold_min, alerted}}. Asla fırlatmaz."""
    from routes.deps import db
    now = datetime.now(timezone.utc)
    out: dict = {}
    try:
        cfg = await db.settings.find_one({"id": _FRESHNESS_CFG_ID}, {"_id": 0}) or {}
        state = await db.settings.find_one({"id": _FRESHNESS_STATE_ID}, {"_id": 0}) or {}
        src_state = dict(state.get("sources") or {})
        try:
            mult = max(2.0, float(cfg.get("multiplier") or _FRESHNESS_DEFAULT_MULT))
        except (TypeError, ValueError):
            mult = float(_FRESHNESS_DEFAULT_MULT)
        try:
            min_min = max(5.0, float(cfg.get("min_minutes") or _FRESHNESS_MIN_MINUTES))
        except (TypeError, ValueError):
            min_min = float(_FRESHNESS_MIN_MINUTES)
        disabled = set(cfg.get("disabled") or [])
        mp_paused = await _outbound_paused("marketplace")
        hb = ((await db.settings.find_one({"id": _FRESHNESS_HB_ID}, {"_id": 0}) or {}).get("sources") or {})
        last_errors: dict = {}

        sources = []   # (key, label, interval_min, last_success_iso)
        for key, label, mp, actions, kind, default_iv in _FRESHNESS_SOURCES:
            if key in disabled:
                continue
            if kind:
                acc = await db.marketplace_accounts.find_one({"key": mp}, {"_id": 0}) or {}
                sync = acc.get("auto_sync") or {}
                if not (acc.get("enabled") and sync.get(f"{kind}_enabled")):
                    continue
                try:
                    iv = max(1.0, float(sync.get(f"{kind}_interval_min") or default_iv))
                except (TypeError, ValueError):
                    iv = float(default_iv)
            else:
                if not await _freshness_amazon_connected():
                    continue
                iv = float(default_iv)
            if mp_paused:
                out[key] = {"status": "paused"}
                continue
            last = await db.integration_logs.find_one(
                {"marketplace": mp, "action": {"$in": list(actions)}, "status": {"$in": ["success", "partial"]}},
                {"_id": 0, "created_at": 1}, sort=[("created_at", -1)])
            last_iso = (last or {}).get("created_at")
            hb_iso = hb.get(key)
            if hb_iso and (_parse_iso_utc(hb_iso) or now) > (_parse_iso_utc(last_iso) or datetime.min.replace(tzinfo=timezone.utc)):
                last_iso = hb_iso
            # Neden durdu? Son başarıdan sonraki en yeni hata mesajı uyarıya eklenir.
            q_err = {"marketplace": mp, "action": {"$in": list(actions)}, "status": "failed"}
            if last_iso:
                q_err["created_at"] = {"$gt": last_iso}
            err = await db.integration_logs.find_one(q_err, {"_id": 0, "message": 1, "created_at": 1},
                                                     sort=[("created_at", -1)])
            if err:
                last_errors[key] = f"{str(err.get('message') or '')[:240]} ({err.get('created_at')})"
            sources.append((key, label, iv, last_iso))
        try:
            xml_h = float(cfg.get("xml_feed_interval_hours") or 0)
        except (TypeError, ValueError):
            xml_h = 0.0
        if xml_h > 0 and "xml_feed" not in disabled:
            xs = await db.settings.find_one({"id": "xml_feed"}, {"_id": 0, "last_sync": 1}) or {}
            sources.append(("xml_feed", "Ürün XML feed içe aktarma", xml_h * 60.0, xs.get("last_sync")))

        from security.alerts import send_alert
        for key, label, iv, last_iso in sources:
            st = dict(src_state.get(key) or {})
            st.setdefault("monitored_since", now.isoformat())
            threshold = max(min_min, mult * iv)
            ref = _parse_iso_utc(last_iso) or _parse_iso_utc(st.get("monitored_since")) or now
            age = (now - ref).total_seconds() / 60.0
            stale = age > threshold
            alerted = False
            if stale:
                last_alert = _parse_iso_utc(st.get("last_alert_at"))
                if not last_alert or (now - last_alert).total_seconds() / 60.0 >= threshold:
                    bucket = int(now.timestamp() // (threshold * 60))
                    await send_alert(
                        "integration_stale", f"Entegrasyon durdu: {label}",
                        (f"{label} {int(age)} dakikadır başarılı çalışmadı (beklenen aralık {iv:g} dk, alarm eşiği "
                         f"{threshold:g} dk). Son başarı: {last_iso or 'hiç'}.\n\n"
                         + (f"Son hata: {last_errors[key]}\n\n" if last_errors.get(key) else
                            "Bu sürede hata kaydı da yok — iş hiç çalışmıyor olabilir (zamanlayıcı/kilit).\n\n")
                         + "Otomasyon Durumu ve Entegrasyon Logları ekranından nedeni kontrol edin "
                         "(kimlik bilgisi, sağlayıcı kesintisi, kilit)."),
                        level="warning", fingerprint=f"freshness:{key}:{bucket}",
                        meta={"source": key, "age_min": int(age), "threshold_min": int(threshold)})
                    st["last_alert_at"] = now.isoformat()
                    alerted = True
                    try:
                        await db.integration_logs.insert_one({
                            "id": uuid.uuid4().hex, "service": "security", "event": "integration_stale",
                            "source": key, "age_min": int(age), "threshold_min": int(threshold),
                            "last_success": last_iso, "status": "error", "created_at": now.isoformat()})
                    except Exception:
                        pass
                st["stale"] = True
                if not st.get("stale_since"):
                    st["stale_since"] = now.isoformat()
            else:
                if st.get("stale"):
                    await send_alert(
                        "integration_recovered", f"Entegrasyon yeniden çalışıyor: {label}",
                        f"{label} yeniden başarılı (son başarı: {last_iso}).", level="info",
                        fingerprint=f"freshness-ok:{key}:{st.get('stale_since')}", meta={"source": key})
                st.update({"stale": False, "stale_since": None, "last_alert_at": None})
            st.update({"last_success": last_iso, "age_min": round(age, 1), "threshold_min": threshold,
                       "checked_at": now.isoformat()})
            src_state[key] = st
            out[key] = {"status": "stale" if stale else "ok", "age_min": round(age, 1),
                        "threshold_min": threshold, "alerted": alerted}
        await db.settings.update_one(
            {"id": _FRESHNESS_STATE_ID},
            {"$set": {"id": _FRESHNESS_STATE_ID, "sources": src_state, "checked_at": now.isoformat()}},
            upsert=True)
    except Exception as e:
        logger.warning(f"[freshness] kontrol hatası: {type(e).__name__}: {e}")
    return out


def _parse_iso_utc(v):
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def start_scheduler():
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    # DENETİM (2026-09-29): varsayılan misfire_grace_time 1 sn → tetik anında döngü 1 sn meşgulse
    # günlük işler (doğum günü kuponu, stok uyarıları) o gün SESSİZCE atlanıyordu. İş tanımında
    # açıkça verilen değerler geçerliliğini korur.
    _scheduler = AsyncIOScheduler(timezone="UTC", job_defaults={"misfire_grace_time": 3600, "coalesce": True})

    # ADM-02: kullanıcısız denetim kayıtları iş adını taşısın (actor.id = "system:<iş>").
    try:
        from activity_audit import as_system_job as _as_system_job
    except Exception:  # noqa: BLE001 — yoksa davranış değişmez
        def _as_system_job(f, name=""):
            return f

    def _add(fn, *a, **k):
        # A2.8: her iş lider-sarmalıyla eklenir (çok-instance'ta çift çalışmayı önler).
        # Dış-entegrasyon işleri ayrıca global outbound-pause anahtarına bağlanır (sıklık DEĞİŞMEZ).
        return _scheduler.add_job(_lead(_outbound_guard(_as_system_job(fn, k.get("id") or ""))), *a, **k)
    # Üyelere özel (erken erişim) pazaryeri engel listesi: HER süreçte (lider-sarmalı DEĞİL) 1 dk'da
    # bir tazelenir — senkron Hepsiburada istemcisi bu önbelleği okur.
    async def _mp_block_refresh():
        try:
            from marketplace_block import refresh_blocked
            await refresh_blocked(force=True)
        except Exception as _e:
            logger.warning(f"[mp-block] tazeleme hata: {_e}")
    _scheduler.add_job(_mp_block_refresh, "interval", seconds=60, id="mp_block_refresh",
                       max_instances=1, coalesce=True, misfire_grace_time=60,
                       next_run_time=datetime.now(timezone.utc) + timedelta(seconds=5))
    # Lider lease heartbeat (fencing): lease'i yalnız sahibi uzatır; lider-sarmalı DEĞİL.
    _scheduler.add_job(
        _leader_heartbeat,
        "interval",
        seconds=_HEARTBEAT_SEC,
        id="scheduler_leader_heartbeat",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=_HEARTBEAT_SEC,
    )
    # Üye sipariş istatistiği önbelleği (users.cached_*): boot+60s'de ilk dolum, sonra 15 dk'da bir.
    # Üye listesi/segment/stats sayfalarının hızı için — canlı per-üye aggregation kaldırıldı.
    _add(
        _run_member_stats_refresh,
        "interval",
        minutes=15,
        id="member_stats_refresh",
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=60),
        max_instances=1,
        coalesce=True,
    )
    # n11: Pazaryeri Hub'da "Otomatik Güncelleme" açıksa sipariş (+ istenirse stok) senkronu.
    _add(
        _run_n11_sync,
        "interval",
        minutes=10,
        id="n11_sync_10m",
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=150),
        max_instances=1,
        coalesce=True,
    )
    # SINIRSIZ Instagram feed: token'ı günde bir kontrol et, 45 günü geçince otomatik yenile.
    _add(
        _refresh_instagram_token,
        "interval",
        hours=24,
        id="instagram_token_refresh",
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=120),
        max_instances=1,
        coalesce=True,
    )
    # TEK SEFERLİK: Trendyol siparişlerine Haziran'dan beri OTANTİK orderDate'i yaz (salt tarih;
    # stok/kalem yok). Settings flag ile bir kez çalışır; sonraki turlar no-op. Boot+3dk sonra.
    _add(
        _backfill_trendyol_order_dates,
        "interval",
        hours=24,
        id="ty_orderdate_backfill",
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=180),
        max_instances=1,
        coalesce=True,
    )
    # WhatsApp görsel hafızası (görselden ürün tanıma) — OTOMATİK doldur/güncelle.
    # İlk çalışma boot+3dk'da tüm eksikleri kurar; 12 saatte bir yeni ürünleri tamamlar
    # (yalnız indekslenmemişleri işler → tekrar tarama yapmaz, maliyet düşük).
    _add(
        _run_visual_index_refresh,
        "interval",
        hours=12,
        id="wa_visual_index_refresh",
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=180),
        max_instances=1,
        coalesce=True,
    )
    # Run every 30 minutes; catches orders promptly as they cross the 48h mark
    _add(
        auto_cancel_unpaid_havale_orders,
        "interval",
        minutes=30,
        id="auto_cancel_havale_48h",
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=30),
        max_instances=1,
        coalesce=True,
    )
    # Havale/EFT ödeme HATIRLATMA — her 30 dk; süre İşletme Kuralları'ndan (varsayılan 24 saat)
    _add(
        send_havale_payment_reminders,
        "interval",
        minutes=30,
        id="havale_payment_reminder",
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=90),
        max_instances=1,
        coalesce=True,
    )
    # SÜRESİ DOLAN SİTE İADE TALEPLERİ: kargoya verilmeyen talepler sonsuza kadar
    # "iade" görünmesin; sipariş normale dönsün (ciroya girsin, listede çıksın).
    _add(
        expire_stale_site_return_requests,
        "interval",
        hours=6,
        id="expire_stale_return_requests",
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=90),
        max_instances=1,
        coalesce=True,
    )
    # KRİTİK: başarısız/ödenmemiş KART siparişleri — 3 saat sonra iptal + stok iadesi.
    # Önceden HİÇ zamanlanmamıştı → başarısız kart ödemeleri stoğu kalıcı sızdırıyordu.
    _add(
        auto_cancel_unpaid_card_orders,
        "interval",
        minutes=30,
        id="auto_cancel_card_24h",
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=45),
        max_instances=1,
        coalesce=True,
    )
    # iyzico OTOMATİK reconcile: para çekilmiş ama 'ödendi' işaretlenmemiş kart siparişlerini
    # her 15 dk'da iyzico'dan doğrulayıp finalize eder (elle recover-charged gerekmez).
    _add(
        reconcile_charged_but_unrecorded_orders,
        "interval",
        minutes=15,
        id="iyzico_reconcile_charged",
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=90),
        max_instances=1,
        coalesce=True,
    )
    # İYS: bildirilmemiş izinleri her 30 dk'da NetGSM'e yeniden gönder. NetGSM modülü
    # aktif olur olmaz (yansıma/aktivasyon gecikmesi sonrası) bekleyenler otomatik geçer.
    _add(
        retry_pending_iys_consents,
        "interval",
        minutes=60,
        id="iys_retry_pending",
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=60),
        max_instances=1,
        coalesce=True,
    )
    # İYS RET taraması: İYS'de (SMS RET / iys.org.tr) reddedenler sitede de RET olur. Saatte bir,
    # tur başına en çok 200 sorgu; her alıcı günde en çok bir kez sorgulanır.
    _add(
        pull_iys_rets,
        "interval",
        minutes=60,
        id="iys_ret_pull",
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=10),
        max_instances=1,
        coalesce=True,
    )
    # Instagram akışı — auto_sync açık + token varsa her 30 dk @atilla gönderilerini tazeler.
    try:
        from routes.instagram import auto_sync_instagram
        # Kadir: Instagram feed'i 2 GÜNDE BİR arka planda otomatik tara (elle "şimdi çek" dışında).
        # Boot'tan 2 dk sonra bir kez, sonra her 48 saatte bir. (Önceden 30 dk idi — IG API'ye
        # gereksiz yüktü ve etkili çalışmıyordu; istenen kadans 2 gün.)
        _add(
            auto_sync_instagram,
            "interval",
            days=2,
            id="instagram_auto_sync",
            next_run_time=datetime.now(timezone.utc) + timedelta(seconds=120),
            max_instances=1,
            coalesce=True,
        )
    except Exception as _e:
        logging.getLogger("scheduler").warning("[scheduler] instagram job eklenemedi: %s", _e)
    # Trendyol yorumları — HAFTADA BİR 4-5 yıldız yorumları otomatik çeker (worker/proxy varsa).
    try:
        from routes.integrations_trendyol_qna import weekly_trendyol_review_sync
        _add(
            weekly_trendyol_review_sync,
            "interval",
            days=7,
            id="trendyol_reviews_weekly",
            next_run_time=datetime.now(timezone.utc) + timedelta(minutes=10),
            max_instances=1,
            coalesce=True,
        )
    except Exception as _e:
        logging.getLogger("scheduler").warning("[scheduler] trendyol yorum job eklenemedi: %s", _e)
    # RPT kritik stok uyarısı — her sabah 05:00 UTC (08:00 TR): kapsaması eşiğin altına
    # düşen ürünler için admin push (21 gün üretim + güvenlik payı; İşletme Kuralları'ndan ayarlı).
    _add(
        alert_critical_stock_for_rpt,
        "cron",
        hour=5, minute=0,
        id="rpt_critical_stock_alert",
        max_instances=1,
        coalesce=True,
    )
    # Marketplace auto-sync tick — her 1 dk'da çalışır, sonra tek tek
    # account'lara ait interval'lere göre ürün/sipariş senkronu planlar.
    # Bu sayede "3 dk'da bir ürün gönder" gibi ince ayarlar çalışır.
    _add(
        _marketplace_sync_tick,
        "interval",
        minutes=1,
        id="marketplace_auto_sync_tick",
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=45),
        max_instances=1,
        coalesce=True,
    )
    # Tek seferlik: panel boşta-kalma süresi 24 saat (kullanıcı isteği). Bayrakla bir kez çalışır.
    _add(
        _ensure_panel_session_24h,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=15),
        id="ensure_panel_session_24h_once",
        misfire_grace_time=3600,
    )
    # Her açılışta (idempotent): düz metin kalan sağlayıcı sırlarını şifrele (denetim #6).
    _add(
        _ensure_provider_secrets_encrypted,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=18),
        id="ensure_provider_secrets_encrypted_once",
        misfire_grace_time=3600,
    )
    # Tek seferlik: Hepsiburada senkronunu 2 dk'ya çek (stok + sipariş). Bayrakla bir kez çalışır.
    _add(
        _ensure_hb_2min_sync,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=20),
        id="ensure_hb_2min_sync_once",
        # Açılış yavaşsa iş düşmesin: APScheduler varsayılan gecikme
        # toleransı 1 sn ve "date" tetikleyicisi bir daha çalışmaz.
        misfire_grace_time=3600,
    )
    # Tek seferlik: TÜM pazaryerlerinde sipariş çekme 2 dk (kullanıcı kararı). Bayrakla
    # bir kez çalışır; sonrasında Admin → Otomasyon'dan serbestçe değiştirilebilir.
    _add(
        _ensure_all_orders_2min,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=25),
        id="ensure_all_orders_2min_once",
        # Açılış yavaşsa iş düşmesin: APScheduler varsayılan gecikme
        # toleransı 1 sn ve "date" tetikleyicisi bir daha çalışmaz.
        misfire_grace_time=3600,
    )
    # Tek seferlik: konsinye stok alanını veritabanından da kaldır (ölü alan).
    _add(
        _ensure_consignment_removed,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=30),
        id="ensure_consignment_removed_once",
        # Açılış yavaşsa iş düşmesin: APScheduler varsayılan gecikme
        # toleransı 1 sn ve "date" tetikleyicisi bir daha çalışmaz.
        misfire_grace_time=3600,
    )
    # Tek seferlik · YALNIZ DEFTER: eski iade hareketlerine delta yaz (stok değişmez).
    # Tek seferlik: Trendyol sipariş tarihlerini gerçek UTC'ye çevir (TR-epoch hatası).
    _add(
        _ensure_trendyol_date_utc_fix,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=55),
        id="ensure_ty_date_utc_fix_once",
        misfire_grace_time=3600,
    )
    # Tek seferlik (Kadir isteği 2026-09-28): HB4153705034 iptal edilen faturanın yeniden kesimi.
    _add(
        _ensure_hb4153_fatura_yeniden,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=60),
        id="ensure_hb4153_fatura_yeniden_once",
        misfire_grace_time=3600,
    )
    # Tek seferlik (2026-09-29): SALE menüsü başlangıç kalemleri.
    _add(
        _ensure_sale_menu_seed,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=20),
        id="ensure_sale_menu_seed_once",
        misfire_grace_time=3600,
    )
    # IAM-02 yedeği: rol izin geçişi açılışta (server.lifespan) çalışır; o an DB hatası olduysa
    # burada bir kez daha denenir (bayrak 'iam_least_privilege_v1' yazılmışsa hiçbir şey yapmaz).
    _add(
        _ensure_least_privilege_grants,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=30),
        id="ensure_least_privilege_grants_once",
        misfire_grace_time=3600,
    )
    # Tek seferlik (2026-09-29): ürün slug'ları {ad}-{ürün kart id}.
    _add(
        _ensure_slug_kart_id,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=90),
        id="ensure_slug_kart_id_once",
        misfire_grace_time=3600,
    )
    # Tek seferlik (2026-09-29): Site fiyat yükleme listesi + İNDİRİM kategorisi.
    _add(
        _ensure_fiyat_yukleme_0929,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=85),
        id="ensure_fiyat_yukleme_0929_once",
        misfire_grace_time=3600,
    )
    # Tek seferlik (2026-09-29): Body hariç tüm ürünlerde indirimli fiyat temizliği.
    _add(
        _ensure_indirimli_temizle_body_haric,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=80),
        id="ensure_indirimli_temizle_body_haric_once",
        misfire_grace_time=3600,
    )
    # Tek seferlik güvenlik (2026-09-29): panelden sabit/boş şifreyle açılmış üye hesapları.
    _add(
        _ensure_zayif_uye_sifre_temizle,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=100),
        id="ensure_zayif_uye_sifre_temizle_once",
        misfire_grace_time=3600,
    )
    # Tek seferlik (2026-09-30): Vela Kareli Midi Etek Bej XXS eski beden farkı.
    _add(
        _ensure_vela_beden_fark_duzelt,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=60),
        id="ensure_vela_beden_fark_duzelt_once",
        misfire_grace_time=3600,
    )
    # Tek seferlik (2026-09-30): kart 2802 varyant rengi Bej → Siyah.
    _add(
        _ensure_2802_varyant_renk_siyah,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=60),
        id="ensure_2802_varyant_renk_siyah_once",
        misfire_grace_time=3600,
    )
    # Tek seferlik (2026-09-30): Noctia Pelerinli Ceket Somon ürün rengi "Ceket Somon" → "Somon".
    _add(
        _ensure_noctia_somon_renk,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=60),
        id="ensure_noctia_somon_renk_once",
        misfire_grace_time=3600,
    )
    # Tek seferlik (2026-09-30): Havlu Kumaş Etek Bej/Pembe tüm bedenler 950.
    _add(
        _ensure_havlu_etek_950,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=60),
        id="ensure_havlu_etek_950_once",
        misfire_grace_time=3600,
    )
    # Tek seferlik (2026-09-30): Vera Ceket imalat kaydı — stok tekrar eklenmesin.
    _add(
        _ensure_vera_imalat_stok_kilidi,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=60),
        id="ensure_vera_imalat_stok_kilidi_once",
        misfire_grace_time=3600,
    )
    # Tek seferlik (2026-10-01): Volanlı Bluz ailesini Amazon'a yeniden gönder + durum oku.
    _add(
        _ensure_amazon_volanli_bluz_yeniden,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=90),
        id="ensure_amazon_volanli_bluz_yeniden_once",
        misfire_grace_time=3600,
    )
    # Gece 04:20 (TR): 30 günden eski dekont + manuel fatura dosyaları → gizli R2 arşivi.
    _add(
        _run_private_archive,
        "cron",
        hour=1, minute=20,
        id="private_archive_nightly",
        max_instances=1,
        misfire_grace_time=3600,
        replace_existing=True,
    )
    # 15 dk'da bir: ödenmiş kart siparişlerinde kaçırılan onay bildirimi / kupon kaydı (idempotent).
    _add(
        sweep_paid_order_side_effects,
        "cron",
        minute="7,22,37,52",
        id="sweep_paid_order_side_effects",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
        replace_existing=True,
    )
    # Kullanıcı onayı (2026-10-03): base64 görsel/video → Cloudflare R2 (bitene dek her açılışta devam).
    _add(
        _run_r2_files_migration,
        "interval",
        minutes=30,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=150),
        id="r2_files_migration_once",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    # Erken erişim bitişi → pazaryerine anında stok+fiyat (kullanıcı talebi), 2 dk'da bir kontrol.
    _add(
        _release_early_access_to_marketplaces,
        "interval",
        minutes=2,
        id="early_access_release",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )
    # Görsel denetimi + onarımı (kullanıcı talebi): açılışta (+4 dk) ve günde bir.
    _add(
        _run_image_audit_job,
        "interval",
        hours=24,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=240),
        id="image_audit_repair",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    # Her açılışta (+5 dk): canlı güvenlik öz-testi — arşiv, denetim zinciri, şifreli sırlar, ortam
    # değişkenleri, acil anahtarlar, veri koruma sayıları; sonuç Güvenlik Paneli + e-posta (günde 1 / hata).
    try:
        from security.selftest import run_selftest_on_boot
        _add(run_selftest_on_boot, "date", run_date=datetime.now(timezone.utc) + timedelta(seconds=300),
             id="security_selftest_boot", misfire_grace_time=3600, replace_existing=True)
        _add(run_selftest_on_boot, "cron", hour=5, minute=13, id="security_selftest_daily",
             max_instances=1, coalesce=True, replace_existing=True)
    except Exception as _se:
        logger.warning(f"[scheduler] security_selftest kaydedilemedi: {_se}")
    # Tek seferlik (2026-10-04, kullanıcı isteği): boş test siparişi W12254 silinir (arşive).
    _add(
        _ensure_w12254_test_siparisi_sil,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=50),
        id="ensure_w12254_test_siparisi_sil_once",
        misfire_grace_time=3600,
    )
    # Tek seferlik (2026-10-04, kullanıcı isteği): güvenlik uyarısı alıcı/gönderen adresleri (boşsa).
    _add(
        _ensure_guvenlik_uyari_adresleri,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=40),
        id="ensure_guvenlik_uyari_adresleri_once",
        misfire_grace_time=3600,
    )
    # Kritik uyarı çağrısı: yetkili panele girene kadar tekrar SMS (her dakika bakılır; aralık kuralda).
    try:
        from security.alert_paging import paging_tick
        _add(paging_tick, "interval", minutes=1, id="security_alert_paging", max_instances=1, coalesce=True,
             replace_existing=True)
    except Exception as _pg:
        logger.warning(f"[scheduler] security_alert_paging kaydedilemedi: {_pg}")
    # Tek seferlik (2026-10-05, kullanıcı isteği): kritik güvenlik uyarısı SMS numarası (boşsa/eksikse).
    _add(
        _ensure_guvenlik_sms_numarasi,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=45),
        id="ensure_guvenlik_sms_numarasi_once",
        misfire_grace_time=3600,
    )
    # 15 dk'da bir: denetim kayıtları (hash zinciri) doğrulanıp gizli R2 arşivine JSONL olarak aktarılır.
    try:
        from activity_audit import export_audit_to_archive
        _add(export_audit_to_archive, "interval", minutes=15, id="audit_chain_export",
             next_run_time=datetime.now(timezone.utc) + timedelta(seconds=300),
             max_instances=1, coalesce=True, replace_existing=True)
    except Exception as _ae:
        logger.warning(f"[scheduler] audit_chain_export kaydedilemedi: {_ae}")
    # ── Güvenlik / mutabakat işleri (2026-10-04; YENİ işler — mevcut işlerin sıklığı değişmedi) ──
    # T17: entegrasyon tazelik alarmı (15 dk).
    # RAPOR VERİ HAVUZU + DENETÇİ: 30 dk'da bir son 62 gün tazelenir ve raporlar havuzla
    # karşılaştırılır; gece 03:40'ta 5 Haziran'dan itibaren TAM yeniden kurulur. Açılıştan 4 dk sonra ilk tur.
    _add(_run_report_ledger, "interval", minutes=30, id="report_ledger_incremental",
         max_instances=1, coalesce=True, misfire_grace_time=600,
         next_run_time=datetime.now(timezone.utc) + timedelta(minutes=4))
    _add(_run_report_ledger_full, "cron", hour=3, minute=40, id="report_ledger_full",
         max_instances=1, coalesce=True, misfire_grace_time=3600)
    _add(check_integration_freshness, "interval", minutes=15, id="integration_freshness_check",
         next_run_time=datetime.now(timezone.utc) + timedelta(minutes=10),
         max_instances=1, coalesce=True, replace_existing=True)
    # T19/ADM-08: vitrin bütünlük izleyicisi (15 dk) — Cloudflare Pages'in GERÇEKTEN sunduğu index.html,
    # CSP başlığı ve JS paket özetleri; karantinadaki sürüm sunulursa kritik uyarı (YENİ iş).
    try:
        from security.storefront_integrity import run_storefront_integrity_check
        _add(run_storefront_integrity_check, "interval", minutes=15, id="storefront_integrity_watch",
             next_run_time=datetime.now(timezone.utc) + timedelta(seconds=420),
             max_instances=1, coalesce=True, replace_existing=True)
    except Exception as _sie:
        logger.warning(f"[scheduler] storefront_integrity_watch kaydedilemedi: {_sie}")
    # CHAIN07: denetim outbox'ı (yazılamayan best-effort denetim kayıtları) zincire aktarılır (5 dk).
    try:
        from security.audit_outbox import flush_audit_outbox
        _add(flush_audit_outbox, "interval", minutes=5, id="audit_outbox_flush",
             next_run_time=datetime.now(timezone.utc) + timedelta(seconds=200),
             max_instances=1, coalesce=True, replace_existing=True)
    except Exception as _oe:
        logger.warning(f"[scheduler] audit_outbox_flush kaydedilemedi: {_oe}")
    # COM12: iyzico ödeme mutabakatı — son 3 günün ödenmiş kart siparişleri (günlük 04:23 UTC).
    try:
        from security.reconcile_iyzico import run_iyzico_reconciliation
        _add(run_iyzico_reconciliation, "cron", hour=4, minute=23, id="iyzico_daily_reconciliation",
             max_instances=1, coalesce=True, replace_existing=True)
    except Exception as _re:
        logger.warning(f"[scheduler] iyzico_daily_reconciliation kaydedilemedi: {_re}")
    # COM12: iyzico komisyon/hesaba-geçiş defteri — önceki 2 TR gününün işlem raporu (günlük 05:41 UTC,
    # salt-okunur; YENİ iş — mevcut işlerin sıklığı değişmedi).
    try:
        from security.payment_ledger import run_payment_ledger
        _add(run_payment_ledger, "cron", hour=5, minute=41, id="iyzico_payment_ledger_daily",
             max_instances=1, coalesce=True, replace_existing=True)
    except Exception as _ple:
        logger.warning(f"[scheduler] iyzico_payment_ledger_daily kaydedilemedi: {_ple}")
    # CHAIN08: geri yükleme sonrası iade kilidi — yarıda kalan/başlamamış mutabakatı sürdür (30 dk).
    try:
        from security_switches import resume_interrupted_post_restore_reconciliation
        _add(resume_interrupted_post_restore_reconciliation, "interval", minutes=30,
             id="post_restore_refund_hold_resume", max_instances=1, coalesce=True, replace_existing=True)
    except Exception as _rh:
        logger.warning(f"[scheduler] post_restore_refund_hold_resume kaydedilemedi: {_rh}")
    # ADM-13/PRIV04: pazarlama ret listesi (HMAC'li) gizli R2 arşivine (gece 03:37 UTC).
    try:
        from routes.email_marketing import archive_marketing_suppressions
        _add(archive_marketing_suppressions, "cron", hour=3, minute=37, id="kvkk_suppression_archive",
             max_instances=1, coalesce=True, replace_existing=True)
    except Exception as _me:
        logger.warning(f"[scheduler] kvkk_suppression_archive kaydedilemedi: {_me}")
    # SOC-01/02: güvenlik kayıt/uyarı hattı kesinti kontrolü (5 dk) — security/selftest.py.
    try:
        from security.selftest import run_security_pipeline_check
        _add(run_security_pipeline_check, "interval", minutes=5, id="security_pipeline_check",
             next_run_time=datetime.now(timezone.utc) + timedelta(seconds=330),
             max_instances=1, coalesce=True, replace_existing=True)
    except Exception as _pe:
        logger.warning(f"[scheduler] security_pipeline_check kaydedilemedi: {_pe}")
    # SOC-01: onaylanmayan kritik uyarının "YANIT YOK" yükseltmesi (5 dk) + aylık alarm tatbikatı
    # (her ayın ilk Pazartesi 10:07 Europe/Istanbul; İşletme Kuralı security.monthly_drill_enabled).
    # YENİ işler — mevcut işlerin sıklığı değişmedi.
    try:
        from security.alert_ack import run_alert_escalation_check, run_monthly_security_drill
        _add(run_alert_escalation_check, "interval", minutes=5, id="security_alert_escalation",
             next_run_time=datetime.now(timezone.utc) + timedelta(seconds=270),
             max_instances=1, coalesce=True, replace_existing=True)
        _add(run_monthly_security_drill, "cron", day="1st mon", hour=10, minute=7, timezone="Europe/Istanbul",
             id="security_monthly_drill", max_instances=1, coalesce=True, replace_existing=True)
    except Exception as _sae:
        logger.warning(f"[scheduler] security_alert_escalation/security_monthly_drill kaydedilemedi: {_sae}")
    # Gece: KVKK silme kayıtları (tombstone) yeniden uygulanır — yedekten geri dönüşte silinmiş
    # kişinin verisi/pazarlama izni geri canlanmasın.
    try:
        from routes.kvkk import reapply_deletion_tombstones
        _add(reapply_deletion_tombstones, "cron", hour=3, minute=47, id="kvkk_tombstone_reapply",
             max_instances=1, coalesce=True, replace_existing=True)
    except Exception as _ke:
        logger.warning(f"[scheduler] kvkk_tombstone_reapply kaydedilemedi: {_ke}")
    # 5 dk'da bir: bekleme süresi dolan IBAN değişiklikleri devreye alınır.
    try:
        from routes.settings import promote_due_bank_changes
        _add(promote_due_bank_changes, "interval", minutes=5, id="iban_cooling_promote",
             next_run_time=datetime.now(timezone.utc) + timedelta(seconds=120),
             max_instances=1, coalesce=True, replace_existing=True)
    except Exception as _ie:
        logger.warning(f"[scheduler] iban_cooling_promote kaydedilemedi: {_ie}")
    # Açılışta: gizli arşiv deposu erişim testi (depo tanımlıysa; sonuç Entegrasyon Logları).
    _add(
        _ensure_arsiv_erisim_testi,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=45),
        id="ensure_arsiv_erisim_testi_once",
        misfire_grace_time=3600,
    )
    # Tek seferlik v2 (2026-10-06): aynı düzeltme + iade kayıtları senkron ezmesine karşı kilitli.
    _add(
        _ensure_hb4029_iade_siparis_tutar_duzelt_v2,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=75),
        id="ensure_hb4029_iade_siparis_tutar_duzelt_v2_once",
        misfire_grace_time=3600,
    )
    # Tek seferlik (2026-10-03, kullanıcı onayı): HB4029270518 iade kayıtları + sipariş kalemleri.
    _add(
        _ensure_hb4029_iade_siparis_tutar_duzelt,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=60),
        id="ensure_hb4029_iade_siparis_tutar_duzelt_once",
        misfire_grace_time=3600,
    )
    # Tek seferlik (2026-10-02, kullanıcı onayı): HB4029270518 gider pusulası tutarı 2 × 3.056,25.
    _add(
        _ensure_hb4029_pusula_tutar_duzelt,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=60),
        id="ensure_hb4029_pusula_tutar_duzelt_once",
        misfire_grace_time=3600,
    )
    # Tek seferlik (2026-10-02): HB4029270518 gider pusulasını yeni faturaya bağla.
    _add(
        _ensure_hb4029_gider_pusulasi_duzelt,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=60),
        id="ensure_hb4029_gider_pusulasi_duzelt_once",
        misfire_grace_time=3600,
    )
    # Tek seferlik (2026-10-02): boş SEO varsayılanlarını doldur (elle girilenlere dokunmaz).
    _add(
        _ensure_seo_varsayilan_doldur,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=60),
        id="ensure_seo_varsayilan_doldur_once",
        misfire_grace_time=3600,
    )
    # Tek seferlik (2026-10-01): stoklu ama Amazon'da pasif SKU teşhisi (yalnız okuma).
    _add(
        _ensure_amazon_pasif_teshis,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=90),
        id="ensure_amazon_pasif_teshis_once",
        misfire_grace_time=3600,
    )
    # Tek seferlik (2026-10-01): Amazon kataloğunu yeniden çek (yalnız okuma) — son durum raporu.
    _add(
        _ensure_amazon_katalog_tazele,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=90),
        id="ensure_amazon_katalog_tazele_once",
        misfire_grace_time=3600,
    )
    # Tek seferlik (2026-10-01): Amazon kategori eşleme + aktarılmamış ürünleri toplu aktarım.
    _add(
        _ensure_amazon_otomatik_kurulum,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=90),
        id="ensure_amazon_otomatik_kurulum_v14_once",
        misfire_grace_time=3600,
    )
    # Trendyol termin takvimi: 15 dakikada bir kontrol (değişim anında en geç 15 dk içinde uygulanır;
    # durum aynıysa hiçbir şey göndermez). max_instances=1: uzun süren toplu güncelleme üst üste binmez.
    _add(
        _trendyol_delivery_tick,
        "interval",
        minutes=15,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=120),
        id="trendyol_delivery_tick",
        max_instances=1,
        misfire_grace_time=600,
        replace_existing=True,
    )
    # Tek seferlik (2026-09-29): Body %10 (Elen hariç) — v3.
    _add(
        _ensure_body_indirim10_v3,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=95),
        id="ensure_body_indirim10_v3_once",
        misfire_grace_time=3600,
    )
    # Tek seferlik (2026-09-29): v18 etiket → Üye Tipi 1 + Elen Body %10 geri alma.
    _add(
        _ensure_v18_uye_tipi_ve_elen_geri,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=75),
        id="ensure_v18_uye_tipi_ve_elen_geri_once",
        misfire_grace_time=3600,
    )
    # Tek seferlik (2026-09-29): Body kategorisi indirimli fiyat = etiket × 0,90.
    _add(
        _ensure_body_indirim10,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=55),
        id="ensure_body_indirim10_once",
        misfire_grace_time=3600,
    )
    # Tek seferlik (2026-09-29): KARLILIK v18 site fiyatları (yalnız site; pazaryeri sabit).
    _add(
        _ensure_fiyat_guncelleme_v18,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=50),
        id="ensure_fiyat_guncelleme_v18_once",
        misfire_grace_time=3600,
    )
    # Tek seferlik (2026-09-28): HB çok-kalemli siparişlerde ilk-kalem total onarımı.
    _add(
        _ensure_hb_toplam_onarim,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=45),
        id="ensure_hb_toplam_onarim_once",
        misfire_grace_time=3600,
    )
    # Tek seferlik (Kadir isteği 2026-09-26): Body kategorisi 3 al 2 öde kampanyası.
    _add(
        _ensure_body_3al2_kampanya,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=40),
        id="ensure_body_3al2_kampanya_once",
        misfire_grace_time=3600,
    )
    # Tek seferlik (Kadir onaylı 2026-09-24): hatalı damgalanan 116 Trendyol iptal tarihi.
    _add(
        _ensure_ty_iptal_tarihi_onarim,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=80),
        id="ensure_ty_iptal_tarihi_onarim_once",
        misfire_grace_time=3600,
    )
    # Tek seferlik (Kadir onaylı): Trendyol mutabakatı — 21 eski siparişin tarihi + 1 talep.
    _add(
        _ensure_ty_mutabakat_fix,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=70),
        id="ensure_ty_mutabakat_fix_once",
        misfire_grace_time=3600,
    )
    # Tek seferlik: geçmiş iptallerin tarihini kanıttan çıkar ve DONDUR.
    # SIRA ÖNEMLİ: iptal damgası basabilen TÜM işlerden (Trendyol iptal taraması +30 sn,
    # pazaryeri senkron tick'i +45 sn) ÖNCE çalışmalı; aksi halde onların "şimdi"
    # damgası kanıta dayalı tarihin yerini kapar (2026-09-23 olayı). Bayrak 'done'
    # olduğunda no-op; bu sıralama yeni kurulumlar içindir.
    _add(
        _ensure_cancelled_at_backfill,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=10),
        id="ensure_cancelled_at_backfill_once",
        misfire_grace_time=3600,
    )
    _add(
        _ensure_restock_delta_backfill,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=35),
        id="ensure_restock_delta_backfill_once",
        # Açılış yavaşsa iş düşmesin: APScheduler varsayılan gecikme
        # toleransı 1 sn ve "date" tetikleyicisi bir daha çalışmaz.
        misfire_grace_time=3600,
    )
    # Trendyol İPTAL HIZLI tarama — her 5 DK (eskiden 60 sn idi; site yavaşlamasının
    # sebebi buydu). Sadece Cancelled + 14g dar pencere → hafif. Yeni iptaller ~5 dk'da düşer.
    _add(
        _run_trendyol_cancel_pass,
        "interval",
        minutes=5,
        id="trendyol_cancel_pass_5m",
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=30),
        max_instances=1,
        coalesce=True,
    )
    # Hepsiburada fatura KALICI oto-yükleme — her 10 DK (eskiden 2 dk; yüklenemeyen siparişler her turda
    # tam belge olarak yeniden okunuyordu → Atlas veri transferi). Faturası kesilmiş ama HB'ye gitmemiş
    # siparişleri yeniden gönderir → fatura kesildikten en geç ~10 dk sonra gider. İdempotent.
    _add(
        _run_hb_invoice_autoheal,
        "interval",
        minutes=10,
        id="hb_invoice_autoheal_2m",
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=30),
        max_instances=1,
        coalesce=True,
    )
    # Trendyol GENİŞ durum taraması — SAATTE BİR. Cancelled 45g + Returned/UnDelivered 30g.
    # Sık taramanın kaçırdığı eski/geç durum değişikliklerini kapatır (sık değil → hafif).
    _add(
        _run_trendyol_status_wide_pass,
        "interval",
        minutes=60,
        id="trendyol_status_wide_60m",
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=3),
        max_instances=1,
        coalesce=True,
    )
    # Trendyol claims (iade/iptal) senkronu — her 30 DK. Müşterinin seçtiği GERÇEK iptal/iade
    # sebebini çeker ve claim'leri eşleşen iptal siparişlerine bağlar.
    _add(
        _run_trendyol_claims_sync,
        "interval",
        minutes=30,
        id="trendyol_claims_sync_30m",
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=120),
        max_instances=1,
        coalesce=True,
    )
    # AÇIK iade kovaları CANLI eşitleme — DAKİKADA BİR (kullanıcı isteği): Talep Oluşturulan /
    # Kargoya Verilen / Aksiyon Bekleyen sayıları TY panelle aynı; kapanan claim Onaylanan/
    # Reddedilen'e anında taşınır. Hafif iş: statü filtreli birkaç sayfa + artık başına tekil sorgu.
    _add(
        _run_trendyol_open_claims_refresh,
        "interval",
        minutes=1,
        id="trendyol_open_claims_1m",
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=90),
        max_instances=1,
        coalesce=True,
    )
    # Hepsiburada claims (iade) senkronu — her 30 DK (Trendyol ile simetrik).
    _add(
        _run_hepsiburada_claims_sync,
        "interval",
        minutes=30,
        id="hepsiburada_claims_sync_30m",
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=150),
        max_instances=1,
        coalesce=True,
    )
    # Amazon claims (iade) senkronu — her 30 DK (HB ile simetrik; rapor bekleme ~1-3 dk).
    _add(
        _run_amazon_claims_sync,
        "interval",
        minutes=30,
        id="amazon_claims_sync_30m",
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=240),
        max_instances=1,
        coalesce=True,
    )
    # Tek seferlik (Kadir açık isteği 2026-10-15): bekleyen/inceleme/kilitli fatura isteklerini temizle.
    _add(_bekleyen_fatura_temizle_once, "date",
         run_date=datetime.now(timezone.utc) + timedelta(seconds=150),
         id="bekleyen_fatura_temizle_once_v1", misfire_grace_time=3600)
    # Tek seferlik (Kadir açık isteği): HB4029270518 hatalı faturasını Doğan'da iptal et.
    _add(
        _ensure_hb4029_fatura_iptal,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=90),
        id="ensure_hb4029_fatura_iptal_once_v2",
        misfire_grace_time=3600,
    )
    # Tek seferlik canlı test (2026-09-29) tamamlandı: 'ac' artık kurulmaz; 'kapat' yalnız testin
    # kendi açtığı işareti (biz_actik) geri alır — kalmış bir işaret varsa temizler, yoksa no-op.
    _add(_members_only_canli_test, "date", args=["kapat"],
         run_date=datetime.now(timezone.utc) + timedelta(seconds=720),
         id="members_only_canli_test_kapat", misfire_grace_time=3600)
    # Beden kimliği kuralı (id = 4 haneli urun_id, tekil) — 6 saatte bir + açılıştan 150 sn sonra.
    _add(
        _run_varyant_id_normalize,
        "interval",
        hours=6,
        id="varyant_id_normalize",
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=150),
        max_instances=1,
        coalesce=True,
    )
    # İptal edilen faturaların doğru tutarla yeniden kesimi (listeli siparişler, 10 dk).
    _add(
        _run_fatura_yeniden_kesim,
        "interval",
        minutes=10,
        id="fatura_yeniden_kesim",
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=120),
        max_instances=1,
        coalesce=True,
    )
    # Pasif / silinmiş ürünlerin pazaryeri stoğu 0 (30 dk + açılıştan 100 sn sonra).
    _add(
        _run_passive_marketplace_zero_sweep,
        "interval",
        minutes=30,
        id="passive_marketplace_zero_sweep",
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=100),
        max_instances=1,
        coalesce=True,
    )
    # Trendyol hayalet ürün sıfırlama — Trendyol'da olup sistemde olmayan barkodlar 0 stok (günlük).
    _add(
        _run_trendyol_orphan_zero,
        "interval",
        hours=2,
        id="trendyol_orphan_zero_daily",
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=3),
        max_instances=1,
        coalesce=True,
    )
    # Trendyol claims TEK SEFERLİK derin backfill (3 yıl) — geçmiş onaylı/reddedilen iadeler
    # de sekme sayılarına insin. Flag korumalı (trendyol_claims_deep_backfill.done) → bir kez.
    _add(
        _run_trendyol_claims_deep_backfill_once,
        "interval",
        hours=6,
        id="trendyol_claims_deep_backfill_once",
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=6),
        max_instances=1,
        coalesce=True,
    )
    # Trendyol TEK SEFERLİK terminal backfill — geçmişteki TÜM iptalleri (satıcı dahil) taşır.
    # Flag korumalı (db.settings.trendyol_terminal_backfill.done) → bir kez çalışır, sonra no-op.
    # 3 saatte bir tetiklenir ama done ise hiçbir şey yapmaz (sadece ucuz flag kontrolü).
    _add(
        _run_trendyol_deep_backfill_once,
        "interval",
        hours=3,
        id="trendyol_deep_backfill_once",
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=4),
        max_instances=1,
        coalesce=True,
    )
    # O12: "Günde bir" işleri SABİT SAATLİ cron ile çalıştır (07:00 UTC ≈ 10:00 İstanbul).
    # Önceki `interval hours=24 + next_run_time=now+2dk` her PROCESS RESTART'ında çalışıyor ve
    # düşük-stok e-postası sent-flag'i olmadığından her deploy'da MÜKERRER mail gidiyordu; ayrıca
    # "günlük" saat her restart'ta kayıyordu. cron ile gün içinde tam olarak bir kez tetiklenir.
    # Terkedilmiş sepet e-postası: 30 dk'da bir tarar (12 saatlik gecikme dakika hassasiyetinde
    # tutulsun diye). Gönderim bayrağı sepet + üye bazında → restart/deploy mükerrer mail üretmez.
    _add(
        _send_abandoned_cart_reminders,
        "interval",
        minutes=30,
        id="abandoned_cart_reminders",
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=5),
        max_instances=1,
        coalesce=True,
    )
    _add(
        _send_daily_stock_alert,
        "cron",
        hour=7, minute=10,
        id="daily_stock_alert",
        max_instances=1,
        coalesce=True,
    )
    # Favori ürün tekrar stokta — her 30 dk favorilenen ürünlerin stoğunu kontrol eder;
    # 'yok'→'var' geçişinde o ürünü favorileyenlere markalı e-posta atar (döngü başına 1 kez).
    _add(
        _notify_back_in_stock,
        "interval",
        minutes=30,
        id="wishlist_back_in_stock",
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=2),
        max_instances=1,
        coalesce=True,
    )
    # Ticimax sipariş senkronu KALDIRILDI (kullanıcı, 2026-10-04: "ticimax sipariş senkronunu sil").
    # Ticimax canlı stok senkronu — KAPALI (Atilla stok master'ı; Ticimax stoğu
    # bu sistemi EZMESİN). Kullanıcı kararı: stok yalnızca sipariş/iptal/iade ile
    # Atilla içinde yönetilir. Gerekirse settings.ticimax_stock_sync_enabled=true
    # yapıp elle tetiklenebilir, ama otomatik job artık çalışmaz.
    # _scheduler.add_job(
    #     _ticimax_sync_stock, "interval", hours=2, id="ticimax_stock_sync",
    #     next_run_time=datetime.now(timezone.utc) + timedelta(minutes=10),
    #     max_instances=1, coalesce=True,
    # )
    # Iter 43 — Günlük stok tükenme uyarısı (her gün sabah 9:00 UTC, ~12:00 TR)
    async def _daily_stockout_alert():
        try:
            from routes.production_hooks import send_stockout_alert_email
            class _SystemAdmin:
                def get(self, k, *a): return "system@atilla.com" if k == "email" else None
            await send_stockout_alert_email(admin=_SystemAdmin())
            logger.info("[scheduler] daily stockout alert sent")
        except Exception as e:
            logger.warning(f"[scheduler] stockout alert failed: {e}")
    from apscheduler.triggers.cron import CronTrigger
    _add(
        _daily_stockout_alert,
        CronTrigger(hour=9, minute=0),
        id="daily_stockout_alert",
        max_instances=1, coalesce=True,
    )
    # Amazon SP-API — siparişleri panele otomatik çek (her 2 dk). Yapılandırılmamışsa
    # fonksiyon sessizce no-op. LastUpdatedAfter penceresi hem yeni siparişi hem iptal/kargo
    # durum değişimini yakalar; yeni MFN siparişte stok düşer, FBA'da düşmez.
    _add(
        _run_amazon_auto_orders_pull,
        "interval",
        minutes=2,
        id="amazon_orders_sync",
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=1),
        max_instances=1,
        coalesce=True,
    )
    # Amazon SP-API — stok/fiyat CANLI push (her 3 dk; Trendyol/HB ile aynı — eskiden 1 dk, her turda
    # tüm aktif ürünler okunuyordu → Atlas veri transferinin en büyük kalemi). Yalnız stoğu DEĞİŞEN
    # varyantları Amazon listing'ine yollar. AMAZON_ALLOW_WRITE=0 iken dry-run. Yapılandırılmamışsa no-op.
    _add(
        _run_amazon_auto_stock_sync,
        "interval",
        minutes=3,
        id="amazon_stock_sync",
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=1),
        max_instances=1,
        coalesce=True,
    )
    # KVKK — TCKN/VKN at-rest şifreleme: tek seferlik backfill (boot+90 sn, settings bayraklı,
    # 200'lük partiler, kaldığı yerden sürer) + her gece güvenlik ağı (kaçan düz değeri yakalar).
    _add(
        encrypt_existing_tax_ids,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=90),
        id="pii_tax_id_backfill_once",
        misfire_grace_time=3600,
        max_instances=1,
    )
    _add(
        encrypt_existing_tax_ids_nightly,
        CronTrigger(hour=1, minute=47),
        id="pii_tax_id_nightly_net",
        max_instances=1, coalesce=True,
    )
    # KVKK — telefon/adres/IBAN/TC at-rest şifreleme (influencer, PR, adres defteri, üye telefonu):
    # tek seferlik backfill (boot+150 sn, bayraklı, kaldığı yerden sürer) + her gece güvenlik ağı.
    # YENİ işler — mevcut işlerin sıklığı DEĞİŞMEDİ.
    _add(
        encrypt_existing_contact_pii,
        "date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=150),
        id="pii_contact_backfill_once",
        misfire_grace_time=3600,
        max_instances=1,
    )
    _add(
        encrypt_existing_contact_pii_nightly,
        CronTrigger(hour=1, minute=53),
        id="pii_contact_nightly_net",
        max_instances=1, coalesce=True,
    )
    # KVKK — saklama süresi dolan kapanmış siparişlerde iletişim verisi minimizasyonu (her gece 03:23 UTC).
    # İlk çalışma KURU (rapor e-postası), sonrakiler etkin. YENİ iş.
    _add(
        minimize_old_order_contact_pii,
        CronTrigger(hour=3, minute=23),
        id="order_pii_retention_minimize",
        max_instances=1, coalesce=True,
    )
    # Amazon DPP — PII saklama süresi dolan siparişlerde kişisel verileri anonimleştir
    # (her gün 03:00 UTC). Amazon "Restricted" rol uyumu için kritik kontrol.
    _add(
        _pii_retention_purge,
        CronTrigger(hour=3, minute=0),
        id="pii_retention_purge",
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=3),
        max_instances=1, coalesce=True,
    )
    # §7 — Güvenlik/audit/SP-API log retention: ≥12 ay sakla, aşanı temizle. Haftalık (Pzt 04:00 UTC).
    _add(
        _security_log_retention_purge,
        CronTrigger(day_of_week="mon", hour=4, minute=0),
        id="security_log_retention_purge",
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=5),
        max_instances=1, coalesce=True,
    )
    # DHL/MNG kargo durum taramasi — her 30 dk (site siparisleri; takip linki -> Kargoya Verildi, teslim -> Teslim Edildi)
    _add(
        _dhl_cargo_poll_tick,
        "interval",
        minutes=30,
        id="dhl_cargo_poll",
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=60),
        max_instances=1,
        coalesce=True,
    )
    # Influencer PR kargo takip no — her SAAT otomatik (ilk tur 2 dk sonra)
    _add(
        _influencer_pr_tracking_tick,
        "interval",
        minutes=60,
        id="influencer_pr_tracking_hourly",
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=2),
        max_instances=1,
        coalesce=True,
    )
    _add(
        _return_cargo_poll_tick,
        "interval",
        minutes=5,
        id="return_cargo_poll",
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=90),
        max_instances=1,
        coalesce=True,
    )
    # Bölüm C — Referans ödülleri (davet edilenin ilk siparişi geldiyse) her 20 dk.
    _add(
        _run_award_referrals,
        "interval",
        minutes=20,
        id="referral_award",
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=120),
        max_instances=1,
        coalesce=True,
    )
    # C3 — Sadakat puanları: ödenmiş siparişlere puan + iptalde geri alma, her 30 dk.
    _add(
        _run_award_loyalty,
        "interval",
        minutes=30,
        id="loyalty_award",
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=180),
        max_instances=1,
        coalesce=True,
    )
    # Bölüm C — Doğum günü kuponları: her gün 07:00 UTC (~10:00 TR).
    _add(
        _run_birthday_coupons,
        "cron",
        hour=7,
        minute=0,
        id="birthday_coupons",
        max_instances=1,
        coalesce=True,
    )
    # COM11 — Ödeme itirazı (chargeback) son tarih hatırlatması: her gün 06:10 UTC (~09:10 TR) +
    # açılıştan 4 dk sonra bir kez (itiraz başına günde en çok 1 uyarı — reminder_sent_on).
    _add(
        _run_chargeback_deadline_reminders,
        "cron",
        hour=6,
        minute=10,
        id="chargeback_deadline_reminders",
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=4),
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    logger.info("[scheduler] Background scheduler started (auto-cancel every 30 min + marketplace auto-sync every 1 min + abandoned cart e-mail every 30 min; Ticimax sync disabled)")
    return _scheduler


def shutdown_scheduler():
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            pass
        _scheduler = None
