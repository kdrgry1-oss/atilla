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
    "description": "Ürün Açıklaması", "gender": "Cinsiyet", "desi": "Desi",
    **{f"img{i}": f"Görsel {i}" for i in range(1, 9)},
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


_SIZE_WORDS = {"NUMARA", "NO", "NR", "BEDEN", "OLCU", "YUZUK", "YUZUKOLCUSU", "MM", "CM", "UZUNLUK", "BOY", "EBAT",
               "SECENEK", "SECINIZ", "OLCUSU"}


def norm_size(s: Any) -> str:
    s = "" if s is None else str(s)
    s = s.translate(_TR).upper().replace(",", ".")
    nums = re.findall(r"\d+(?:\.\d+)?", s)
    words = set(re.findall(r"[A-Z]+", s))
    if len(nums) == 1 and words <= _SIZE_WORDS:
        n = nums[0]
        return n[:-2] if n.endswith(".0") else n
    s = s.replace(" ", "").replace("CM", "")
    s = s[:-2] if re.fullmatch(r"\d+\.0", s) else s
    return "" if s in _NO_SIZE else s


def code_candidates(code: str, size: str) -> List[str]:
    """Sistem stok kodu için denenecek biçimler: kendisi; sonuna eklenmiş beden atılmış hâli
    (LR1100511117 / LR11005111-17 → LR11005111); 'inure' önekli hâli."""
    out = [code] if code else []
    if code and size:
        for suf in (f"-{size}", f"_{size}", f".{size}", f"/{size}", size):
            if code.endswith(suf) and len(code) > len(suf) + 2:
                out.append(code[: -len(suf)].rstrip("-_./"))
    if code:
        si = strip_inure(code)
        if si:
            out.append(si)
    seen, res = set(), []
    for c in out:
        if c and c not in seen:
            seen.add(c)
            res.append(c)
    return res


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


def _has_attr(attrs: Any, name: str) -> bool:
    return any(isinstance(a, dict) and norm_name(a.get("name") or a.get("label")) == norm_name(name)
               and (a.get("value") not in (None, "")) for a in (attrs or []))


def _set_attr(attrs: Any, name: str, value: str) -> List[Dict[str, Any]]:
    """Liste biçimli özelliklere {name,value} ekler; aynı adda DOLU değer varsa dokunmaz (elle girilen korunur)."""
    out = [a for a in (attrs or []) if isinstance(a, dict)] if isinstance(attrs, list) else []
    if _has_attr(out, name):
        return out
    out = [a for a in out if norm_name(a.get("name") or a.get("label")) != norm_name(name)]
    out.append({"name": name, "value": value})
    return out


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


async def run_match(db, content: bytes, *, dry_run: bool = False, create_missing: bool = False,
                    skip_archived: bool = False, rebuild_created: bool = False) -> Dict[str, Any]:
    rows = read_excel(content)
    if rebuild_created and not dry_run:
        # Excel'den oluşturulmuş ürünler baştan kurulur (yalnız bu kaynağa dokunur)
        await db.products.delete_many({"source": EXCEL_SOURCE})
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
    unmatched_sample: List[Dict[str, Any]] = []
    price_sample: List[Dict[str, Any]] = []
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
                                     "category_id": 1, "stock_code": 1, "attributes": 1}):
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
            cands = code_candidates(code, size)
            for c in cands:
                r = pick(by_code_size.get((c, size), []), own)
                if r is not None:
                    how = "kod+beden"
                    break
            if r is None:
                for c in cands:
                    same = by_code.get(c, [])
                    sizes = {x["size_n"] for x in same}
                    if same and (not size or sizes <= {""} or len(variants) == 1 and len(sizes) == 1):
                        r = pick(same, own)
                        if r is not None:
                            how = "kod"
                            break
            if r is None and pname:
                r = pick(by_name_size.get((pname, size), []), own); how = "ad+beden" if r else ""
            if r is None:
                stats["unmatched_variants"] += 1
                ty_sizes = sorted({x["size"] or x["size2"] or "" for c in cands for x in by_code.get(c, [])})
                if not code:
                    stats["unmatched_no_code"] += 1
                elif ty_sizes:
                    stats["unmatched_size_mismatch"] += 1
                else:
                    stats["unmatched_code_not_in_excel"] += 1
                if len(unmatched_sample) < 80:
                    unmatched_sample.append({"product_id": p.get("id"), "name": p.get("name"),
                                             "stock_code": v.get("stock_code") or "", "size": v.get("size"),
                                             "variant_count": len(variants),
                                             "trendyol_sizes_for_code": ty_sizes[:12]})
                continue
            stats[f"by_{how}"] += 1
            cur = str(v.get("barcode") or "")
            if cur and cur != r["barcode"]:
                if cur in pre_used and cur == str(v.get("trendyol_barcode") or ""):
                    pass   # önceki eşleştirmenin barkodu → güncellenebilir
                elif cur == str(v.get("ticimax_barcode") or ""):
                    pass   # `barcode`da duran Ticimax barkodu → Trendyol barkodu yazılır (Ticimax'ınki ayrı alanda)
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
                "gender": r.get("gender") or "",
                "matched_by": how, "matched_at": datetime.now(timezone.utc).isoformat(),
            }
            if not v.get("color") and r["color"]:
                # Varyantın kendi rengi yoksa Trendyol ilanındaki renk ÖZELLİK olarak tutulur (vitrin
                # seçeneği olan `color` alanına yazılmaz) → pazaryeri gönderiminde Renk/Web Color dolar.
                v["attributes"] = _set_attr(v.get("attributes"), "Renk", r["color"])
            variants[i] = v
            changed = True
            stats["matched_variants"] += 1
            matched_rows.append(r)
            base = v.get("price") or p.get("sale_price") or p.get("price")
            if base and r["price_f"]:
                ratios.append(r["price_f"] / float(base))
                if len(price_sample) < 25:
                    price_sample.append({"name": p.get("name"), "size": v.get("size"), "site_price": float(base),
                                         "trendyol_price": r["price_f"], "matched_by": how})
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
                genders = Counter(x.get("gender") for x in matched_rows if x.get("gender")).most_common(1)
                if genders:
                    cur_attrs = p.get("attributes")
                    if isinstance(cur_attrs, (list, type(None))) and not _has_attr(cur_attrs, "Cinsiyet"):
                        upd["attributes"] = _set_attr(cur_attrs, "Cinsiyet", genders[0][0])
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

    # Sistemde karşılığı olmayan Trendyol ilanları → Excel'deki bilgilerle YENİ ürün (istenirse)
    if create_missing:
        to_create = [r for r in unmatched if not (skip_archived and _row_archived(r))]
        stats["skipped_archived_rows"] = len(unmatched) - len(to_create)
        created = await _create_from_rows(db, to_create, dry_run=dry_run)
        stats.update(created)
        if not dry_run:
            unmatched, um_active = [], []

    if not dry_run:
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
        "unmatched_variant_sample": unmatched_sample,
        "price_sample": price_sample,
        "unmatched_sample": [{"barcode": r["barcode"], "stock_code": r["code"], "name": r["name"],
                              "size": r["size"] or r["size2"], "status": r["status"]} for r in um_active[:50]],
    }
    if not dry_run:
        await db.settings.update_one({"id": "trendyol_excel_match_last"},
                                     {"$set": {"id": "trendyol_excel_match_last", **report}}, upsert=True)
    return report


EXCEL_SOURCE = "trendyol_excel"


def _desc_html(text: Optional[str]) -> str:
    import html as _h
    parts = [p.strip() for p in re.split(r";\s*", text or "") if p.strip()]
    return "".join(f"<p>{_h.escape(p)}</p>" for p in parts)


async def _create_from_rows(db, rows: List[Dict[str, Any]], *, dry_run: bool) -> Dict[str, int]:
    """Eşleşmeyen Trendyol satırlarından ürün oluşturur: Model Kodu = ürün, her satır = varyant.
    Varyant `barcode` = Trendyol barkodu. Arşivli/ihlalli ilanlardan oluşan ürün PASİF açılır."""
    import ticimax_xml_import as tx
    from routes.deps import generate_id
    from routes.products import slug_with_card_id

    groups: Dict[str, List[dict]] = defaultdict(list)
    for r in rows:
        groups[r["model"] or r["code"] or r["barcode"]].append(r)
    out = Counter({"created_products": 0, "created_variants": 0, "created_inactive": 0})
    if dry_run:
        out["created_products"] = len(groups)
        out["created_variants"] = len(rows)
        out["created_inactive"] = sum(1 for g in groups.values() if all(x["bad"] or not x["price_f"] for x in g))
        return dict(out)
    now = datetime.now(timezone.utc).isoformat()
    cat_cache: Dict[Tuple[Optional[str], str], str] = {}
    cstats = {"categories_created": 0}
    batch: List[dict] = []
    issued: set = set()   # henüz yazılmamış (aynı partideki) ürünlerin kimlikleri — çakışma olmasın
    for key, g in groups.items():
        g = sorted(g, key=_rank, reverse=True)
        best = g[0]
        price = next((x["price_f"] for x in g if x["price_f"]), None)
        images: List[str] = []
        for x in g:
            for i in range(1, 9):
                u = (x.get(f"img{i}") or "").strip()
                if u.startswith("http") and u not in images:
                    images.append(u)
        cat_ids = await tx._ensure_category_path(db, [best["category"]], cat_cache, cstats) if best["category"] else []
        variants = []
        for x in g:
            size = (x["size"] or x["size2"] or "").strip()
            variants.append({
                "id": generate_id(),
                "size": size if norm_size(size) else None,
                "color": x["color"] or None,
                "barcode": x["barcode"], "trendyol_barcode": x["barcode"], "ticimax_barcode": None,
                "sku": x["code"] or None, "stock_code": x["code"] or None,
                "stock": max(0, x["stock_i"]),
                "price": x["price_f"],
                "price_adjustment": round(x["price_f"] - price, 2) if (x["price_f"] and price) else 0,
                "options": [{"name": "Beden", "value": size}] if norm_size(size) else [],
                "trendyol_listing": {
                    "barcode": x["barcode"], "model_code": x["model"], "stock_code": x["code"],
                    "size": x["size"] or x["size2"], "color": x["color"], "category": x["category"],
                    "price": x["price_f"], "list_price": _num(x["list_price"]), "stock": x["stock_i"],
                    "status": x["status"], "status_note": x["status_note"], "link": x["link"],
                    "matched_by": "excel_olusturuldu", "matched_at": now,
                },
                "source": EXCEL_SOURCE,
            })
        active = price is not None and any(not x["bad"] and x["price_f"] for x in g)
        pid = await tx._new_product_id(db)
        while pid in issued:
            pid = await tx._new_product_id(db)
        issued.add(pid)
        vat = _num(best["vat"])
        doc = {
            "id": pid,
            "name": best["name"] or key,
            "slug": slug_with_card_id(best["name"] or key, pid),
            "description": _desc_html(best.get("description")),
            "short_description": "",
            "brand": best["brand"] or "",
            "images": images, "thumbnail": images[0] if images else "",
            "category_name": best["category"] or "",
            "category_id": cat_ids[-1] if cat_ids else None,
            "category_ids": cat_ids,
            "breadcrumb": best["category"] or "",
            "price": price, "sale_price": None,
            "market_price": _num(best["list_price"]),
            "currency": "TRY", "vat_rate": vat, "vat_included": True, "unit": "ADET",
            "cargo_weight": _num(best.get("desi")),
            "gender": best.get("gender") or "",
            "attributes": ([{"name": "Cinsiyet", "value": best["gender"]}] if best.get("gender") else []),
            "stock_code": best["code"] or "",
            "barcode": variants[0]["barcode"],
            "variants": variants,
            "sizes": [v["size"] for v in variants if v.get("size")],
            "stock": sum(v["stock"] for v in variants),
            "is_active": bool(active), "is_featured": False, "is_new": False,
            "trendyol_category_name": best["category"] or "",
            "trendyol_model_code": best["model"] or "",
            "source": EXCEL_SOURCE,
            "created_at": now, "updated_at": now,
        }
        if not active:
            doc["deactivated_reason"] = "trendyol_arsivli_veya_ihlalli" if price is not None else "fiyat_yok"
            out["created_inactive"] += 1
        batch.append(doc)
        out["created_products"] += 1
        out["created_variants"] += len(variants)
        if len(batch) >= 500:
            await db.products.insert_many(batch, ordered=False)
            batch = []
    if batch:
        await db.products.insert_many(batch, ordered=False)
    out["categories_created"] = cstats["categories_created"]
    return dict(out)


def _row_archived(r: Dict[str, Any]) -> bool:
    st = (r.get("status") or "").lower()
    return "arşiv" in st or "arsiv" in st


def _is_archived(v: Dict[str, Any]) -> bool:
    st = str(((v.get("trendyol_listing") or {}).get("status")) or "").lower()
    return "arşiv" in st or "arsiv" in st


async def prune_archived(db, *, dry_run: bool = True) -> Dict[str, Any]:
    """Excel'den oluşturulmuş ürünlerde Trendyol'da ARŞİVLİ ilandan gelen varyantları kalıcı siler;
    varyantı kalmayan ürün de silinir. Ticimax kaynaklı ürünlere ve ihlalli/çoklanan ilanlara dokunmaz."""
    from pymongo import DeleteOne, UpdateOne
    out = Counter({"products_scanned": 0, "products_deleted": 0, "products_trimmed": 0, "variants_deleted": 0})
    ops: list = []
    now = datetime.now(timezone.utc).isoformat()
    async for p in db.products.find({"source": EXCEL_SOURCE}, {"_id": 0, "id": 1, "variants": 1}):
        out["products_scanned"] += 1
        vs = list(p.get("variants") or [])
        keep = [v for v in vs if not _is_archived(v)]
        if len(keep) == len(vs):
            continue
        out["variants_deleted"] += len(vs) - len(keep)
        if not keep:
            out["products_deleted"] += 1
            ops.append(DeleteOne({"id": p["id"], "source": EXCEL_SOURCE}))
        else:
            out["products_trimmed"] += 1
            ops.append(UpdateOne({"id": p["id"], "source": EXCEL_SOURCE}, {"$set": {
                "variants": keep, "sizes": [v["size"] for v in keep if v.get("size")],
                "stock": sum(int(v.get("stock") or 0) for v in keep),
                "barcode": keep[0].get("barcode"), "updated_at": now}}))
    if not dry_run:
        for i in range(0, len(ops), 500):
            await db.products.bulk_write(ops[i:i + 500], ordered=False)
    return {"dry_run": dry_run, **dict(out)}


async def catalog_fixups(db, *, brand: Optional[str] = None, excel_price_divisor: Optional[float] = None,
                         dry_run: bool = True) -> Dict[str, Any]:
    """Toplu düzeltmeler:
      • brand: TÜM ürünlerde marka adı (elle sabitlenmiş sayılır → XML senkronu ezmez).
      • excel_price_divisor: Excel'den oluşturulmuş ürünlerde panel fiyatı = Trendyol ilan fiyatı ÷ bölen
        (Trendyol'a "panel fiyatı × çarpan" gönderildiğinde bugünkü ilan fiyatı korunur). Her zaman
        kayıtlı ilan fiyatından hesaplanır → tekrar çalıştırmak fiyatı yeniden bölmez."""
    from pymongo import UpdateOne
    now = datetime.now(timezone.utc).isoformat()
    out: Dict[str, Any] = {"dry_run": dry_run}
    if brand:
        q = {"brand": {"$ne": brand}}
        out["brand_changed"] = await db.products.count_documents(q)
        if not dry_run:
            await db.products.update_many(q, {"$set": {"brand": brand, "brand_by": "catalog_fixups",
                                                       "updated_at": now}})
    if excel_price_divisor and excel_price_divisor > 0:
        ops, n, sample = [], 0, []
        async for p in db.products.find({"source": EXCEL_SOURCE},
                                        {"_id": 0, "id": 1, "name": 1, "variants": 1, "price": 1}):
            vs = list(p.get("variants") or [])
            for v in vs:
                lp = _num((v.get("trendyol_listing") or {}).get("price"))
                v["price"] = round(lp / excel_price_divisor, 2) if lp else v.get("price")
            base = next((v["price"] for v in vs if v.get("price")), None)
            if base is None:
                continue
            for v in vs:
                v["price_adjustment"] = round(v["price"] - base, 2) if v.get("price") else 0
            lm = _num((vs[0].get("trendyol_listing") or {}).get("list_price")) if vs else None
            upd = {"variants": vs, "price": base, "updated_at": now}
            if lm:
                upd["market_price"] = round(lm / excel_price_divisor, 2)
            if len(sample) < 5:
                sample.append({"name": p.get("name"), "old": p.get("price"), "new": base})
            n += 1
            ops.append(UpdateOne({"id": p["id"], "source": EXCEL_SOURCE}, {"$set": upd}))
        out["excel_repriced"] = n
        out["excel_price_sample"] = sample
        if not dry_run:
            for i in range(0, len(ops), 500):
                await db.products.bulk_write(ops[i:i + 500], ordered=False)
    return out


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


def last_upload_path():
    import os
    import pathlib
    return pathlib.Path(os.environ.get("ATILLA_DATA_DIR") or "/opt/atilla/data") / "trendyol_last_upload.xlsx"


def save_last_upload(content: bytes) -> None:
    import os
    path = last_upload_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(content)
    os.chmod(tmp, 0o600)
    tmp.replace(path)
