"""
TEK SEFERLİK ERİTME FİYAT YÜKLEMESİ — "Site_Fiyat_Yükleme_2026-09-29 - eritme - developer.xlsx"
=============================================================================
Beden bazlı liste (barkodla eşleşir). Kullanıcı: "satış fiyatı, üye tipi fiyatı ve indirimli
fiyat alanı — en güncel olarak yükle":
  • Piyasa Fiyatı → etiket fiyatı (price) VE Üye Tipi Fiyatı 1 (member_price_1)
  • Satış Fiyatı  → indirimli fiyat (sale_price); Piyasa'dan ucuz değilse indirimli fiyat boş
  • Bedenler arasında farklı satış fiyatı (eritme: ör. L 440, diğerleri 970): ürüne ÇOĞUNLUK
    fiyatı yazılır, farklı bedenlere fiyat farkı (price_adjustment/price_diff) — sunucu fiyat
    hesabı (orders._eff_unit_price) ve vitrin aynı alanı kullanır.
  • Sale'de = Evet → İNDİRİM (9306) kategorisine eklenir; Hayır → çıkarılır.
Stok/ad/görsel vb. değişmez. Sonuç: settings.fiyat_eritme_0929.
"""
from datetime import datetime, timezone

FLAG = "fiyat_eritme_0929"
SALE_CAT = "9306"

# (ürün id, stok kodu, piyasa, indirimli|None, sale'de?, {barkod: beden fiyat farkı})
PLAN = [
    ('8498', 'FCSS0900001', 1290, 1160, True, {'8684483517433': 0.0, '8684483517426': 0.0, '8684483517419': 0.0}),  # Yandan Bağlamalı Poplin Gömlek Açık Pembe
    ('2066', 'FCSS0900001', 1290, 1030, True, {'8684483517372': 0.0, '8684483517365': 0.0, '8684483517358': 0.0}),  # Yandan Bağlamalı Poplin Gömlek Sarı
    ('5014', 'FCFW0900001', 1290, 1030, True, {'8684483517341': 0.0}),  # Kolu Volanlı Gömlek Sarı
    ('9740', 'FCFW0900001', 1290, 1030, True, {'8684483517297': 0.0}),  # Kolu Volanlı Gömlek Beyaz
    ('3787', 'FCFW1800003', 1650, 1320, True, {'8684483518690': 0.0, '8684483518515': 0.0, '8684483518508': 0.0, '8684483518492': 0.0, '8684483518522': 0.0}),  # Yandan Bağlamalı Palazzo Pantolon Acı Kahve
    ('5969', 'FCFW1800003', 1650, 1320, True, {'8684483518676': 0.0, '8684483518300': 0.0, '8684483518294': 0.0, '8684483518287': 0.0, '8684483518317': 0.0}),  # Yandan Bağlamalı Palazzo Pantolon Siyah
    ('1983', 'FCFW1800002', 1490, 970, True, {'8684483518607': 0.0, '8684483518591': 0.0, '8684483518584': 0.0, '8684483518577': -530.0}),  # Havlu Kumaş Pantolon Kırmızı
    ('7000', 'FCFW1800002', 1490, 970, True, {'8684483518485': -370.0, '8684483518478': 0.0, '8684483518461': 0.0, '8684483518454': -530.0}),  # Havlu Kumaş Pantolon Pembe
    ('8632', 'FCFW1800002', 1490, 970, True, {'8684483518232': 0.0, '8684483518225': 0.0, '8684483518218': 0.0, '8684483518201': -530.0}),  # Havlu Kumaş Pantolon Bej
    ('6941', 'FCFW0700001', 1190, 950, True, {'8684483518560': 0.0, '8684483518553': 0.0, '8684483518546': 0.0, '8684483518539': -460.0}),  # Havlu Kumaş Etek Kırmızı
    ('4235', 'FCFW0700001', 1190, 950, True, {'8684483518447': 0.0, '8684483518430': -460.0, '8684483518423': 0.0, '8684483518416': -460.0}),  # Havlu Kumaş Etek Pembe
    ('5483', 'FCFW0700001', 1190, 950, True, {'8684483518195': 0.0, '8684483518188': 0.0, '8684483518171': 0.0, '8684483518164': -460.0}),  # Havlu Kumaş Etek Bej
    ('3773', 'FCFW0600002', 2400, 1920, True, {'8684483518362': 0.0, '8684483518355': 0.0, '8684483518348': 0.0, '8684483518331': 0.0}),  # Dokulu Blazer Etek Takım Beyaz
    ('6109', 'FCFW0100003', 1590, 1270, True, {'8684483518072': 0.0, '8684483518058': 0.0, '8684483518041': 0.0, '8684483518034': 0.0, '8684483518065': 0.0}),  # Şal Detaylı Bluz Haki
    ('9785', 'FCFW0100003', 1590, 1270, True, {'8684483518126': 0.0, '8684483518102': 0.0, '8684483518096': 0.0, '8684483518089': 0.0, '8684483518119': 0.0}),  # Şal Detaylı Bluz Acı Kahve
    ('5273', 'FCFW0100003', 1590, 1270, True, {'8684483517983': 0.0, '8684483517969': 0.0, '8684483517952': 0.0, '8684483517945': 0.0, '8684483517976': 0.0}),  # Şal Detaylı Bluz Siyah
    ('5852', 'FCFW0100003', 1590, 1430, True, {'8684483517938': 0.0, '8684483517914': 0.0, '8684483517907': 0.0, '8684483517891': 0.0, '8684483517921': 0.0}),  # Şal Detaylı Bluz Bej
    ('2607', 'FCFW0400003', 2390, 2030, True, {'8684483518966': 0.0, '8684483518959': 0.0, '8684483518942': 0.0, '8684483518935': 0.0}),  # Çizgili Belden Oturmalı Fular Detay Blazer Acı Kahve
    ('8980', 'FCFW0400003', 2390, 2030, True, {'8684483518805': 0.0, '8684483518799': 0.0, '8684483518782': 0.0, '8684483518775': 0.0}),  # Çizgili Belden Oturmalı Fular Detay Blazer Lacivert
    ('5076', 'FCFW0900002', 1290, 1100, True, {'8684483519338': 0.0}),  # Oversize Poplin Gömlek Acı Kahve
    ('3068', 'FCFW0900002', 1290, 1100, True, {'8684483519314': 0.0}),  # Oversize Poplin Gömlek Sarı
    ('8226', 'FCFW0900002', 1290, 1100, True, {'8684483519291': 0.0}),  # Oversize Poplin Gömlek Bordo
    ('9115', 'FCFW0900002', 1290, 1160, True, {'8684483519277': 0.0}),  # Oversize Poplin Gömlek Beyaz
    ('9032', 'FCFW0900002', 1290, 1100, True, {'8684483519253': 0.0}),  # Oversize Poplin Gömlek Haki
    ('4313', 'FCFW0900002', 1290, 1100, True, {'8684483519239': 0.0}),  # Oversize Poplin Gömlek Siyah
    ('4540', 'FCFW0400005', 2190, 1750, True, {'8684483519468': 0.0, '8684483519451': 0.0, '8684483519444': 0.0, '8684483519437': 0.0}),  # Suni Deri Bomber Ceket Bej
    ('6697', 'FCFW0400005', 2190, 1970, True, {'8684483519543': 0.0, '8684483519536': 0.0, '8684483519529': 0.0, '8684483519512': 0.0}),  # Suni Deri Bomber Ceket Gri
    ('4162', 'FCFW1800007', 1790, 1520, True, {'8684483519505': 0.0, '8684483519499': 0.0, '8684483519482': 0.0, '8684483519475': 0.0}),  # Suni Deri Yırtmaç Detay Pantolon Bej
    ('1594', '1440', 2290, 1830, True, {'8684483520075': 0.0, '8684483520068': 0.0, '8684483520051': 0.0}),  # Apoletli Kısa Kaşe Kaban Siyah
    ('1600', 'FCFW0600007', 1890, None, False, {'8684483520730': 0.0}),  # Önden Düğmeli Triko Elbise Acı Kahve
    ('4489', 'FCFW0600007', 1890, None, False, {'8684483520594': 0.0}),  # Önden Düğmeli Triko Elbise Vizon
    ('2221', 'FCSS0700001', 1590, 1270, True, {'8683851001697': -380.0, '8684483522307': 0.0, '8684483522291': 0.0, '8684483522284': 0.0, '8684483522277': 0.0}),  # Vela Kareli Midi Etek Bej
    ('4828', 'FCSS2100002', 1090, None, False, {'8684483524844': 0.0, '8684483524837': 0.0, '8684483524820': 0.0, '8684483524851': 0.0}),  # Core Dik Yaka Kısa Kollu T-Shirt Kırmızı
    ('3732', 'FCSS2100002', 1090, None, False, {'8684483524714': 0.0, '8684483524707': 0.0, '8684483524691': 0.0, '8684483524721': 0.0}),  # Core Dik Yaka Kısa Kollu T-Shirt Sarı
    ('5205', 'FCSS2100002', 1090, None, False, {'8684483524622': 0.0, '8684483524615': 0.0, '8684483524608': 0.0, '8684483524639': 0.0}),  # Core Dik Yaka Kısa Kollu T-Shirt Ekru
    ('8685', 'FCSS2100002', 1090, None, False, {'8684483524547': 0.0, '8684483524530': 0.0, '8684483524523': 0.0, '8684483524554': 0.0}),  # Core Dik Yaka Kısa Kollu T-Shirt Siyah
    ('3791', 'FCSS2100002', 1090, None, False, {'8684483524417': 0.0, '8684483524400': 0.0, '8684483524394': 0.0, '8684483524424': 0.0}),  # Core Dik Yaka Kısa Kollu T-Shirt Bej
    ('3663', 'FCSS2100002', 1090, None, False, {'8684483524370': 0.0, '8684483524363': 0.0, '8684483524356': 0.0, '8684483524387': 0.0}),  # Core Dik Yaka Kısa Kollu T-Shirt Mavi
]


async def apply(db) -> dict:
    if ((await db.settings.find_one({"id": FLAG}, {"_id": 0, "done": 1})) or {}).get("done"):
        return {"skipped": "done"}
    now = datetime.now(timezone.utc).isoformat()
    changed, missing, added, removed, var_missing = [], [], 0, 0, []
    for pid, sc, price, sale, evet, adj_by_bc in PLAN:
        p = await db.products.find_one({"id": pid}, {"_id": 0, "id": 1, "stock_code": 1, "price": 1, "sale_price": 1,
                                                     "variants": 1, "categories": 1, "category_ids": 1,
                                                     "category_id": 1})
        if not p or str(p.get("stock_code") or "").strip() != sc:
            missing.append(f"{pid} {sc}")
            continue
        price = float(price)
        sale = float(sale) if (sale and 0 < float(sale) < price) else None
        base = sale if sale else price
        st = {"price": price, "sale_price": sale, "member_price_1": price, "updated_at": now}
        vs, seen = [], set()
        for v in (p.get("variants") or []):
            if not isinstance(v, dict):
                vs.append(v)
                continue
            v = dict(v)
            b = str(v.get("barcode") or "").strip()
            if b in adj_by_bc:
                adj = float(adj_by_bc[b])
                v["price_adjustment"] = adj
                v["price_diff"] = adj
                seen.add(b)
            else:
                try:
                    adj = float(v.get("price_adjustment") or v.get("price_diff") or 0)
                except Exception:
                    adj = 0.0
            v["member_price_1"] = round(price, 2)
            if v.get("price") not in (None, "", 0) or v.get("sale_price") not in (None, "", 0) or b in adj_by_bc:
                v["price"] = round(price, 2)
                v["sale_price"] = round(base + adj, 2) if (sale or adj) else None
            vs.append(v)
        var_missing += [b for b in adj_by_bc if b not in seen]
        st["variants"] = vs
        cats = [str(x) for x in (p.get("categories") or [])]
        cids = [str(x) for x in (p.get("category_ids") or [])]
        in_sale = SALE_CAT in cats or SALE_CAT in cids
        if evet and not in_sale:
            st["categories"] = cats + [SALE_CAT]
            st["category_ids"] = cids + [SALE_CAT]
            added += 1
        elif not evet and in_sale:
            st["categories"] = [x for x in cats if x != SALE_CAT]
            st["category_ids"] = [x for x in cids if x != SALE_CAT]
            if str(p.get("category_id") or "") == SALE_CAT:
                st["category_id"] = (st["categories"] or st["category_ids"] or [None])[0]
            removed += 1
        await db.products.update_one({"id": pid}, {"$set": st})
        changed.append({"id": pid, "eski": [p.get("price"), p.get("sale_price")], "yeni": [price, sale],
                        "beden_farki": {k: x for k, x in adj_by_bc.items() if x}})
    res = {"id": FLAG, "done": True, "at": now, "changed": len(changed), "missing": missing,
           "variant_missing": var_missing, "indirim_eklenen": added, "indirim_cikarilan": removed,
           "items": changed}
    await db.settings.update_one({"id": FLAG}, {"$set": res}, upsert=True)
    return {k: res[k] for k in ("changed", "missing", "variant_missing", "indirim_eklenen", "indirim_cikarilan")}
