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
# Google Shopping feed'i adet vermez (yalnız "in stock"); stoktaki varyantın varsayılan adedi.
# .env PRODUCT_XML_DEFAULT_STOCK ile değiştirilebilir. Senkron her çalıştığında yeniden yazılır.
import os as _os
try:
    DEFAULT_STOCK = max(1, int(_os.environ.get("PRODUCT_XML_DEFAULT_STOCK") or 1))
except ValueError:
    DEFAULT_STOCK = 1
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

def parse_feed(content: bytes, images: Optional[Dict[str, List[str]]] = None) -> List[Dict[str, Any]]:
    """XML'i güvenli ayrıştır (DTD/ENTITY/ağ yok) ve ürün sözlükleri listesi döndür."""
    from lxml import etree
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False,
                             huge_tree=True, recover=True, remove_comments=True)
    root = etree.fromstring(content, parser=parser)
    if root is None:
        raise ValueError("XML okunamadı")
    urunler = [e for e in root.iter() if isinstance(e.tag, str) and _key(e.tag) in ("URUN", "PRODUCT")
               and (_child(e, "UrunKartiID", "UrunAdi", "ProductName") is not None)]
    if urunler:
        if _is_flat_layout(urunler):
            prods = _parse_flat_products(urunler)
        else:
            prods = [_parse_product(u) for u in urunler]
        if images:
            for p in prods:
                if not p["images"]:
                    p["images"] = list(images.get(str(p.get("card_id") or "")) or [])
        return prods
    # Google Shopping (RSS/Atom) biçimi: her <item> bir varyant, item_group_id ile ürüne bağlanır.
    items = [e for e in root.iter() if isinstance(e.tag, str) and _key(e.tag) in ("ITEM", "ENTRY")
             and _child(e, "id") is not None and _child(e, "title") is not None]
    return _parse_google_items(items)


# ───────────────────────── Google Shopping biçimi ─────────────────────────

def _g(item, name: str) -> str:
    return html.unescape(_txt(_child(item, name)))


def _g_all(item, name: str) -> List[str]:
    return [html.unescape(_txt(c)) for c in _children(item, name) if _txt(c)]


def _price_num(v: str) -> Optional[float]:
    m = re.search(r"\d[\d.,]*", v or "")
    return _num(m.group(0)) if m else None


def _variant_attrs_google(item) -> List[Tuple[str, str]]:
    """g:color / g:size / g:material / g:pattern + additional_variant_attribute (alt etiketli ya da
    'Ad: Değer' metni) → [(ad, değer)]."""
    out: List[Tuple[str, str]] = []
    for tag, label in (("color", "Renk"), ("size", "Beden"), ("material", "Materyal"), ("pattern", "Desen")):
        v = _g(item, tag)
        if v:
            out.append((label, v))
    for ava in _children(item, "additional_variant_attribute"):
        kids = [c for c in ava if isinstance(c.tag, str)]
        if kids:
            n = _txt(_child(ava, "label", "name", "attribute_name"))
            v = _txt(_child(ava, "value", "attribute_value"))
        else:
            t = _txt(ava)
            n, v = (t.split(":", 1) + [""])[:2] if ":" in t else ("Seçenek", t)
        n, v = html.unescape(n).strip(), html.unescape(v).strip()
        if v:
            out.append((n or "Seçenek", v))
    return out


def _product_details_google(item) -> List[Dict[str, str]]:
    out = []
    for d in _children(item, "product_detail"):
        n = html.unescape(_txt(_child(d, "attribute_name")))
        v = html.unescape(_txt(_child(d, "attribute_value")))
        sec = html.unescape(_txt(_child(d, "section_name")))
        if n and v:
            out.append({"section": sec, "name": n, "value": v})
    return out


def _common_title(titles: List[str]) -> str:
    titles = [t for t in titles if t]
    if not titles:
        return ""
    if len(set(titles)) == 1:
        return titles[0]
    words = [t.split() for t in titles]
    pref = []
    for ws in zip(*words):
        if len(set(w.lower() for w in ws)) == 1:
            pref.append(ws[0])
        else:
            break
    name = " ".join(pref).strip(" -–,/|")
    return name if len(name) >= 3 else min(titles, key=len)


def _parse_google_items(items: list) -> List[Dict[str, Any]]:
    groups: Dict[str, list] = {}
    for it in items:
        gid = _g(it, "item_group_id") or _g(it, "id")
        groups.setdefault(gid, []).append(it)
    products: List[Dict[str, Any]] = []
    for gid, its in groups.items():
        first = its[0]
        details = [_product_details_google(i) for i in its]
        # Gruptaki varyantlar arasında DEĞERİ DEĞİŞEN ürün detayı → varyant ekseni
        varying = set()
        if len(its) > 1:
            keys = {d["name"] for ds in details for d in ds}
            for k in keys:
                vals = {next((d["value"] for d in ds if d["name"] == k), None) for ds in details}
                if len(vals) > 1:
                    varying.add(k)
        raw_attrs = [_variant_attrs_google(i) for i in its]
        # Grup içinde DEĞERİ DEĞİŞMEYEN seçenek (ör. feed'deki anlamsız 'Name') varyant ekseni değildir.
        if len(its) > 1:
            keep = set()
            for n in {_norm(n) for ra in raw_attrs for n, _ in ra}:
                vals = {next((v for nn, v in ra if _norm(nn) == n), None) for ra in raw_attrs}
                if len(vals) > 1:
                    keep.add(n)
            raw_attrs = [[(n, v) for n, v in ra if _norm(n) in keep] for ra in raw_attrs]
        else:
            raw_attrs = [[(n, v) for n, v in ra if ":" in f"{n}:" and _norm(n) not in ("SECENEK",)
                          and _norm(v) not in ("NAME", "VALUE", "")] for ra in raw_attrs]
        group_name = _common_title([_g(i, "title") for i in its])
        variants = []
        for it, ds, attrs0 in zip(its, details, raw_attrs):
            attrs = list(attrs0)
            names = {_norm(n) for n, _ in attrs}
            for d in ds:
                if d["name"] in varying and _norm(d["name"]) not in names:
                    attrs.append((d["name"], d["value"]))
            color = size = None
            extra = []
            for n, v in attrs:
                nn = _norm(n)
                if color is None and any(k in nn for k in _COLOR_KEYS):
                    color = v
                elif size is None and any(k in nn for k in _SIZE_KEYS):
                    size = v
                else:
                    extra.append(v)
            if size is None and extra:
                size = " / ".join(extra)
            if len(its) > 1 and color is None and size is None:
                # Ayırt edici özellik yok → başlıktaki fark varyant adı olur ("Kolye 45 cm" → "45 cm")
                t = _g(it, "title")
                rest = t[len(group_name):].strip(" -–,/|") if group_name and t.lower().startswith(group_name.lower()) else t
                size = rest or t
                attrs.append(("Seçenek", size))
            avail = _g(it, "availability").lower().replace("_", " ")
            qty = None
            for t in ("quantity", "stock_quantity", "stock"):
                if _g(it, t):
                    qty = _num(_g(it, t))
                    break
            in_stock = avail in ("in stock", "instock", "stokta", "preorder", "backorder") if avail else (qty or 0) > 0
            stock = int(qty) if qty is not None else (DEFAULT_STOCK if in_stock else 0)
            if not in_stock:
                stock = 0
            price = _price_num(_g(it, "price"))
            sale = _price_num(_g(it, "sale_price"))
            f = {_key(c.tag): html.unescape(_txt(c)) for c in it if isinstance(c.tag, str)
                 and not [g for g in c if isinstance(g.tag, str)]}
            variants.append({
                "urun_id": _g(it, "id"),
                "stock_code": _g(it, "mpn") or "",
                "barcode": _g(it, "gtin") or "",
                "stock": max(0, stock),
                "price": price, "sale_price": sale,
                "purchase_price": None, "market_price": None,
                "vat_rate": None, "vat_included": True,
                "currency": (re.findall(r"[A-Z]{3}", _g(it, "price")) or ["TRY"])[0],
                "desi": None,
                "color": color, "size": size,
                "options": [{"name": n, "value": v} for n, v in attrs],
                "title": _g(it, "title"),
                "link": _g(it, "link"),
                "availability": avail,
                "fields": f,
                "raw": _el_to_obj(it),
            })
        imgs: List[str] = []
        for it in its:
            for u in [_g(it, "image_link")] + _g_all(it, "additional_image_link"):
                u = (u or "").strip()
                if u.startswith("//"):
                    u = "https:" + u
                if u.startswith("http") and u not in imgs:
                    imgs.append(u)
        ptype = _g(first, "product_type")
        if not ptype:
            gpc = _g(first, "google_product_category")
            if gpc and not gpc.strip().isdigit():
                ptype = gpc
        cat_path = [x.strip() for x in re.split(r"\s*(?:>|»|/|\\|\|)\s*", ptype) if x.strip()]
        cat_path = [x for x in cat_path if _norm(x) not in ("ANA SAYFA", "ANASAYFA", "HOME", "TUM URUNLER")]
        tech_seen, tech = set(), []
        for d in details[0]:
            if d["name"] in varying:
                continue
            key = (d["name"], d["value"])
            if key not in tech_seen:
                tech_seen.add(key)
                tech.append({"name": d["name"], "value": d["value"], **({"section": d["section"]} if d["section"] else {})})
        name = group_name or _common_title([v["title"] for v in variants])
        fv = variants[0]
        fields = {
            # Ticimax kolon adlarıyla (ürün kartında görünür)
            "URUNKARTIID": gid, "URUNID": fv["urun_id"], "URUNADI": name,
            "ACIKLAMA": _g(first, "description"), "MARKA": _g(first, "brand"),
            "STOKKODU": fv["stock_code"], "BARKOD": fv["barcode"],
            "SATISFIYATI": str(fv["price"] or ""), "INDIRIMLIFIYAT": str(fv["sale_price"] or ""),
            "BREADCRUMBKAT": " > ".join(cat_path), "PARABIRIMI": fv["currency"],
            "ADWORDSKATEGORI": _g(first, "google_product_category"),
            "OZELALAN1": _g(first, "custom_label_0"), "OZELALAN2": _g(first, "custom_label_1"),
            "OZELALAN3": _g(first, "custom_label_2"), "OZELALAN4": _g(first, "custom_label_3"),
            "OZELALAN5": _g(first, "custom_label_4"),
            "URUNURL": _g(first, "link"),
            "KARGOUCRETI": str(_price_num(_txt(_child(_child(first, "shipping") if _child(first, "shipping") is not None else first, "price"))) or ""),
        }
        # Feed'in kendi alanları da eksiksiz (FEED_ önekiyle)
        for k, v in fv["fields"].items():
            fields.setdefault(f"FEED_{k}", v)
        products.append({
            "card_id": gid,
            "name": name,
            "fields": {k: v for k, v in fields.items() if v not in (None, "")},
            "images": imgs,
            "category_paths": [cat_path] if cat_path else [],
            "technical": tech,
            "variants": variants,
            "raw": {"items": [v["raw"] for v in variants]},
        })
    return products


def feed_samples(content: bytes, n: int = 2) -> Dict[str, Any]:
    """Tanı: ilk n kaydın alan değerleri (kısaltılmış) + bazı alanlarda görülen farklı değerler."""
    from lxml import etree
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=True, recover=True)
    root = etree.fromstring(content, parser=parser)
    items = [e for e in root.iter() if isinstance(e.tag, str) and _key(e.tag) == "ITEM"]
    samples = []
    for it in items[:n]:
        row = []
        for c in it:
            if not isinstance(c.tag, str) or _key(c.tag) == "DESCRIPTION":
                continue
            kids = [g for g in c if isinstance(g.tag, str)]
            val = " | ".join(f"{_key(g.tag)}={_txt(g)}" for g in kids) if kids else _txt(c)
            row.append(f"{c.tag.split('}')[-1]}: {val[:90]}")
        samples.append(row)
    distinct: Dict[str, Dict[str, int]] = {}
    for it in items[:3000]:
        for tag in ("availability", "additional_variant_attribute", "custom_label_0", "custom_label_1",
                    "custom_label_2", "custom_label_3", "custom_label_4", "product_type"):
            for c in _children(it, tag):
                v = _txt(c)[:60]
                d = distinct.setdefault(tag, {})
                d[v] = d.get(v, 0) + 1
    groups: Dict[str, int] = {}
    for it in items:
        gid = _txt(_child(it, "item_group_id"))
        groups[gid] = groups.get(gid, 0) + 1
    return {"samples": samples,
            "distinct": {k: sorted(v.items(), key=lambda kv: -kv[1])[:8] for k, v in distinct.items()},
            "groups": len(groups), "items": len(items)}


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
        "currency": f.get("PARABIRIMIKODU") or f.get("PARABIRIMI") or "",
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
            name = _txt(_child(d, "OzellikAdi", "OzellikTanim", "Tanim", "Ad", "Name"))
            val = _txt(_child(d, "OzellikDegeri", "DegerTanim", "Deger", "Value")) or val
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


# ───────────────────── TicimaxXmlV2 "Tek Link" (düz) biçimi ─────────────────────
# Her <Urun> bir VARYANTTIR (VaryasyonID/StokKodu/Barkod ürün düzeyinde); aynı UrunKartiID'li kayıtlar
# tek üründür. Varyasyon değeri, varyasyon grubunun XML kodu adındaki etikette gelir (kod tanımsızsa
# Ticimax "VARYASYON_GRUBU_XMLKOD_KONTROL_EDINIZ" yazar). Resimler ayrı dosyadadır (UrunResimleri.xml).

_FLAT_VARIANT_KEYS = ("AKTIF", "VARYASYONID", "STOKKODU", "BARKOD", "STOKADEDI", "ALISFIYATI", "SATISFIYATI",
                      "INDIRIMLIFIYAT", "PIYASAFIYATI", "KDVDAHIL", "KDVORANI", "PARABIRIMI", "PARABIRIMIKODU",
                      "DESI", "KARGOAGIRLIGI")
_FLAT_PRODUCT_KEYS = ("URUNKARTIID", "URUNADI", "ONYAZI", "ACIKLAMA", "MARKA", "MARKAID", "SATISBIRIMI",
                      "KATEGORIID", "KATEGORI", "KATEGORITREE", "URUNURL", "TEDARIKCI", "TEDARIKCIID",
                      "RESIMLER", "TEKNIKDETAYLAR", "GTIPKODU", "ANAHTARKELIME")
_FLAT_OPTION_PLACEHOLDER = "VARYASYON_GRUBU_XMLKOD_KONTROL_EDINIZ"
V2_URL_RE = re.compile(r"^(https?://[^/]+/TicimaxXmlV2/[A-Za-z0-9]+)/?$", re.I)


def _is_flat_layout(urunler: list) -> bool:
    head = urunler[:50]
    if any(_child(u, "UrunSecenek", "Secenekler", "Varyasyonlar") is not None for u in head):
        return False
    return any(_child(u, "VaryasyonID") is not None for u in head)


def _parse_flat_products(urunler: list) -> List[Dict[str, Any]]:
    groups: Dict[str, list] = {}
    for u in urunler:
        cid = _txt(_child(u, "UrunKartiID"))
        groups.setdefault(cid, []).append(u)
    out: List[Dict[str, Any]] = []
    for cid, els in groups.items():
        flats = [_flat_scalars(e) for e in els]
        # Değeri grup içinde DEĞİŞEN, bilinmeyen yaprak etiket → varyasyon ekseni
        opt_keys: List[str] = []
        for k in {k for f in flats for k in f}:
            if k == _FLAT_OPTION_PLACEHOLDER or k.startswith("VARYASYON_GRUBU"):
                opt_keys.append(k)
        if len(els) > 1:
            for k in sorted({k for f in flats for k in f}):
                if k in _FLAT_VARIANT_KEYS or k in _FLAT_PRODUCT_KEYS or k in opt_keys \
                        or k.startswith(("SEO", "OZELALAN", "UYETIPI", "FIYAT")):
                    continue
                vals = {f.get(k, "") for f in flats}
                if len(vals) > 1:
                    opt_keys.append(k)
        rows = []
        for e, f in zip(els, flats):
            opts = [(("Beden" if k == _FLAT_OPTION_PLACEHOLDER or k.startswith("VARYASYON_GRUBU")
                      else k.replace("_", " ").title()), f[k]) for k in opt_keys if f.get(k)]
            rows.append((e, f, opts))
        if len(rows) > 1:
            # Varyasyonlu ürünün seçeneksiz PASİF "ana" kaydı satılabilir varyant değildir
            kept = [r for r in rows if r[2] or _bool(r[1].get("AKTIF")) is not False]
            rows = kept or rows[:1]
        first_el = rows[0][0]
        base = _parse_product(first_el)
        variants = []
        for e, f, opts in rows:
            v = _parse_variant(e)
            color = size = None
            extra: List[str] = []
            for name, val in opts:
                n = _norm(name)
                if color is None and any(k in n for k in _COLOR_KEYS):
                    color = val
                elif size is None and any(k in n for k in _SIZE_KEYS):
                    size = val
                else:
                    extra.append(val)
            if size is None and extra:
                size = " / ".join(extra)
            vf = {k: f[k] for k in f if k in _FLAT_VARIANT_KEYS or k in opt_keys}
            v.update({"color": color, "size": size, "options": [{"name": n, "value": x} for n, x in opts],
                      "fields": vf, "raw": dict(vf)})
            variants.append(v)
        pf = {k: v for k, v in base["fields"].items() if k not in opt_keys}
        base.update({"card_id": cid, "fields": pf, "variants": variants})
        out.append(base)
    return out


def parse_images_feed(content: bytes) -> Dict[str, List[str]]:
    """UrunResimleri.xml → {UrunKartiID: [resim adresleri]} (dosyadaki sırayla)."""
    from lxml import etree
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False,
                             huge_tree=True, recover=True, remove_comments=True)
    root = etree.fromstring(content, parser=parser)
    out: Dict[str, List[str]] = {}
    if root is None:
        return out
    for r in root.iter():
        if not isinstance(r.tag, str) or _key(r.tag) != "RESIM":
            continue
        cid = _txt(_child(r, "UrunKartiID"))
        url = _txt(_child(r, "ResimAdresi", "ResimUrl", "Url")).strip()
        if url.startswith("//"):
            url = "https:" + url
        if cid and url.startswith("http"):
            lst = out.setdefault(cid, [])
            if url not in lst:
                lst.append(url)
    return out


# ───────────────────────────── yazma ─────────────────────────────

def _seo(v: Any) -> str:
    v = str(v or "").strip()
    return "" if v in (".", "-", "0") else v


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


_seq_state: Dict[str, int] = {}


async def _new_product_id(db) -> str:
    """Önce sistemin 4 haneli kimliği; 4 haneli alan dolmaya başladıysa (rastgele deneme tutmazsa)
    çakışmasız SIRALI sayısal kimlik (10000+). Kimlik her zaman benzersiz ve sayısaldır."""
    from routes.deps import generate_short_id
    if not _seq_state.get("full"):
        cid = await generate_short_id("products")
        if cid.isdigit() and not await db.products.find_one({"id": cid}, {"_id": 1}):
            return cid
        _seq_state["full"] = 1
    if "next" not in _seq_state:
        mx = 9999
        async for d in db.products.find({}, {"_id": 0, "id": 1}):
            v = str(d.get("id") or "")
            if v.isdigit():
                mx = max(mx, int(v))
        _seq_state["next"] = mx + 1
    while True:
        cid = str(_seq_state["next"])
        _seq_state["next"] += 1
        if not await db.products.find_one({"id": cid}, {"_id": 1}):
            return cid


def _variant_doc(v: Dict[str, Any], base_price: Optional[float]) -> Dict[str, Any]:
    from routes.deps import generate_id
    vp = _with_vat(v.get("price"), v)
    return {
        "id": generate_id(),
        "size": v.get("size"),
        "color": v.get("color"),
        # `barcode` = pazaryeri (Trendyol) barkodu — Trendyol Excel eşleştirmesi doldurur.
        # Ticimax'taki (site) barkodu kimlik için `ticimax_barcode` alanında saklanır.
        "barcode": None,
        "ticimax_barcode": v.get("barcode") or None,
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
    for a, b in (("urun_id", "urun_id"), ("ticimax_barcode", "barcode"), ("barcode", "barcode")):
        x, y = str(old.get(a) or "").strip(), str(new.get(b) or "").strip()
        if x and y and x == y:
            return True
    # Stok kodu çoğu mağazada ÜRÜN düzeyindedir (tüm bedenlerde aynı) → yalnız beden de tutuyorsa eşleşir
    x, y = str(old.get("stock_code") or "").strip(), str(new.get("stock_code") or "").strip()
    if x and y and x == y and not (str(old.get("urun_id") or "").strip() and str(new.get("urun_id") or "").strip()):
        return str(old.get("size") or "") == str(new.get("size") or "")
    return False


def _is_manual(existing: Dict[str, Any], key: str) -> bool:
    manual = set(existing.get("manual_edit_fields") or []) | set(existing.get("locked_fields") or [])
    return (key in manual or bool(existing.get(f"{key}_by")) or bool(existing.get(f"{key}_manual_at"))
            or bool(existing.get(f"{key}_edited_at")))


async def import_products(db, products: List[Dict[str, Any]], *, dry_run: bool = False,
                          include_no_stock: bool = False) -> Dict[str, Any]:
    from routes.deps import generate_short_id
    from routes.products import slug_with_card_id

    now = datetime.now(timezone.utc).isoformat()
    stats = {"feed_products": len(products), "skipped_no_stock": 0, "skipped_invalid": 0,
             "created": 0, "updated": 0, "variants": 0, "categories_created": 0, "errors": 0}
    cat_cache: Dict[Tuple[Optional[str], str], str] = {}

    # Excel'den açılıp Ticimax URUNID'sine bağlanmış varyantlar: stok/fiyatı buradan güncellenir;
    # tamamı bağlı ürünlerde karşılanan kart için yeni (mükerrer) ürün açılmaz.
    linked_cards: set = set()
    if not dry_run:
        try:
            import ticimax_identity as _ti
            _lk = await _ti.sync_linked_variants(db, products, _with_vat)
            linked_cards = _lk["linked_cards"]
            stats["linked_updated"] = _lk["linked_updated"]
            stats["linked_cards"] = len(linked_cards)
        except Exception as e:
            log.warning("bağlı varyant senkronu atlandı: %s", type(e).__name__)

    for p in products:
        try:
            card_id, name = str(p.get("card_id") or "").strip(), (p.get("name") or "").strip()
            if not card_id or not name:
                stats["skipped_invalid"] += 1
                continue
            if card_id in linked_cards and not await db.products.find_one(
                    {"source": SOURCE, "urun_karti_id": card_id}, {"_id": 1}):
                stats["skipped_linked"] = stats.get("skipped_linked", 0) + 1
                continue
            f, vs = p["fields"], p["variants"]
            total_stock = sum(int(v.get("stock") or 0) for v in vs)
            existing = await db.products.find_one({"source": SOURCE, "urun_karti_id": card_id}, {"_id": 0})
            if existing is None and total_stock <= 0 and not include_no_stock:
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
                "meta_title": _seo(f.get("SEO_SAYFABASLIK") or f.get("SEOSAYFABASLIK")),
                "meta_description": _seo(f.get("SEO_SAYFAACIKLAMA") or f.get("SEOSAYFAACIKLAMA") or f.get("SEOACIKLAMA")),
                "meta_keywords": _seo(f.get("SEO_ANAHTARKELIME") or f.get("SEOANAHTARKELIME")),
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
                "ticimax_barcode": first.get("barcode") or "",
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
                    "id": await _new_product_id(db) if not dry_run else "dry",
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
                    if nv.get("barcode"):
                        ov["ticimax_barcode"] = nv["barcode"]
                    ov["ticimax_fields"] = nv.get("fields") or {}
                    ov["ticimax_xml"] = nv.get("raw") or {}
                    ov["options"] = nv.get("options") or []
                    ov["size"] = nv.get("size")
                    ov["color"] = nv.get("color")
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
                upd["sizes"] = [v["size"] for v in merged if v.get("size")]
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


SOURCE_SETTING_ID = "ticimax_xml_source"


async def get_source(db) -> Dict[str, Any]:
    """Panelden seçilmiş kalıcı XML kaynağı (varsa .env PRODUCT_XML_URL'in ÖNÜNE geçer)."""
    return await db.settings.find_one({"id": SOURCE_SETTING_ID}, {"_id": 0}) or {}


async def set_source(db, url: str, include_no_stock: bool) -> None:
    await db.settings.update_one(
        {"id": SOURCE_SETTING_ID},
        {"$set": {"id": SOURCE_SETTING_ID, "url": url, "include_no_stock": bool(include_no_stock),
                  "updated_at": datetime.now(timezone.utc).isoformat()}}, upsert=True)


async def run_import(db, url: str, *, dry_run: bool = False, describe: bool = False,
                     include_no_stock: bool = False) -> Dict[str, Any]:
    from security_fetch import safe_fetch
    res = await safe_fetch(url, max_bytes=MAX_FEED_BYTES, timeout=600, allow_redirects=3)
    if not res.ok:
        raise RuntimeError(f"XML indirilemedi: HTTP {res.status_code}")
    images: Optional[Dict[str, List[str]]] = None
    m = V2_URL_RE.match(url.strip())
    if m:   # TicimaxXmlV2: resimler ayrı dosyada
        ires = await safe_fetch(m.group(1) + "/UrunResimleri.xml", max_bytes=MAX_FEED_BYTES, timeout=600,
                                allow_redirects=3)
        if ires.ok:
            images = parse_images_feed(ires.content)
    products = parse_feed(res.content, images)
    stats = await import_products(db, products, dry_run=dry_run, include_no_stock=include_no_stock)
    stats["include_no_stock"] = bool(include_no_stock)
    stats["with_images"] = sum(1 for p in products if p.get("images"))
    if not products:
        stats["structure"] = describe_structure(res.content)
        stats["content_type"] = res.content_type
    stats["finished_at"] = datetime.now(timezone.utc).isoformat()
    if not dry_run:
        await db.settings.update_one({"id": "ticimax_xml_import_last"},
                                     {"$set": {"id": "ticimax_xml_import_last",
                                               **{k: v for k, v in stats.items() if k != "structure"}}},
                                     upsert=True)
    if describe:
        try:
            stats["feed_samples"] = feed_samples(res.content)
        except Exception as e:
            stats["feed_samples"] = {"error": type(e).__name__}
    return stats


def describe_structure(content: bytes, limit: int = 70) -> Dict[str, Any]:
    """Tanı: XML'in etiket yolu dağılımı (değer içermez) + kök bilgisi. Ürün bulunamazsa log'a yazılır."""
    info: Dict[str, Any] = {"bytes": len(content or b"")}
    head = (content or b"")[:300].decode("utf-8", "replace")
    info["starts_with"] = re.sub(r"https?://\S+", "<url>", head.replace("\n", " "))[:200]
    try:
        from lxml import etree
        parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False,
                                 huge_tree=True, recover=True)
        root = etree.fromstring(content, parser=parser)
    except Exception as e:
        info["parse_error"] = type(e).__name__
        return info
    if root is None:
        info["parse_error"] = "empty"
        return info
    counts: Dict[str, int] = {}
    attrs: Dict[str, set] = {}

    def walk(el, path, depth):
        if depth > 7:
            return
        for c in el:
            if not isinstance(c.tag, str):
                continue
            pth = path + "/" + c.tag.split("}")[-1]
            counts[pth] = counts.get(pth, 0) + 1
            if c.attrib:
                attrs.setdefault(pth, set()).update(k.split("}")[-1] for k in c.attrib)
            walk(c, pth, depth + 1)
    walk(root, root.tag.split("}")[-1], 0)
    top = sorted(counts.items(), key=lambda kv: kv[0])[:limit]
    info["root"] = root.tag.split("}")[-1]
    info["paths"] = [f"{k} x{v}" + (f" @{','.join(sorted(attrs[k]))}" if k in attrs else "") for k, v in top]
    info["total_paths"] = len(counts)
    return info
