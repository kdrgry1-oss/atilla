"""
ÜYELERE ÖZEL (erken erişim) ürünler için PAZARYERİ GÖNDERİM ENGELİ.

Kural (kullanıcı talebi): Bir ürün, ŞU AN üyelere özel olan (members_only + bitiş tarihi
geçmemiş) herhangi bir kategoride ise — başka kategorilerde de olsa — Trendyol, Hepsiburada,
Amazon, n11, Temu'ya HİÇBİR veri (ürün oluşturma/güncelleme, stok, fiyat, arşiv, stok-0)
GÖNDERİLMEZ. Pencere bitince engel kalkar; periyodik senkronlar veriyi göndermeye başlar.

Engel, istemcilerin HTTP'ye çıktığı son noktada (taşıma katmanı) uygulanır: hangi akış
(cron, manuel buton, sipariş sonrası anlık itme, pasif/yetim stok-0 süpürmesi) çağırırsa
çağırsın engelli kalem gönderilmez. Anahtarlar: barkod, stok kodu, varyant SKU'ları,
Hepsiburada merchantSku (yapılandırılmış kaynağa göre), Amazon SellerSKU ve sku eşleşmeleri.
"""
import logging
import time
from typing import Any, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

_STATE = {"keys": frozenset(), "members": frozenset(), "nonxml": frozenset(), "ts": 0.0, "products": 0,
          "nonxml_products": 0}
XML_SOURCE = "ticimax_xml"
_TTL = 60.0


def _norm(v: Any) -> str:
    return str(v or "").strip().lower()


async def refresh_blocked(force: bool = False) -> frozenset:
    """Engelli anahtar kümesini DB'den yeniden hesaplar (≤60 sn önbellek)."""
    now = time.monotonic()
    if not force and _STATE["ts"] and now - _STATE["ts"] < _TTL:
        return _STATE["keys"]
    try:
        from routes.deps import db
        from routes.products import _members_only_cat_ids, _mo_forms
        mo = await _members_only_cat_ids()
        keys: set = set()
        n = 0
        if mo:
            forms = _mo_forms(mo)
            q = {"is_deleted": {"$ne": True}, "$or": [
                {"category_id": {"$in": forms}}, {"category_ids": {"$in": forms}}, {"categories": {"$in": forms}}]}
            try:
                from routes.integrations_hepsiburada import _hb_sku_source, _hb_variant_sku
                hb_src = await _hb_sku_source()
            except Exception:
                _hb_variant_sku, hb_src = None, None
            try:
                from routes.amazon_spapi import _amazon_seller_sku, _amz_variants
            except Exception:
                _amazon_seller_sku = _amz_variants = None
            pids = []
            async for p in db.products.find(q, {"_id": 0}):
                n += 1
                pids.append(p.get("id"))
                for k in ("barcode", "stock_code", "sku", "model_code"):
                    if p.get(k):
                        keys.add(_norm(p[k]))
                variants = [v for v in (p.get("variants") or []) if isinstance(v, dict)]
                for v in variants:
                    for k in ("barcode", "stock_code", "sku", "merchant_sku", "hb_sku"):
                        if v.get(k):
                            keys.add(_norm(v[k]))
                if _hb_variant_sku:
                    try:
                        used: set = set()
                        if variants:
                            for vi, v in enumerate(variants):
                                s = _hb_variant_sku(p, v, vi, hb_src, used)
                                if s:
                                    keys.add(_norm(s))
                        else:
                            s = _hb_variant_sku(p, None, 0, hb_src)
                            if s:
                                keys.add(_norm(s))
                    except Exception:
                        pass
                if _amazon_seller_sku and _amz_variants:
                    try:
                        for v in _amz_variants(p):
                            s = _amazon_seller_sku(v, p)
                            if s:
                                keys.add(_norm(s))
                    except Exception:
                        pass
            if pids:
                async for m in db.amazon_sku_map.find({"product_id": {"$in": pids}}, {"_id": 0, "sku": 1}):
                    if m.get("sku"):
                        keys.add(_norm(m["sku"]))
        keys.discard("")
        nonxml, n_nonxml = await _nonxml_keys(db)
        _STATE.update({"members": frozenset(keys), "nonxml": nonxml, "keys": frozenset(keys) | nonxml,
                       "ts": now, "products": n, "nonxml_products": n_nonxml})
    except Exception as e:  # hata halinde önceki küme korunur (engel sessizce kalkmaz)
        logger.warning(f"[mp-block] engel listesi yenilenemedi: {e}")
    return _STATE["keys"]


def _product_keys(p: dict) -> set:
    out: set = set()
    for k in ("barcode", "stock_code", "sku", "model_code", "trendyol_model_code"):
        if p.get(k):
            out.add(_norm(p[k]))
    for v in (p.get("variants") or []):
        if not isinstance(v, dict):
            continue
        for k in ("barcode", "stock_code", "sku", "merchant_sku", "hb_sku", "trendyol_barcode", "amazon_sku"):
            if v.get(k):
                out.add(_norm(v[k]))
    return out


async def _nonxml_keys(db) -> Tuple[frozenset, int]:
    """İşletme Kuralı marketplace.only_xml_products AÇIKSA: XML'den (Ticimax) GELMEYEN ürünlerin tüm
    tanımlayıcıları (barkod/stok kodu/SKU/HB merchantSku/Amazon SellerSKU) — XML ürünleriyle ORTAK olan
    anahtarlar çıkarılır (elle eklenmiş kopya, XML ürününün gönderimini engellemesin)."""
    try:
        from business_rules import get_rule
        if not bool(await get_rule(db, "marketplace.only_xml_products", True)):
            return frozenset(), 0
    except Exception:
        pass   # kural okunamazsa güvenli taraf: engel uygulanır
    try:
        from routes.integrations_hepsiburada import _hb_sku_source, _hb_variant_sku
        hb_src = await _hb_sku_source()
    except Exception:
        _hb_variant_sku, hb_src = None, None
    try:
        from routes.amazon_spapi import _amazon_seller_sku, _amz_variants
    except Exception:
        _amazon_seller_sku = _amz_variants = None
    xml_keys: set = set()
    non: set = set()
    non_pids: list = []
    n = 0
    async for p in db.products.find({}, {"_id": 0}):
        ks = _product_keys(p)
        variants = [v for v in (p.get("variants") or []) if isinstance(v, dict)]
        if _hb_variant_sku:
            try:
                used: set = set()
                for vi, v in enumerate(variants or [None]):
                    s = _hb_variant_sku(p, v, vi, hb_src, used) if v is not None else _hb_variant_sku(p, None, 0, hb_src)
                    if s:
                        ks.add(_norm(s))
            except Exception:
                pass
        if _amazon_seller_sku and _amz_variants:
            try:
                for v in _amz_variants(p):
                    s = _amazon_seller_sku(v, p)
                    if s:
                        ks.add(_norm(s))
            except Exception:
                pass
        if p.get("source") == XML_SOURCE:
            xml_keys |= ks
        else:
            non |= ks
            n += 1
            non_pids.append(p.get("id"))
    if non_pids:
        async for mm in db.amazon_sku_map.find({"product_id": {"$in": non_pids}}, {"_id": 0, "sku": 1}):
            if mm.get("sku"):
                non.add(_norm(mm["sku"]))
    non -= xml_keys
    non.discard("")
    return frozenset(non), n


def blocked_keys() -> frozenset:
    """Senkron erişim (Hepsiburada istemcisi senkron çalışır) — son hesaplanan küme."""
    return _STATE["keys"]


def _strings(obj: Any, depth: int = 0) -> Iterable[str]:
    if depth > 6:
        return
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v, depth + 1)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _strings(v, depth + 1)


def is_blocked_value(v: Any, keys: Optional[frozenset] = None) -> bool:
    keys = blocked_keys() if keys is None else keys
    return bool(keys) and _norm(v) in keys


def _never_refreshed() -> bool:
    """Liste süreç açıldığından beri HİÇ hesaplanmadıysa (ilk ~5 sn) güvenli taraf: gönderme."""
    return not _STATE["ts"]


def item_is_blocked(item: Any, keys: Optional[frozenset] = None) -> bool:
    """Kalemin herhangi bir metin alanı (barkod/SKU/stok kodu...) engelli anahtarla eşleşirse True."""
    if keys is None and _never_refreshed():
        return True
    keys = blocked_keys() if keys is None else keys
    if not keys:
        return False
    return any(_norm(s) in keys for s in _strings(item))


def filter_items(items: List[Any], where: str = "") -> Tuple[List[Any], int]:
    """Engelli kalemleri çıkarır → (kalan, atlanan_sayısı)."""
    if items and _never_refreshed():
        logger.warning(f"[mp-block] {where}: engel listesi henüz hesaplanmadı — {len(items)} kalem GÖNDERİLMEDİ")
        return [], len(items)
    keys = blocked_keys()
    if not keys or not items:
        return list(items or []), 0
    keep = [it for it in items if not item_is_blocked(it, keys)]
    skipped = len(items) - len(keep)
    if skipped:
        logger.info(f"[mp-block] {where}: {skipped} kalem (üyelere özel / XML dışı) pazaryerine GÖNDERİLMEDİ")
    return keep, skipped


def skipped_response(skipped: int) -> dict:
    return {"skipped_members_only": skipped, "items": [], "message": "Üyelere özel ya da XML dışı ürünler pazaryerine gönderilmedi."}


_QTY_KEYS = ("quantity", "stock", "availableStock", "stockQuantity")


def zero_stock_items(items: List[Any], where: str = "") -> Tuple[List[Any], int]:
    """Üyelere özel kalemleri ÇIKARMAZ; ürün olarak gönderilir ama stok alanları 0'a çekilir
    (kullanıcı kararı: Trendyol'a ürün aktarılsın, erken erişim bitene kadar satılamasın)."""
    nonxml = _STATE["nonxml"]
    if nonxml and items:
        before = len(items)
        items = [it for it in items if not item_is_blocked(it, nonxml)]
        if len(items) != before:
            logger.info(f"[mp-block] {where}: XML dışı {before - len(items)} ürün kalemi pazaryerine GÖNDERİLMEDİ")
    keys = _STATE["members"]
    if not keys or not items:
        return list(items or []), 0
    out, n = [], 0
    for it in items:
        if isinstance(it, dict) and item_is_blocked(it, keys):
            it = {**it}
            for k in _QTY_KEYS:
                if k in it:
                    it[k] = 0
            if not any(k in it for k in _QTY_KEYS):
                it["quantity"] = 0
            n += 1
        out.append(it)
    if n:
        logger.info(f"[mp-block] {where}: üyelere özel {n} kalem STOK 0 ile gönderildi")
    return out, n
