"""
Ticimax "XMLExport" ürün feed'i içe aktarımı (ör. https://<magaza>/XMLExport/<ANAHTAR>).

Feed yapısı (Ticimax standardı; etiket adları büyük/küçük harf duyarsız okunur):

    <Root><Urunler>
      <Urun>
        <UrunKartiID/> <UrunAdi/> <OnYazi/> <Aciklama/> <Marka/> <Tedarikci/>
        <Kategori/> <KategoriTree/>  (veya <Kategoriler><Kategori/>...)
        <Resimler><Resim/>...</Resimler>
        <UrunSecenek>
          <Secenek>
            <VaryasyonID/> <StokKodu/> <Barkod/> <StokAdedi/> <SatisFiyati/> <IndirimliFiyat/>
            <KDVDahil/> <KdvOrani/> <ParaBirimi/> <Desi/> ...
            <EkSecenekOzellik><Ozellik Tanim="Beden" Deger="M">M</Ozellik></EkSecenekOzellik>
          </Secenek>
        </UrunSecenek>
        <TeknikDetaylar>...</TeknikDetaylar>  (varsa)
      </Urun>
    </Urunler></Root>

Kurallar:
  • YALNIZ STOĞU OLAN ürünler YENİ olarak eklenir (varyant stok toplamı > 0). Ürünün stoksuz
    varyantları da (0 stokla) eklenir ki beden/renk seçenekleri eksiksiz görünsün.
  • Kategori ağacı korunur: "Takı > Kolye > Gümüş Kolye" → üç kademeli kategori; aynı adlı
    kategori aynı üst kategori altında varsa yeniden KULLANILIR, yoksa oluşturulur.
  • XML'deki TÜM bilgi alınır: bilinen alanlar tipli ürün alanlarına (fiyat, açıklama, marka, KDV,
    SEO, ağırlık/boyut …) yazılır; her alan ayrıca `ticimax_fields` (Ticimax kolon adlarıyla, ürün
    kartında görünür) ve `ticimax_xml` (ham, eksiksiz) altında saklanır. Varyant alanları da
    varyantın `ticimax_fields` / `ticimax_xml` alanında durur.
  • Tekrar çalıştırılabilir: eşleştirme `urun_karti_id` (+ varyant ID / barkod / stok kodu) ile.
    Mevcut üründe stok/fiyat güncellenir; panelden ELLE düzenlenmiş alanlara dokunulmaz
    (CLAUDE.md "kullanıcının değişikliği kalıcıdır"); elle pasife alınmış/silinmiş ürün açılmaz.
"""
from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("ticimax_xml_import")

SOURCE = "ticimax_xml"
MAX_FEED_BYTES = 300 * 1024 * 1024

_TR_UP = str.maketrans({"ı": "I", "i": "I", "ğ": "G", "ü": "U", "ş": "S", "ö": "O", "ç": "C",
                        "İ": "I", "Ğ": "G", "Ü": "U", "Ş": "S", "Ö": "O", "Ç": "C"})


def _key(tag: str) -> str:
    """Etiket adı → Ticimax kolon biçimi (URUNADI, KDVORANI …)."""
    t = tag.split("}")[-1]
    return re.sub(r"[^A-Z0-9_]", "", t.translate(_TR_UP).upper())


def _norm(s: str) -> str:
    s = (s or "").translate(_TR_UP).upper()
    return re.sub(r"[^A-Z0-9]+", " ", s).strip()


def _txt(el) -> str:
    if el is None:
        return ""
    return "".join(el.itertext()).strip()


def _num(v: Any) -> Optional[float]:
    if v is None:
        return None
    s = str(v).strip().replace("TL", "").replace("₺", "").strip()
    if not s:
        return None
    # 1.234,56 → 1234.56 ; 1234,56 → 1234.56 ; 1234.56 → 1234.56
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _bool(v: Any) -> Optional[bool]:
    s = str(v or "").strip().lower()
    if s in ("1", "true", "evet", "yes", "e", "aktif"):
        return True
    if s in ("0", "false", "hayir", "hayır", "no", "h", "pasif"):
        return False
    return None


def _children(el, *names: str) -> list:
    want = {_key(n) for n in names}
    return [c for c in el if isinstance(c.tag, str) and _key(c.tag) in want]


def _child(el, *names: str):
    ch = _children(el, *names)
    return ch[0] if ch else None


def _el_to_obj(el) -> Any:
    """Elemanı eksiksiz sözlüğe çevir (öznitelikler '@ad', metin '#text', tekrar eden çocuk → liste)."""
    kids = [c for c in el if isinstance(c.tag, str)]
    attrs = {f"@{k.split('}')[-1]}": v for k, v in el.attrib.items()}
    if not kids and not attrs:
        return _txt(el)
    out: Dict[str, Any] = dict(attrs)
    for c in kids:
        k = c.tag.split("}")[-1]
        v = _el_to_obj(c)
        if k in out:
            if not isinstance(out[k], list):
                out[k] = [out[k]]
            out[k].append(v)
        else:
            out[k] = v
    t = (el.text or "").strip()
    if t:
        out["#text"] = t
    return out


def _flat_scalars(el) -> Dict[str, str]:
    """Doğrudan çocuklardan YAPRAK olanlar → {TICIMAXKOLON: metin}."""
    out: Dict[str, str] = {}
    for c in el:
        if not isinstance(c.tag, str):
            continue
        if len([g for g in c if isinstance(g.tag, str)]) == 0:
            k = _key(c.tag)
            if k and k not in out:
                out[k] = html.unescape(_txt(c))
    return out


# ───────────────────────────── ayrıştırma ─────────────────────────────

def parse_feed(content: bytes) -> List[Dict[str, Any]]:
    """XML'i güvenli ayrıştır (DTD/ENTITY/ağ yok) ve ürün sözlükleri listesi döndür."""
    from lxml import etree
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False,
                             huge_tree=True, recover=True, remove_comments=True)
    root = etree.fromstring(content, parser=parser)
    if root is None:
        raise ValueError("XML okunamadı")
    urunler = [e for e in root.iter() if isinstance(e.tag, str) and _key(e.tag) in ("URUN", "PRODUCT")
               and (_child(e, "UrunKartiID", "UrunAdi", "ProductName") is not None)]
    return [_parse_product(u) for u in urunler]


_COLOR_KEYS = ("RENK", "COLOR", "RENKLER")
_SIZE_KEYS = ("BEDEN", "SIZE", "OLCU", "NUMARA", "EBAT", "BOY", "YUZUK OLCUSU", "UZUNLUK")


def _parse_option_attrs(sec) -> List[Tuple[str, str]]:
    pairs: List[Tuple[str, str]] = []
    holder = _child(sec, "EkSecenekOzellik", "SecenekOzellik", "Ozellikler")
    for oz in (holder if holder is not None else sec).iter():
        if not isinstance(oz.tag, str) or _key(oz.tag) not in ("OZELLIK", "OZELLIKLER", "ATTRIBUTE"):
            continue
        name = oz.get("Tanim") or oz.get("tanim") or oz.get("Ad") or oz.get("Name") or ""
        val = oz.get("Deger") or oz.get("deger") or oz.get("Value") or _txt(oz)
        if not name:
            tn, dv = _child(oz, "Tanim", "Ad", "Name"), _child(oz, "Deger", "Value")
            name, val = _txt(tn), _txt(dv) or val
        if name and val:
            pairs.append((name.strip(), val.strip()))
    # Eski biçim: <Varyasyon>Renk Seçiniz;GRİ,Beden Seçiniz;S</Varyasyon>
    if not pairs:
        vs = _txt(_child(sec, "Varyasyon", "VaryasyonAdi"))
        for part in vs.split(","):
            if ";" in part:
                k, v = part.split(";", 1)
                if k.strip() and v.strip():
                    pairs.append((re.sub(r"\s*se[cç]iniz\s*$", "", k.strip(), flags=re.I), v.strip()))
    return pairs


def _parse_variant(sec) -> Dict[str, Any]:
    f = _flat_scalars(sec)
    attrs = _parse_option_attrs(sec)
    color = size = None
    extra: List[str] = []
    for name, val in attrs:
        n = _norm(name)
        if color is None and any(k in n for k in _COLOR_KEYS):
            color = val
        elif size is None and any(k in n for k in _SIZE_KEYS):
            size = val
        else:
            extra.append(val)
    if size is None and extra:
        size = " / ".join(extra)   # tanımsız seçenek (ör. "Taş Rengi") boyut eksenine düşer
    stock = _num(f.get("STOKADEDI") or f.get("STOK") or f.get("MIKTAR"))
    return {
        "urun_id": f.get("VARYASYONID") or f.get("URUNID") or f.get("ID") or "",
        "stock_code": f.get("STOKKODU") or "",
        "barcode": f.get("BARKOD") or "",
        "stock": max(0, int(stock or 0)),
        "price": _num(f.get("SATISFIYATI")),
        "sale_price": _num(f.get("INDIRIMLIFIYAT")),
        "purchase_price": _num(f.get("ALISFIYATI")),
        "market_price": _num(f.get("PIYASAFIYATI")),
        "vat_rate": _num(f.get("KDVORANI") or f.get("KDV")),
        "vat_included": _bool(f.get("KDVDAHIL")),
        "currency": f.get("PARABIRIMI") or "",
        "desi": _num(f.get("DESI") or f.get("KARGOAGIRLIGI")),
        "color": color,
        "size": size,
        "options": [{"name": n, "value": v} for n, v in attrs],
        "fields": f,
        "raw": _el_to_obj(sec),
    }


def _images(u) -> List[str]:
    out: List[str] = []
    holder = _child(u, "Resimler", "Images")
    pool = list(holder) if holder is not None else _children(u, "Resim", "ResimUrl", "Image")
    for r in pool:
        if not isinstance(r.tag, str):
            continue
        s = _txt(r) or r.get("url") or r.get("Url") or ""
        s = s.strip()
        if s.startswith("//"):
            s = "https:" + s
        if s.startswith("http") and s not in out:
            out.append(s)
    return out


def _category_paths(u, f: Dict[str, str]) -> List[List[str]]:
    paths: List[List[str]] = []

    def split(s: str) -> List[str]:
        parts = [p.strip() for p in re.split(r"\s*(?:>|»|/|\\|\|)\s*", s or "") if p.strip()]
        return [p for p in parts if _norm(p) not in ("ANA SAYFA", "ANASAYFA", "HOME", "TUM URUNLER")]

    tree = f.get("KATEGORITREE") or f.get("KATEGORIAGACI") or f.get("BREADCRUMBKAT") or ""
    if tree:
        paths.append(split(tree))
    holder = _child(u, "Kategoriler", "Categories")
    if holder is not None:
        for c in holder:
            if isinstance(c.tag, str):
                p = split(c.get("Tree") or c.get("Yol") or _txt(c))
                if p and p not in paths:
                    paths.append(p)
    if not paths and f.get("KATEGORI"):
        paths.append(split(f["KATEGORI"]))
    return [p for p in paths if p]


def _parse_technical(u) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    holder = _child(u, "TeknikDetaylar", "TeknikDetay", "Ozellikler")
    if holder is None:
        return out
    for d in holder.iter():
        if not isinstance(d.tag, str) or d is holder:
            continue
        name = d.get("Tanim") or d.get("OzellikAdi") or d.get("Grup") or ""
        val = d.get("Deger") or d.get("OzellikDegeri") or ""
        if not name:
            name = _txt(_child(d, "OzellikAdi", "Tanim", "Ad", "Name"))
            val = _txt(_child(d, "OzellikDegeri", "Deger", "Value")) or val
        if name and val and {"name": name, "value": val} not in out:
            out.append({"name": name.strip(), "value": val.strip()})
    return out


def _parse_product(u) -> Dict[str, Any]:
    f = _flat_scalars(u)
    sec_holder = _child(u, "UrunSecenek", "Secenekler", "Varyasyonlar")
    secs = [s for s in (sec_holder if sec_holder is not None else []) if isinstance(s.tag, str)]
    variants = [_parse_variant(s) for s in secs]
    if not variants:   # seçeneksiz ürün: ürün düzeyindeki alanlardan tek varyant
        v = _parse_variant(u)
        v["raw"] = {}
        variants = [v]
    return {
        "card_id": f.get("URUNKARTIID") or f.get("URUNID") or "",
        "name": f.get("URUNADI") or f.get("PRODUCTNAME") or "",
        "fields": f,
        "images": _images(u),
        "category_paths": _category_paths(u, f),
        "technical": _parse_technical(u),
        "variants": variants,
        "raw": _el_to_obj(u),
    }


# ───────────────────────────── yazma ─────────────────────────────

def _with_vat(price: Optional[float], v: Dict[str, Any]) -> Optional[float]:
    if price is None or price <= 0:
        return None
    if v.get("vat_included") is False and (v.get("vat_rate") or 0) > 0:
        return round(price * (1 + v["vat_rate"] / 100.0), 2)
    return round(price, 2)


async def _ensure_category_path(db, path: List[str], cache: Dict[Tuple[Optional[str], str], str],
                                stats: Dict[str, int]) -> List[str]:
    from routes.deps import generate_short_id
    from routes.products import generate_slug
    ids: List[str] = []
    parent: Optional[str] = None
    for name in path:
        ck = (parent, _norm(name))
        cid = cache.get(ck)
        if cid is None:
            q = {"parent_id": parent} if parent else {"parent_id": {"$in": [None, ""]}}
            async for c in db.categories.find(q, {"_id": 0, "id": 1, "name": 1}):
                if _norm(c.get("name", "")) == ck[1]:
                    cid = c["id"]
                    break
            if cid is None:
                cid = await generate_short_id("categories")
                base = generate_slug(name) or "kategori"
                slug, i = base, 2
                while await db.categories.find_one({"slug": slug}, {"_id": 1}):
                    slug, i = f"{base}-{i}", i + 1
                await db.categories.insert_one({
                    "id": cid, "name": name, "slug": slug, "description": "", "image": "", "image_url": "",
                    "parent_id": parent, "attribute_mapping": {}, "sort_order": 0, "is_active": True,
                    "members_only": False, "members_only_start": None, "members_only_end": None,
                    "source": SOURCE, "created_at": datetime.now(timezone.utc).isoformat(),
                })
                stats["categories_created"] += 1
            cache[ck] = cid
        ids.append(cid)
        parent = cid
    return ids


def _variant_doc(v: Dict[str, Any], base_price: Optional[float]) -> Dict[str, Any]:
    from routes.deps import generate_id
    vp = _with_vat(v.get("price"), v)
    return {
        "id": generate_id(),
        "size": v.get("size"),
        "color": v.get("color"),
        "barcode": v.get("barcode") or None,
        "sku": v.get("stock_code") or None,
        "stock_code": v.get("stock_code") or None,
        "urun_id": str(v.get("urun_id") or "") or None,
        "stock": int(v.get("stock") or 0),
        "price_adjustment": round((vp - base_price), 2) if (vp and base_price) else 0,
        "price": vp,
        "options": v.get("options") or [],
        "ticimax_fields": v.get("fields") or {},
        "ticimax_xml": v.get("raw") or {},
        "source": SOURCE,
    }


def _variant_match(old: Dict[str, Any], new: Dict[str, Any]) -> bool:
    for a, b in (("urun_id", "urun_id"), ("barcode", "barcode"), ("stock_code", "stock_code")):
        x, y = str(old.get(a) or "").strip(), str(new.get(b) or "").strip()
        if x and y and x == y:
            return True
    return False


def _is_manual(existing: Dict[str, Any], key: str) -> bool:
    manual = set(existing.get("manual_edit_fields") or []) | set(existing.get("locked_fields") or [])
    return (key in manual or bool(existing.get(f"{key}_by")) or bool(existing.get(f"{key}_manual_at"))
            or bool(existing.get(f"{key}_edited_at")))


async def import_products(db, products: List[Dict[str, Any]], *, dry_run: bool = False) -> Dict[str, Any]:
    from routes.deps import generate_short_id
    from routes.products import slug_with_card_id

    now = datetime.now(timezone.utc).isoformat()
    stats = {"feed_products": len(products), "skipped_no_stock": 0, "skipped_invalid": 0,
             "created": 0, "updated": 0, "variants": 0, "categories_created": 0, "errors": 0}
    cat_cache: Dict[Tuple[Optional[str], str], str] = {}

    for p in products:
        try:
            card_id, name = str(p.get("card_id") or "").strip(), (p.get("name") or "").strip()
            if not card_id or not name:
                stats["skipped_invalid"] += 1
                continue
            f, vs = p["fields"], p["variants"]
            total_stock = sum(int(v.get("stock") or 0) for v in vs)
            existing = await db.products.find_one({"source": SOURCE, "urun_karti_id": card_id}, {"_id": 0})
            if existing is None and total_stock <= 0:
                stats["skipped_no_stock"] += 1
                continue

            first = next((v for v in vs if v.get("price")), vs[0] if vs else {})
            price = _with_vat(first.get("price"), first) if first else None
            sale = _with_vat(first.get("sale_price"), first) if first else None
            if sale is not None and price is not None and sale >= price:
                sale = None

            cat_ids: List[str] = []
            for path in p["category_paths"]:
                ids = await _ensure_category_path(db, path, cat_cache, stats) if not dry_run else []
                for i in ids:
                    if i not in cat_ids:
                        cat_ids.append(i)
            primary_path = p["category_paths"][0] if p["category_paths"] else []
            primary_cat = None
            if p["category_paths"] and cat_ids:
                primary_cat = cat_ids[len(primary_path) - 1] if len(primary_path) <= len(cat_ids) else cat_ids[-1]

            desc = html.unescape(f.get("ACIKLAMA") or f.get("URUNACIKLAMA") or "")
            typed: Dict[str, Any] = {
                "name": name,
                "description": desc,
                "short_description": f.get("ONYAZI") or "",
                "brand": f.get("MARKA") or "",
                "supplier": f.get("TEDARIKCI") or "",
                "gtip_code": f.get("GTIPKODU") or "",
                "keywords": f.get("ANAHTARKELIME") or "",
                "meta_title": f.get("SEO_SAYFABASLIK") or f.get("SEOSAYFABASLIK") or "",
                "meta_description": f.get("SEO_SAYFAACIKLAMA") or f.get("SEOSAYFAACIKLAMA") or "",
                "meta_keywords": f.get("SEO_ANAHTARKELIME") or f.get("SEOANAHTARKELIME") or "",
                "breadcrumb": " > ".join(primary_path),
                "unit": f.get("SATISBIRIMI") or "ADET",
                "currency": "TRY" if (first.get("currency") or "TL").upper() in ("TL", "TRY", "") else first.get("currency"),
                "vat_rate": first.get("vat_rate"),
                "vat_included": True,
                "purchase_price": first.get("purchase_price"),
                "market_price": first.get("market_price"),
                "cargo_weight": first.get("desi"),
                "product_url": f.get("URUNURL") or f.get("URL") or "",
                "images": p["images"],
                "thumbnail": p["images"][0] if p["images"] else "",
                "category_name": primary_path[-1] if primary_path else "",
                "category_id": primary_cat,
                "category_ids": cat_ids,
                "technical_details": p["technical"],
                "ticimax_fields": {**f, **{k: v for k, v in (first.get("fields") or {}).items() if k not in f}},
                "ticimax_xml": p["raw"],
                "stock_code": first.get("stock_code") or f.get("STOKKODU") or "",
                "barcode": first.get("barcode") or "",
            }
            for k in ("is_showcase:VITRIN", "is_new:YENIURUN", "is_opportunity:FIRSATURUNU",
                      "is_free_shipping:UCRETSIZKARGO"):
                fld, col = k.split(":")
                b = _bool(f.get(col))
                if b is not None:
                    typed[fld] = b
            for col, fld in (("URUNAGIRLIGI", "product_weight"), ("URUNGENISLIK", "width"),
                             ("URUNDERINLIK", "depth"), ("URUNYUKSEKLIK", "height"),
                             ("MAKSTAKSITSAYISI", "max_installment")):
                n = _num(f.get(col))
                if n is not None:
                    typed[fld] = n
            if price is not None:
                typed["price"], typed["sale_price"] = price, sale
            typed = {k: v for k, v in typed.items() if v is not None}

            if existing is None:
                variants = [_variant_doc(v, price) for v in vs]
                doc = {
                    **typed,
                    "id": await generate_short_id("products") if not dry_run else "dry",
                    "urun_karti_id": card_id,
                    "slug": slug_with_card_id(name, card_id),
                    "variants": variants,
                    "sizes": [v["size"] for v in variants if v.get("size")],
                    "stock": total_stock,
                    "is_active": price is not None,
                    "is_featured": False,
                    "source": SOURCE,
                    "created_at": now, "updated_at": now,
                }
                doc.setdefault("is_new", False)
                if price is None:
                    doc["deactivated_reason"] = "xml_price_missing"   # 0 TL'ye satış olmasın
                if not dry_run:
                    await db.products.insert_one(doc)
                stats["created"] += 1
                stats["variants"] += len(variants)
                continue

            # ── mevcut ürün: stok/fiyat/varyant güncelle; elle düzenlenmiş alana dokunma ──
            upd = {k: v for k, v in typed.items() if not _is_manual(existing, k)}
            for k in ("description", "images", "thumbnail", "category_ids", "category_id",
                      "technical_details", "brand"):
                if k in upd and not upd[k] and existing.get(k):
                    upd.pop(k)   # feed'in BOŞ değeri DOLU mevcut veriyi silmesin
            old_vars = list(existing.get("variants") or [])
            merged: List[Dict[str, Any]] = []
            used = set()
            for nv in vs:
                hit = next((i for i, ov in enumerate(old_vars) if i not in used and _variant_match(ov, nv)), None)
                if hit is None:
                    merged.append(_variant_doc(nv, price))
                else:
                    used.add(hit)
                    ov = dict(old_vars[hit])
                    ov["stock"] = int(nv.get("stock") or 0)
                    ov["ticimax_fields"] = nv.get("fields") or {}
                    ov["ticimax_xml"] = nv.get("raw") or {}
                    ov["options"] = nv.get("options") or ov.get("options") or []
                    vp = _with_vat(nv.get("price"), nv)
                    if vp and not _is_manual(existing, "price"):
                        ov["price"] = vp
                    merged.append(ov)
            # feed'de artık olmayan varyant: silinmez, stoğu 0'lanır (satılamasın)
            for i, ov in enumerate(old_vars):
                if i not in used:
                    ov = dict(ov)
                    ov["stock"] = 0
                    merged.append(ov)
            if not _is_manual(existing, "variants"):
                upd["variants"] = merged
                upd["stock"] = sum(int(v.get("stock") or 0) for v in merged)
            if existing.get("manual_deactivated") or existing.get("is_deleted"):
                upd.pop("is_active", None)
            upd["updated_at"] = now
            if not dry_run:
                await db.products.update_one({"id": existing["id"]}, {"$set": upd})
            stats["updated"] += 1
            stats["variants"] += len(merged)
        except Exception as e:   # tek ürün hatası tüm aktarımı durdurmasın
            stats["errors"] += 1
            log.warning("ürün aktarılamadı (kart=%s): %s", p.get("card_id"), type(e).__name__)
    return stats


async def run_import(db, url: str, *, dry_run: bool = False) -> Dict[str, Any]:
    from security_fetch import safe_fetch
    res = await safe_fetch(url, max_bytes=MAX_FEED_BYTES, timeout=600, allow_redirects=3)
    if not res.ok:
        raise RuntimeError(f"XML indirilemedi: HTTP {res.status_code}")
    products = parse_feed(res.content)
    stats = await import_products(db, products, dry_run=dry_run)
    stats["finished_at"] = datetime.now(timezone.utc).isoformat()
    if not dry_run:
        await db.settings.update_one({"id": "ticimax_xml_import_last"},
                                     {"$set": {"id": "ticimax_xml_import_last", **stats}}, upsert=True)
    return stats
