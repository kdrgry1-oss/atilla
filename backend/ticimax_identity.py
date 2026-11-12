"""
Ticimax kimliği (URUNID = varyasyon numarası) ile katalog onarımı.

Sorun: pazaryeri barkodu stok kodu+beden ile eşleştirilince, pazaryerindeki beden etiketi Ticimax'takinden
farklı olan ilanlarda yanlış varyanta barkod yazılabiliyor; XML'e girmeyen ürünler de (Ticimax'ta kategori
yolu boş kartlar) Excel'den ayrı ürün olarak açılıp gerçek stoktan kopuyordu.

Çözüm: tek kimlik Ticimax URUNID'dir. Girdi kayıtları (Ticimax ürün Excel'inden + pazaryeri barkod
eşlemesinden üretilir):  {"u": URUNID, "c": UrunKartiID, "k": stok kodu, "b": Ticimax barkodu,
                          "s": stok, "p": KDV dahil fiyat, "a": aktif (0/1), "t": Trendyol barkodu | None}

  1) XML kaynaklı ürünler: varyantın barkodu, kendi URUNID'sinin Trendyol barkoduna DÜZELTİLİR; başka bir
     URUNID'ye ait barkod taşıyorsa (karışma) temizlenir. Stok/fiyat XML senkronundan gelmeye devam eder.
  2) Excel'den oluşturulmuş ürünler: varyant barkodundan URUNID bulunur →
       • o URUNID XML ürünlerinde zaten varsa: mükerrer varyant kaldırılır (barkod XML varyantında),
       • yoksa: varyant Ticimax'a BAĞLANIR (urun_id, Ticimax barkodu, stok ve fiyat Ticimax'tan),
       • Ticimax'ta hiç yoksa: stok 0 kalır.
     Varyantı kalmayan ürün silinir.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List

XML_SOURCE = "ticimax_xml"
EXCEL_SOURCE = "trendyol_excel"


def _s(v: Any) -> str:
    return "" if v is None else str(v).strip()


async def apply_identity(db, records: List[Dict[str, Any]], *, dry_run: bool = True) -> Dict[str, Any]:
    from pymongo import DeleteOne, UpdateOne
    M: Dict[str, Dict[str, Any]] = {}
    B2U: Dict[str, str] = {}
    for r in records:
        u = _s(r.get("u"))
        if not u:
            continue
        M[u] = r
        if _s(r.get("t")):
            B2U[_s(r["t"])] = u
    now = datetime.now(timezone.utc).isoformat()
    st = Counter()
    ops: list = []
    xml_ids: set = set()

    # 1) XML kaynaklı ürünler — barkodu URUNID'ye göre düzelt
    async for p in db.products.find({"source": XML_SOURCE}, {"_id": 0, "id": 1, "variants": 1}):
        vs = list(p.get("variants") or [])
        changed = False
        for v in vs:
            u = _s(v.get("urun_id"))
            if not u:
                continue
            xml_ids.add(u)
            st["xml_variants"] += 1
            rec = M.get(u)
            exp = _s(rec.get("t")) if rec else ""
            cur = _s(v.get("barcode"))
            if exp:
                if cur != exp:
                    st["xml_barcode_fixed" if cur else "xml_barcode_added"] += 1
                    v["barcode"] = exp
                    v["trendyol_barcode"] = exp
                    changed = True
                else:
                    st["xml_barcode_ok"] += 1
            elif cur and cur in B2U and B2U[cur] != u:
                st["xml_barcode_cleared_wrong"] += 1   # başka varyantın barkodu
                v["barcode"] = None
                v.pop("trendyol_barcode", None)
                changed = True
        if changed:
            ops.append(UpdateOne({"id": p["id"]}, {"$set": {"variants": vs, "updated_at": now}}))

    # 2) Excel'den oluşturulmuş ürünler — Ticimax'a bağla / mükerreri kaldır
    async for p in db.products.find({"source": EXCEL_SOURCE}, {"_id": 0, "id": 1, "variants": 1, "price": 1}):
        vs = list(p.get("variants") or [])
        keep = []
        cards = Counter()
        changed = False
        for v in vs:
            u = B2U.get(_s(v.get("barcode")))
            if not u:
                st["excel_not_in_ticimax"] += 1
                if int(v.get("stock") or 0) != 0:
                    v["stock"] = 0
                    changed = True
                keep.append(v)
                continue
            if u in xml_ids:
                st["excel_duplicate_removed"] += 1
                changed = True
                continue
            rec = M[u]
            stock = max(0, int(rec.get("s") or 0)) if int(rec.get("a") or 0) else 0
            price = rec.get("p")
            new = dict(v)
            new["urun_id"] = u
            new["ticimax_barcode"] = _s(rec.get("b")) or None
            new["stock"] = stock
            if price:
                new["price"] = round(float(price), 2)
            cards[_s(rec.get("c"))] += 1
            if new != v:
                changed = True
            st["excel_linked"] += 1
            if stock > 0:
                st["excel_linked_in_stock"] += 1
            keep.append(new)
        if not keep:
            st["excel_products_deleted"] += 1
            ops.append(DeleteOne({"id": p["id"], "source": EXCEL_SOURCE}))
            continue
        if changed:
            base = next((x.get("price") for x in keep if x.get("price")), p.get("price"))
            for x in keep:
                x["price_adjustment"] = round(float(x["price"]) - float(base), 2) if (x.get("price") and base) else 0
            upd = {"variants": keep, "stock": sum(int(x.get("stock") or 0) for x in keep),
                   "sizes": [x["size"] for x in keep if x.get("size")], "barcode": keep[0].get("barcode"),
                   "updated_at": now}
            if base:
                upd["price"] = base
            if cards:
                upd["ticimax_linked"] = True
                upd["ticimax_card_id"] = cards.most_common(1)[0][0]
            st["excel_products_updated"] += 1
            ops.append(UpdateOne({"id": p["id"], "source": EXCEL_SOURCE}, {"$set": upd}))

    if not dry_run:
        for i in range(0, len(ops), 500):
            await db.products.bulk_write(ops[i:i + 500], ordered=False)
        await db.settings.update_one({"id": "ticimax_identity_last"},
                                     {"$set": {"id": "ticimax_identity_last", "finished_at": now, **dict(st)}},
                                     upsert=True)
    return {"dry_run": dry_run, "records": len(M), "with_trendyol_barcode": len(B2U), **dict(st)}


async def sync_linked_variants(db, parsed_products: List[Dict[str, Any]], with_vat) -> Dict[str, int]:
    """XML senkronunda: kartı XML'e sonradan giren (ör. Ticimax'ta kategori yolu atanınca) ama bizde
    Excel'den açılıp Ticimax'a BAĞLANMIŞ varyantların stok/fiyatını URUNID ile günceller. Dönen
    `linked_cards`: tamamı bağlı ürünlerde karşılanan kartlar → içe aktarıcı bunlar için yeni ürün AÇMAZ."""
    from pymongo import UpdateOne
    want: Dict[str, Dict[str, Any]] = {}
    card_of: Dict[str, str] = {}
    for p in parsed_products:
        for v in p.get("variants") or []:
            u = _s(v.get("urun_id"))
            if u:
                want[u] = v
                card_of[u] = _s(p.get("card_id"))
    if not want:
        return {"linked_updated": 0, "linked_cards": set()}
    now = datetime.now(timezone.utc).isoformat()
    ops, n, found = [], 0, set()
    async for p in db.products.find({"source": EXCEL_SOURCE, "ticimax_linked": True},
                                    {"_id": 0, "id": 1, "variants": 1}):
        vs = list(p.get("variants") or [])
        changed = False
        for v in vs:
            nv = want.get(_s(v.get("urun_id")))
            if nv is None:
                continue
            found.add(_s(v.get("urun_id")))
            stock = int(nv.get("stock") or 0)
            price = with_vat(nv.get("price"), nv)
            if int(v.get("stock") or 0) != stock or (price and v.get("price") != price):
                v["stock"] = stock
                if price:
                    v["price"] = price
                changed = True
                n += 1
        if changed:
            ops.append(UpdateOne({"id": p["id"]}, {"$set": {
                "variants": vs, "stock": sum(int(x.get("stock") or 0) for x in vs), "updated_at": now}}))
    for i in range(0, len(ops), 500):
        await db.products.bulk_write(ops[i:i + 500], ordered=False)
    cards_all: Dict[str, List[str]] = {}
    for u, c in card_of.items():
        cards_all.setdefault(c, []).append(u)
    linked_cards = {c for c, us in cards_all.items() if c and all(u in found for u in us)}
    return {"linked_updated": n, "linked_cards": linked_cards}
