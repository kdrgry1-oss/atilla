"""
Trendyol "Ürünleriniz" Excel'i (satıcı panelinden indirilen ürün listesi) ↔ sistem ürünleri eşleştirmesi.

Amaç: aynı ürünler Trendyol'a BAŞKA barkodlarla yüklenmiş. Her sistem varyantına doğru Trendyol
barkodunu yazarak mevcut Trendyol stok/fiyat senkronunun (variant.barcode) o ilanları güncellemesini
sağlamak; ayrıca kategori eşleştirmesini (sistem kategorisi → Trendyol kategorisi) çıkarmak.

Eşleştirme sırası (her sistem varyantı için):
  1. Tedarikçi Stok Kodu + beden            (en güvenilir)
  2. Tedarikçi Stok Kodu (bedensiz/tek ebat, ya da kodun tek Trendyol satırı varsa)
  3. "inure" önekli Trendyol kodu (inureyR998… → R998…) + beden
  4. Ürün adı + beden (normalize)
Aynı anahtarda birden çok Trendyol ilanı varsa (çoklanan/arşivli ilanlar): arşivlenmemiş/ihlalsiz
olan, sonra stoğu yüksek olan seçilir. Bir Trendyol barkodu yalnız BİR varyanta atanır.

Kullanıcı değişikliği kalıcıdır: varyantta zaten FARKLI bir barkod varsa üzerine yazılmaz (çakışma
olarak raporlanır); kategoride elle girilmiş Trendyol kategorisi ezilmez.
"""
from __future__ import annotations

import io
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

_TR = str.maketrans({"ı": "i", "İ": "i", "ğ": "g", "Ğ": "g", "ü": "u", "Ü": "u", "ş": "s", "Ş": "s",
                     "ö": "o", "Ö": "o", "ç": "c", "Ç": "c"})
_NO_SIZE = {"", "NAN", "NONE", "TEKEBAT", "STANDART", "KLASIK", "STD", "TEK", "UNI", "UNISIZE", "ONESIZE", "-"}
_BAD_STATUS = ("arşiv", "arsiv", "ihlal", "hatalı", "hatali", "çoklanan", "coklanan", "tedarik problemi",
               "orijinallik")

COLS = {
    "barcode": "Barkod", "model": "Model Kodu", "color": "Ürün Rengi", "size": "Beden",
    "size2": "Boyut/Ebat", "brand": "Marka", "category": "Kategori İsmi", "code": "Tedarikçi Stok Kodu",
    "name": "Ürün Adı", "list_price": "Piyasa Satış Fiyatı (KDV Dahil)",
    "price": "Trendyolda Satılacak Fiyat (KDV Dahil)", "stock": "Ürün Stok Adedi", "vat": "KDV Oranı",
    "status": "Durum", "status_note": "Durum Açıklaması", "link": "Trendyol.com Linki",
}


def norm_code(s: Any) -> str:
    s = "" if s is None else str(s)
    if s.lower() in ("nan", "none"):
        return ""
    s = s.strip().upper().replace(" ", "")
    return s[:-2] if re.fullmatch(r"\d+\.0", s) else s


def strip_inure(code: str) -> str:
    """'INUREYR9984417' → 'R9984417' ; 'INURESS9237441' → 'S9237441' ; değilse ''."""
    m = re.fullmatch(r"INURE([A-Z])([A-Z0-9].*)", code)
    return m.group(2) if m else ""


def norm_size(s: Any) -> str:
    s = "" if s is None else str(s)
    s = s.translate(_TR).upper().replace(" ", "").replace("CM", "")
    s = s[:-2] if re.fullmatch(r"\d+\.0", s) else s
    return "" if s in _NO_SIZE else s


def norm_name(s: Any) -> str:
    s = ("" if s is None else str(s)).translate(_TR).lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _num(v: Any) -> Optional[float]:
    try:
        f = float(str(v).replace(",", "."))
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def read_excel(content: bytes) -> List[Dict[str, Any]]:
    import pandas as pd
    xl = pd.ExcelFile(io.BytesIO(content))
    sheet = "Ürünler" if "Ürünler" in xl.sheet_names else xl.sheet_names[0]
    df = pd.read_excel(xl, sheet, dtype=str)
    missing = [c for c in ("Barkod", "Tedarikçi Stok Kodu", "Ürün Adı") if c not in df.columns]
    if missing:
        raise ValueError(f"Excel'de beklenen sütun yok: {', '.join(missing)}")
    rows = []
    for rec in df.to_dict("records"):
        r = {k: (None if (v := rec.get(c)) is None or str(v).lower() == "nan" else str(v).strip())
             for k, c in COLS.items()}
        if not r["barcode"]:
            continue
        r["code_n"] = norm_code(r["code"])
        r["size_n"] = norm_size(r["size"]) or norm_size(r["size2"])
        r["name_n"] = norm_name(r["name"])
        r["price_f"] = _num(r["price"])
        r["stock_i"] = int(_num(r["stock"]) or 0)
        st = (r["status"] or "").lower()
        r["bad"] = any(b in st for b in _BAD_STATUS)
        rows.append(r)
    return rows


def _rank(r: Dict[str, Any]) -> Tuple:
    return (0 if r["bad"] else 1, 1 if not r["status"] else 0, r["stock_i"])


def _flatten_ty_categories(nodes, out: Dict[str, List[int]]):
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        name, cid = n.get("name"), n.get("id")
        subs = n.get("subCategories") or []
        if name and cid is not None and not subs:
            out.setdefault(norm_name(name), []).append(int(cid))
        _flatten_ty_categories(subs, out)


async def run_match(db, content: bytes, *, dry_run: bool = False) -> Dict[str, Any]:
    rows = read_excel(content)
    by_code_size: Dict[Tuple[str, str], List[dict]] = defaultdict(list)
    by_code: Dict[str, List[dict]] = defaultdict(list)
    by_name_size: Dict[Tuple[str, str], List[dict]] = defaultdict(list)
    for r in rows:
        for c in filter(None, {r["code_n"], strip_inure(r["code_n"])}):
            by_code_size[(c, r["size_n"])].append(r)
            by_code[c].append(r)
        if r["name_n"]:
            by_name_size[(r["name_n"], r["size_n"])].append(r)

    used: set = set()
    stats = Counter()
    ratios: List[float] = []
    cat_votes: Dict[str, Counter] = defaultdict(Counter)
    product_updates: List[Tuple[str, Dict[str, Any]]] = []

    def pick(cands: List[dict], own: str = "") -> Optional[dict]:
        ranked = sorted(cands, key=_rank, reverse=True)
        for r in ranked:   # varyantın kendi mevcut barkodu (önceki eşleştirme) her zaman yeniden seçilebilir
            if own and r["barcode"] == own:
                return r
        for r in ranked:
            if r["barcode"] not in used:
                return r
        return None

    # Sistemde zaten kayıtlı Trendyol barkodları (önceki eşleştirme / elle) tekrar atanmasın
    async for p in db.products.find({"is_deleted": {"$ne": True}}, {"_id": 0, "variants.barcode": 1, "barcode": 1}):
        for v in p.get("variants") or []:
            if v.get("barcode"):
                used.add(str(v["barcode"]))
    pre_used = set(used)

    async for p in db.products.find({"is_deleted": {"$ne": True}},
                                    {"_id": 0, "id": 1, "name": 1, "variants": 1, "price": 1, "sale_price": 1,
                                     "category_id": 1, "stock_code": 1}):
        variants = list(p.get("variants") or [])
        if not variants:
            continue
        changed = False
        pname = norm_name(p.get("name"))
        matched_rows = []
        for i, v in enumerate(variants):
            stats["variants"] += 1
            code = norm_code(v.get("stock_code") or v.get("sku") or p.get("stock_code"))
            size = norm_size(v.get("size"))
            own = str(v.get("barcode") or "")
            r, how = None, ""
            if code:
                r = pick(by_code_size.get((code, size), []), own); how = "kod+beden" if r else ""
                if r is None:
                    same = by_code.get(code, [])
                    sizes = {x["size_n"] for x in same}
                    if same and (not size or sizes <= {""} or len(variants) == 1 and len(sizes) == 1):
                        r = pick(same, own); how = "kod" if r else ""
            if r is None and pname:
                r = pick(by_name_size.get((pname, size), []), own); how = "ad+beden" if r else ""
            if r is None:
                stats["unmatched_variants"] += 1
                continue
            stats[f"by_{how}"] += 1
            cur = str(v.get("barcode") or "")
            if cur and cur != r["barcode"]:
                if cur in pre_used and cur == str(v.get("trendyol_barcode") or ""):
                    pass   # önceki eşleştirmenin barkodu → güncellenebilir
                else:
                    stats["conflict_existing_barcode"] += 1
                    used.add(r["barcode"])
                    v["trendyol_barcode_candidate"] = r["barcode"]
                    variants[i] = v
                    changed = True
                    continue
            used.add(r["barcode"])
            v = dict(v)
            v["barcode"] = r["barcode"]
            v["trendyol_barcode"] = r["barcode"]
            v["trendyol_listing"] = {
                "barcode": r["barcode"], "model_code": r["model"], "stock_code": r["code"],
                "size": r["size"] or r["size2"], "color": r["color"], "category": r["category"],
                "price": r["price_f"], "list_price": _num(r["list_price"]), "stock": r["stock_i"],
                "status": r["status"], "status_note": r["status_note"], "link": r["link"],
                "matched_by": how, "matched_at": datetime.now(timezone.utc).isoformat(),
            }
            variants[i] = v
            changed = True
            stats["matched_variants"] += 1
            matched_rows.append(r)
            base = p.get("sale_price") or p.get("price")
            if base and r["price_f"]:
                ratios.append(r["price_f"] / float(base))
        if matched_rows:
            stats["matched_products"] += 1
            cats = Counter(x["category"] for x in matched_rows if x["category"])
            if p.get("category_id") and cats:
                cat_votes[str(p["category_id"])].update(cats)
        if changed:
            upd = {"variants": variants, "updated_at": datetime.now(timezone.utc).isoformat()}
            if matched_rows:
                top = Counter(x["category"] for x in matched_rows if x["category"]).most_common(1)
                if top:
                    upd["trendyol_category_name"] = top[0][0]
                models = {x["model"] for x in matched_rows if x["model"]}
                if len(models) == 1:
                    upd["trendyol_model_code"] = models.pop()
            product_updates.append((p["id"], upd))

    # Kategori eşleştirmesi (çoğunluk oyu); Trendyol kategori ağacı önbellekte varsa ID de bulunur
    ty_ids: Dict[str, List[int]] = {}
    try:
        tree = await db.trendyol_categories.find({}, {"_id": 0}).to_list(None)
        _flatten_ty_categories(tree, ty_ids)
    except Exception:
        pass
    cat_report = []
    cat_updates = []
    for cid, votes in cat_votes.items():
        c = await db.categories.find_one({"id": cid}, {"_id": 0, "id": 1, "name": 1,
                                                        "trendyol_category_name": 1, "trendyol_category_id": 1})
        if not c:
            continue
        name, n = votes.most_common(1)[0]
        share = round(100 * n / sum(votes.values()))
        ids = ty_ids.get(norm_name(name)) or []
        tid = ids[0] if len(ids) == 1 else None
        row = {"category": c.get("name"), "category_id": cid, "trendyol_category": name, "share_pct": share,
               "trendyol_category_id": tid, "others": [k for k, _ in votes.most_common(4)[1:]]}
        if c.get("trendyol_category_name") or c.get("trendyol_category_id"):
            row["kept_existing"] = c.get("trendyol_category_name") or c.get("trendyol_category_id")
        else:
            st = {"trendyol_category_name": name, "trendyol_category_source": "excel_match"}
            if tid:
                st["trendyol_category_id"] = tid
            cat_updates.append((cid, st))
        cat_report.append(row)

    # Eşleşmeyen Trendyol satırları
    unmatched = [r for r in rows if r["barcode"] not in used]
    um_active = [r for r in unmatched if not r["bad"]]
    stats["excel_rows"] = len(rows)
    stats["excel_unmatched"] = len(unmatched)
    stats["excel_unmatched_active"] = len(um_active)
    stats["excel_unmatched_archived_or_flagged"] = len(unmatched) - len(um_active)

    if not dry_run:
        from pymongo import UpdateOne
        ops = [UpdateOne({"id": pid}, {"$set": u}) for pid, u in product_updates]
        for i in range(0, len(ops), 500):
            await db.products.bulk_write(ops[i:i + 500], ordered=False)
        for cid, st in cat_updates:
            await db.categories.update_one({"id": cid}, {"$set": st})
        await db.trendyol_excel_unmatched.delete_many({})
        docs = [{"barcode": r["barcode"], "stock_code": r["code"], "model_code": r["model"], "name": r["name"],
                 "size": r["size"] or r["size2"], "category": r["category"], "price": r["price_f"],
                 "stock": r["stock_i"], "status": r["status"], "link": r["link"]} for r in unmatched]
        for i in range(0, len(docs), 1000):
            await db.trendyol_excel_unmatched.insert_many(docs[i:i + 1000])

    report = {
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "stats": dict(stats),
        "price_ratio_median": round(statistics.median(ratios), 3) if ratios else None,
        "suggested_markup_pct": round((statistics.median(ratios) - 1) * 100, 1) if ratios else None,
        "categories": sorted(cat_report, key=lambda x: -x["share_pct"]),
        "unmatched_sample": [{"barcode": r["barcode"], "stock_code": r["code"], "name": r["name"],
                              "size": r["size"] or r["size2"], "status": r["status"]} for r in um_active[:50]],
    }
    if not dry_run:
        await db.settings.update_one({"id": "trendyol_excel_match_last"},
                                     {"$set": {"id": "trendyol_excel_match_last", **report}}, upsert=True)
    return report


async def unmatched_xlsx(db) -> bytes:
    import pandas as pd
    rows = await db.trendyol_excel_unmatched.find({}, {"_id": 0}).to_list(None)
    df = pd.DataFrame(rows, columns=["barcode", "stock_code", "model_code", "name", "size", "category",
                                     "price", "stock", "status", "link"])
    df.columns = ["Barkod", "Tedarikçi Stok Kodu", "Model Kodu", "Ürün Adı", "Beden", "Kategori",
                  "Fiyat", "Stok", "Durum", "Trendyol Linki"]
    # Excel/CSV formül enjeksiyonu: = + - @ ile başlayan metin hücreleri kaçışlanır
    for c in df.columns:
        df[c] = df[c].map(lambda v: ("'" + v) if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r") else v)
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    return buf.getvalue()
