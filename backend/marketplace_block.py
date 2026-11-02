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

_STATE = {"keys": frozenset(), "ts": 0.0, "products": 0}
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
        _STATE.update({"keys": frozenset(keys), "ts": now, "products": n})
    except Exception as e:  # hata halinde önceki küme korunur (engel sessizce kalkmaz)
        logger.warning(f"[mp-block] engel listesi yenilenemedi: {e}")
    return _STATE["keys"]


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


def item_is_blocked(item: Any, keys: Optional[frozenset] = None) -> bool:
    """Kalemin herhangi bir metin alanı (barkod/SKU/stok kodu...) engelli anahtarla eşleşirse True."""
    keys = blocked_keys() if keys is None else keys
    if not keys:
        return False
    return any(_norm(s) in keys for s in _strings(item))


def filter_items(items: List[Any], where: str = "") -> Tuple[List[Any], int]:
    """Engelli kalemleri çıkarır → (kalan, atlanan_sayısı)."""
    keys = blocked_keys()
    if not keys or not items:
        return list(items or []), 0
    keep = [it for it in items if not item_is_blocked(it, keys)]
    skipped = len(items) - len(keep)
    if skipped:
        logger.info(f"[mp-block] {where}: üyelere özel {skipped} kalem pazaryerine GÖNDERİLMEDİ")
    return keep, skipped


def skipped_response(skipped: int) -> dict:
    return {"skipped_members_only": skipped, "items": [], "message": "Üyelere özel (erken erişim) ürünler pazaryerine gönderilmedi."}


_QTY_KEYS = ("quantity", "stock", "availableStock", "stockQuantity")


def zero_stock_items(items: List[Any], where: str = "") -> Tuple[List[Any], int]:
    """Üyelere özel kalemleri ÇIKARMAZ; ürün olarak gönderilir ama stok alanları 0'a çekilir
    (kullanıcı kararı: Trendyol'a ürün aktarılsın, erken erişim bitene kadar satılamasın)."""
    keys = blocked_keys()
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
