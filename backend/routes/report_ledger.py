"""RAPOR VERİ HAVUZU + DENETÇİ (Kadir isteği 2026-10-16).

"Raporlar karışık, doğru sonuç vermiyor" → 5 Haziran 2026'dan bugüne her GÜN × PLATFORM × ÜRÜN
için sipariş / iptal / iade adet ve tutarını tek bir havuzda (db.report_ledger_daily) tutar.
Kaynak: paneldeki siparişler (raporlarla AYNI kanonik popülasyon ve AYNI iptal/iade kuralları).

İki tarih görünümü (kullanıcı kararı: ikisi de, ayrı sütun):
  • İŞLEM TARİHİ (ce_*/re_*): iptal → iptalin kesinleştiği gün, iade → gider pusulasının kesildiği gün.
  • SİPARİŞ TARİHİ / KOHORT (cc_*/rc_*): o gün verilen siparişlerden bugüne kadar iptal/iade olanlar.

Denetçi (reconcile): 1 / 7 / 30 / 60 gün ve 5 Haziran'dan bugüne pencerelerinde, her kaynak için
rapor uçlarının (sales-breakdown, products/top) verdiği rakamları havuzla karşılaştırır. Fark varsa
rapor önbelleğini düşürüp yeniden hesaplatır; hâlâ farklıysa "FARK" olarak kaydeder (panelde görünür).
Yazdığı tek yer kendi havuz/denetim koleksiyonlarıdır — sipariş verisine DOKUNMAZ.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query

from .deps import db, logger, require_permission, tr_range_to_utc

router = APIRouter(prefix="/admin/report-ledger", tags=["admin-report-ledger"])

LEDGER_START = "2026-06-05"
WINDOWS = [("1g", 1), ("7g", 7), ("30g", 30), ("60g", 60), ("5haz", None)]
SOURCES = [None, "site", "trendyol", "hepsiburada", "amazon"]
_FIELDS = ("o_units", "o_rev", "ce_units", "ce_rev", "re_units", "re_rev",
           "cc_units", "cc_rev", "rc_units", "rc_rev")
_SYSTEM_USER = {"id": "system:report_ledger", "email": "system:report_ledger", "is_super_admin": True}


def _tr_day(iso: str) -> str:
    """UTC ISO → Türkiye yerel günü (YYYY-MM-DD)."""
    try:
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return (d.astimezone(timezone.utc) + timedelta(hours=3)).date().isoformat()
    except Exception:
        return str(iso)[:10]


def _today_tr() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=3)).date().isoformat()


def _fnum(v) -> float:
    try:
        return float(v or 0)
    except Exception:
        return 0.0


def _pkey(it: dict) -> tuple:
    bc = str((it or {}).get("barcode") or "").strip()
    pid = str((it or {}).get("product_id") or "").strip()
    nm = str((it or {}).get("name") or (it or {}).get("product_name") or "").strip()
    key = f"bc:{bc}" if bc else (f"pid:{pid}" if pid else f"nm:{nm.lower()}")
    return key, nm, bc


def _lines(order: dict) -> list:
    """[(pkey, name, barcode, qty, line_amount)] — tutar siparişin toplamına oransal dağıtılır."""
    from .report_dedup import allocate_order_total
    items = [i for i in (order.get("items") or []) if isinstance(i, dict)]
    if not items:
        return [("nm:(kalemsiz)", "(kalemsiz)", "", 1, _fnum(order.get("total")))]
    raw = []
    for it in items:
        try:
            q = max(1, int(it.get("quantity") or 1))
        except Exception:
            q = 1
        p = _fnum(it.get("price") if it.get("price") not in (None, "") else it.get("unit_price"))
        raw.append((it, q, p * q))
    amounts = allocate_order_total([r[2] for r in raw], _fnum(order.get("total")))
    out = []
    for (it, q, _), amt in zip(raw, amounts):
        k, nm, bc = _pkey(it)
        out.append((k, nm, bc, q, float(amt)))
    return out


def _spread(lines: list, rev: float, units: int) -> list:
    """Bir iptal/iade tutarını + adedini siparişin kalemlerine oransal dağıtır (büyük kalan yöntemi)."""
    tot_amt = sum(l[4] for l in lines) or 0.0
    tot_q = sum(l[3] for l in lines) or 1
    out, given = [], 0
    shares = []
    for l in lines:
        w = (l[4] / tot_amt) if tot_amt > 0.005 else (l[3] / tot_q)
        exact = units * (l[3] / tot_q)
        shares.append((l, w, exact))
    floors = [int(x[2]) for x in shares]
    rem = units - sum(floors)
    order_idx = sorted(range(len(shares)), key=lambda i: -(shares[i][2] - floors[i]))
    for i in order_idx[:max(0, rem)]:
        floors[i] += 1
    for (l, w, _), u in zip(shares, floors):
        out.append((l[0], l[1], l[2], u, round(rev * w, 2)))
        given += u
    return out


async def build_ledger(start_day: str = LEDGER_START, end_day: Optional[str] = None) -> dict:
    """[start_day, end_day] günleri için havuzu BAŞTAN üretir (o günlerin eski satırları silinir)."""
    from . import reports as R
    from .report_dedup import canonical_order_stages, merge_match, effective_order_date_match
    end_day = end_day or _today_tr()
    s, e = tr_range_to_utc(start_day, end_day)
    _, now_e = tr_range_to_utc(_today_tr(), _today_tr())
    await R._ensure_dup_loaded()

    acc: dict = {}      # (day, ch, pkey) → satır
    tot: dict = {}      # (day, ch) → sipariş sayıları

    def _row(day, ch, k, nm, bc):
        r = acc.get((day, ch, k))
        if r is None:
            r = {"day": day, "ch": ch, "pkey": k, "name": nm, "barcode": bc,
                 **{f: 0 for f in _FIELDS}}
            acc[(day, ch, k)] = r
        elif nm and not r["name"]:
            r["name"] = nm
        return r

    def _t(day, ch):
        return tot.setdefault((day, ch), {"orders": 0, "cancel_orders_event": 0, "return_docs_event": 0})

    proj = {"_id": 0, "id": 1, "order_number": 1, "status": 1, "total": 1, "platform": 1,
            "marketplace": 1, "marketplace_order_date": 1, "created_at": 1, "payment_method": 1,
            "payment_status": 1, "paid_at": 1, "paid_with_gift_card_only": 1,
            "partial_cancel_amount": 1, "partial_cancel_units": 1, "partial_cancel_total_scope": 1,
            "items.quantity": 1, "items.price": 1, "items.unit_price": 1, "items.barcode": 1,
            "items.product_id": 1, "items.name": 1, "items.product_name": 1}
    # ── 1) SİPARİŞLER (sipariş günü) — raporlarla aynı kanonik popülasyon ──────────────
    omap: dict = {}
    async for o in db.orders.aggregate([
            {"$match": merge_match({"$and": [effective_order_date_match(s, e)]})},
            *canonical_order_stages(), {"$project": proj}], allowDiskUse=True):
        if R._is_unpaid_site_cancel(o):
            continue
        R._fill_cancel_total(o)
        st = str(o.get("status") or "")
        if (st in R._EXCLUDED_STATUSES and st not in R._CANCEL_STATUSES
                and st not in R._RETURN_STATUSES_BD):
            continue   # ödenmemiş grubu — ciroya girmez (sales-breakdown ile aynı)
        day = _tr_day(o.get("marketplace_order_date") or o.get("created_at"))
        ch = R._channel_of(o)
        o["_day"], o["_ch"], o["_lines"] = day, ch, _lines(o)
        omap[str(o.get("order_number") or o.get("id"))] = o
        _t(day, ch)["orders"] += 1
        for k, nm, bc, q, amt in o["_lines"]:
            r = _row(day, ch, k, nm, bc)
            r["o_units"] += q
            r["o_rev"] += amt
        # KISMİ İPTAL (aktif kapsam): kalemler/tutar yalnız KALAN paket → iptal edilen kısım da
        # sipariş verilmiş sayılır (sales-breakdown 'included' = net + iptal + iade ile aynı).
        _pc = max(0.0, _fnum(o.get("partial_cancel_amount")))
        if (_pc > 0 and st not in R._CANCEL_STATUSES
                and str(o.get("partial_cancel_total_scope") or "").lower() == "active"):
            try:
                _pcu = max(1, int(o.get("partial_cancel_units") or 1))
            except Exception:
                _pcu = 1
            for k, nm, bc, q, amt in _spread(o["_lines"], _pc, _pcu):
                r = _row(day, ch, k, nm, bc)
                r["o_units"] += q
                r["o_rev"] += amt

    # ── 2) İPTAL/İADE İŞLEMLERİ — raporların AYNI kural kümesi (_action_rows) ──────────
    rows = await R._action_rows_uncached(s, now_e)
    missing = set()
    for kind in ("cancels", "returns"):
        for r in rows.get(kind) or []:
            if r.get("onum") and r["onum"] not in omap:
                missing.add(r["onum"])
    extra: dict = {}
    ml = list(missing)
    for i in range(0, len(ml), 2000):
        async for o in db.orders.find({"order_number": {"$in": ml[i:i + 2000]}}, proj):
            R._fill_cancel_total(o)
            o["_day"] = _tr_day(o.get("marketplace_order_date") or o.get("created_at"))
            o["_ch"] = R._channel_of(o)
            o["_lines"] = _lines(o)
            extra[str(o.get("order_number"))] = o
    # İade satırlarını ÜRÜNE bağlamak için pusula kalemleri (barkod) — numaralı pusulalar.
    pus: dict = {}
    ron = list({r.get("onum") for r in rows.get("returns") or [] if r.get("onum")})
    for i in range(0, len(ron), 2000):
        async for g in db.gider_pusulasi.find(
                {"order_number": {"$in": ron[i:i + 2000]}, **R._NUMBERED_PUSULA},
                {"_id": 0, "order_number": 1, "created_at": 1, "items": 1}):
            pus[(str(g.get("order_number")), str(g.get("created_at") or ""))] = g.get("items") or []

    for kind, ev_u, ev_r, co_u, co_r in (("cancels", "ce_units", "ce_rev", "cc_units", "cc_rev"),
                                         ("returns", "re_units", "re_rev", "rc_units", "rc_rev")):
        for r in rows.get(kind) or []:
            onum = r.get("onum") or ""
            o = omap.get(onum) or extra.get(onum)
            ch = r.get("ch") or (o or {}).get("_ch") or "site"
            ev_day = _tr_day(r.get("at"))
            rev, units = _fnum(r.get("rev")), int(r.get("units") or 0)
            parts = None
            if kind == "returns":
                pl = pus.get((onum, str(r.get("at") or "")))
                if pl:
                    prod = [it for it in pl if R._pusula_line_kind(it) == "urun"]
                    if prod:
                        base = []
                        for it in prod:
                            k, nm, bc = _pkey(it)
                            q = max(1, int((it or {}).get("quantity") or 1))
                            base.append((k, nm, bc, q, _fnum((it or {}).get("net_price") or (it or {}).get("unit_price")) * q))
                        parts = _spread(base, rev, units)
            if parts is None:
                lines = (o or {}).get("_lines") or [("nm:(sipariş bulunamadı)", "(sipariş bulunamadı)", "", 1, rev)]
                parts = _spread(lines, rev, units)
            in_win = start_day <= ev_day <= end_day
            if in_win:
                _t(ev_day, ch)["cancel_orders_event" if kind == "cancels" else "return_docs_event"] += 1
            for k, nm, bc, u, amt in parts:
                if in_win:
                    x = _row(ev_day, ch, k, nm, bc)
                    x[ev_u] += u
                    x[ev_r] += amt
                # Kohort: yalnız havuz penceresinde VERİLMİŞ siparişler (sipariş günü + kanalı)
                if o is not None and onum in omap:
                    y = _row(o["_day"], o["_ch"], k, nm, bc)
                    y[co_u] += u
                    y[co_r] += amt

    # ── 3) YAZ — o günlerin eski satırları silinip yenileri yazılır ────────────────────
    now = datetime.now(timezone.utc).isoformat()
    docs = []
    for r in acc.values():
        for f in _FIELDS:
            if f.endswith("_rev"):
                r[f] = round(r[f], 2)
        r["kind"] = "product"
        r["built_at"] = now
        docs.append(r)
    for (day, ch), t in tot.items():
        docs.append({"kind": "total", "day": day, "ch": ch, **t, "built_at": now})
    # Yalnız silinen gün aralığı yazılır: iptal/iade olay günleri end_day'den sonra düşebilir;
    # aralık dışı satırlar yazılırsa o günlerde ÇİFT kayıt oluşur (tek gün tazelemede).
    docs = [d for d in docs if start_day <= str(d.get("day") or "") <= end_day]
    await db.report_ledger_daily.delete_many({"day": {"$gte": start_day, "$lte": end_day}})
    for i in range(0, len(docs), 1000):
        await db.report_ledger_daily.insert_many([dict(d) for d in docs[i:i + 1000]])
    await db.settings.update_one({"id": "report_ledger_state"},
                                 {"$set": {"id": "report_ledger_state", "built_at": now,
                                           "range": [start_day, end_day], "rows": len(docs)}},
                                 upsert=True)
    return {"rows": len(docs), "orders": len(omap), "range": [start_day, end_day]}


def _window(days: Optional[int]) -> tuple:
    end = _today_tr()
    if days is None:
        return LEDGER_START, end
    start = (datetime.fromisoformat(end) - timedelta(days=days - 1)).date().isoformat()
    return max(start, LEDGER_START), end


async def ledger_sums(start: str, end: str, source: Optional[str] = None) -> dict:
    m = {"kind": "product", "day": {"$gte": start, "$lte": end}}
    if source:
        m["ch"] = source
    g = {"_id": None, **{f: {"$sum": f"${f}"} for f in _FIELDS}}
    r = await db.report_ledger_daily.aggregate([{"$match": m}, {"$group": g}]).to_list(1)
    out = {f: 0 for f in _FIELDS}
    if r:
        out.update({f: (round(r[0][f], 2) if f.endswith("_rev") else int(r[0][f])) for f in _FIELDS})
    tm = {"kind": "total", "day": {"$gte": start, "$lte": end}}
    if source:
        tm["ch"] = source
    t = await db.report_ledger_daily.aggregate([{"$match": tm}, {"$group": {
        "_id": None, "orders": {"$sum": "$orders"}}}]).to_list(1)
    out["orders"] = int(t[0]["orders"]) if t else 0
    return out


# (rapor alanı adı, rapor değerini alan fonksiyon, havuz alanı, tolerans)
_CHECKS = [
    ("Ciro (iptal+iade dahil) adet", lambda b: b["included"]["units"], "o_units", 0),
    ("Ciro (iptal+iade dahil) tutar", lambda b: b["included"]["revenue"], "o_rev", 1.0),
    ("Sipariş sayısı", lambda b: b["included"]["orders"], "orders", 0),
    ("İptal (işlem tarihi) adet", lambda b: b["cancels"]["units"], "ce_units", 0),
    ("İptal (işlem tarihi) tutar", lambda b: b["cancels"]["revenue"], "ce_rev", 1.0),
    ("İade (işlem tarihi) adet", lambda b: b["returns"]["units"], "re_units", 0),
    ("İade (işlem tarihi) tutar", lambda b: b["returns"]["revenue"], "re_rev", 1.0),
    ("İptal (sipariş tarihi) adet", lambda b: b["cancels_kohort"]["units"], "cc_units", 0),
    ("İptal (sipariş tarihi) tutar", lambda b: b["cancels_kohort"]["revenue"], "cc_rev", 1.0),
    ("İade (sipariş tarihi) adet", lambda b: b["returns_kohort"]["units"], "rc_units", 0),
    ("İade (sipariş tarihi) tutar", lambda b: b["returns_kohort"]["revenue"], "rc_rev", 1.0),
]


def _drop_report_cache() -> None:
    """Denetçi rapor rakamını TAZE hesaplatır (bayat-ama-yakın önbellek karşılaştırmayı yanıltmasın)."""
    from . import reports as R
    R._CTX_CACHE.clear()
    R._EP_CACHE.clear()
    try:
        R._ACT_CACHE.clear()
    except Exception:
        pass


async def _compare_once(start: str, end: str, source: Optional[str]) -> list:
    from .reports import sales_breakdown
    b = await sales_breakdown(start_date=start, end_date=end, source=source, current_user=_SYSTEM_USER)
    L = await ledger_sums(start, end, source)
    out = []
    for label, get, f, tol in _CHECKS:
        try:
            rv = get(b)
        except Exception:
            rv = None
        lv = L.get(f)
        ok = rv is not None and abs(_fnum(rv) - _fnum(lv)) <= tol
        out.append({"alan": label, "rapor": rv, "havuz": lv, "ok": bool(ok),
                    "fark": None if rv is None else round(_fnum(rv) - _fnum(lv), 2)})
    return out


async def reconcile() -> dict:
    """DENETÇİ: tüm pencere × kaynak hücrelerini karşılaştırır; farkta raporu yeniden hesaplatır."""
    from .reports import invalidate_report_cache
    _drop_report_cache()
    cells = []
    for wname, days in WINDOWS:
        start, end = _window(days)
        for src in SOURCES:
            try:
                res = await _compare_once(start, end, src)
                recomputed = False
                if not all(x["ok"] for x in res):
                    await invalidate_report_cache()          # bayat önbellek olasılığını ele
                    _drop_report_cache()
                    res = await _compare_once(start, end, src)
                    recomputed = True
                cells.append({"pencere": wname, "baslangic": start, "bitis": end,
                              "kaynak": src or "tumu", "ok": all(x["ok"] for x in res),
                              "yeniden_hesaplandi": recomputed, "alanlar": res})
            except Exception as ex:
                cells.append({"pencere": wname, "baslangic": start, "bitis": end,
                              "kaynak": src or "tumu", "ok": False, "hata": str(ex)[:300], "alanlar": []})
    now = datetime.now(timezone.utc).isoformat()
    doc = {"id": "last", "at": now, "hucre": len(cells),
           "farkli": sum(1 for c in cells if not c["ok"]), "cells": cells}
    await db.report_reconciliation.replace_one({"id": "last"}, doc, upsert=True)
    if doc["farkli"]:
        logger.warning(f"[rapor-denetci] {doc['farkli']}/{doc['hucre']} hücrede rapor ≠ havuz")
    return doc


async def run_ledger_and_reconcile(full: bool = False, manual: bool = False) -> dict:
    """Zamanlayıcı. full → 5 Haziran'dan TAM yeniden kurulum + denetçi (gece 1 kez).
    Değilse (30 dk) yalnız DÜN+BUGÜN tazelenir, denetçi çalışmaz — veritabanı maliyeti (Atlas veri
    transferi/CPU): eskiden her 30 dk son 62 gün okunup raporlar 25 kez yeniden hesaplanıyordu."""
    if full or manual:
        # Panelden elle "Denetle": son 62 gün + denetçi (eski davranış, yalnız istenince).
        b = await build_ledger(LEDGER_START if full else max(LEDGER_START, _window(62)[0]))
        r = await reconcile()
        return {"ledger": b, "farkli": r["farkli"], "hucre": r["hucre"]}
    win_start = max(LEDGER_START, _window(2)[0])
    b = await build_ledger(win_start)
    # GEÇ ONAY: havale/EFT siparişi günler sonra onaylanınca ciroya SİPARİŞ GÜNÜYLE girer (kullanıcı
    # kararı). O gün 2 günlük pencerenin dışındaysa gece tam kurulumunu beklemeden burada tazelenir.
    late_days = await _late_paid_order_days(win_start)
    for d in late_days:
        await build_ledger(d, d)
    return {"ledger": b, "late_paid_days": late_days, "denetci": "gece tam kurulumda"}


async def _late_paid_order_days(win_start: str, limit: int = 10) -> list:
    """Son 2 günde ÖDENDİ işaretlenen ama sipariş günü pencereden önce olan siparişlerin günleri."""
    since = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    days = set()
    async for o in db.orders.find({"paid_at": {"$gte": since}},
                                  {"_id": 0, "created_at": 1, "marketplace_order_date": 1}).limit(500):
        d = _tr_day(o.get("marketplace_order_date") or o.get("created_at"))
        if d and LEDGER_START <= d < win_start:
            days.add(d)
    return sorted(days)[-limit:]


# ───────────────────────────── API (panel: Rapor Doğrulama) ─────────────────────────────
@router.get("/summary")
async def ledger_summary(window: str = Query("30g"), source: Optional[str] = None,
                         current_user: dict = Depends(require_permission("reports.sales"))):
    days = dict(WINDOWS).get(window, 30)
    start, end = _window(days)
    src = source if source in SOURCES and source else None
    sums = await ledger_sums(start, end, src)
    m = {"kind": "product", "day": {"$gte": start, "$lte": end}}
    if src:
        m["ch"] = src
    prods = await db.report_ledger_daily.aggregate([
        {"$match": m},
        {"$group": {"_id": "$pkey", "name": {"$first": "$name"}, "barcode": {"$first": "$barcode"},
                    **{f: {"$sum": f"${f}"} for f in _FIELDS}}},
        {"$sort": {"o_units": -1}}, {"$limit": 500}]).to_list(500)
    daily = await db.report_ledger_daily.aggregate([
        {"$match": m},
        {"$group": {"_id": "$day", **{f: {"$sum": f"${f}"} for f in _FIELDS}}},
        {"$sort": {"_id": -1}}]).to_list(400)
    by_ch = await db.report_ledger_daily.aggregate([
        {"$match": {"kind": "product", "day": {"$gte": start, "$lte": end}}},
        {"$group": {"_id": "$ch", **{f: {"$sum": f"${f}"} for f in _FIELDS}}},
        {"$sort": {"o_units": -1}}]).to_list(20)
    st = await db.settings.find_one({"id": "report_ledger_state"}, {"_id": 0}) or {}
    return {"window": window, "start": start, "end": end, "source": src or "tumu",
            "totals": sums, "by_platform": by_ch, "daily": daily, "products": prods,
            "built_at": st.get("built_at")}


@router.get("/reconciliation")
async def ledger_reconciliation(current_user: dict = Depends(require_permission("reports.sales"))):
    return (await db.report_reconciliation.find_one({"id": "last"}, {"_id": 0})) or {"cells": []}


@router.post("/run")
async def ledger_run(full: bool = False, current_user: dict = Depends(require_permission("reports.sales"))):
    return await run_ledger_and_reconcile(full=full, manual=True)
