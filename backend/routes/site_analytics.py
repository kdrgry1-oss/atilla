"""SİTE DAVRANIŞ ANALİZİ (Hotjar/Clarity benzeri, birinci taraf) — Kadir isteği 2026-10-16.

Toplanan (yalnız vitrinde, KİŞİSEL VERİ YOK):
  • /api/analytics/collect  — analitik çerez İZNİ veren ziyaretçinin oturum olayları:
      pv (sayfa görüntüleme: yol, sayfa tipi, ürün/kategori), click (göreli x, sayfa y, öğe etiketi),
      scroll (en derin %), time (aktif süre ms), atc (sepete ekleme). Oturum kimliği rastgele ve
      sekme ömürlüdür (sessionStorage) — kullanıcı/e-posta/IP SAKLANMAZ. 180 gün sonra silinir.
  • /api/analytics/search   — aranan kelime + sonuç sayısı (kimlik/oturum YOK, salt istatistik). 365 gün.
Panel: /api/admin/behavior/* (reports.sales izni) — özet, ürün/kategori/arama/sayfa raporları, ısı haritası.
"""
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request

from .deps import db, limiter, logger, require_permission

router = APIRouter(prefix="/analytics", tags=["site-analytics"])
admin_router = APIRouter(prefix="/admin/behavior", tags=["admin-behavior"])

_TYPES = {"pv", "click", "scroll", "time", "atc"}
_PTYPES = {"home", "product", "category", "search", "cart", "checkout", "account", "other"}
_MAX_EVENTS = 60
_SID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_EVENT_TTL_DAYS = 180
_SEARCH_TTL_DAYS = 365
_idx_done = False


async def _ensure_indexes():
    global _idx_done
    if _idx_done:
        return
    try:
        await db.site_events.create_index("ts", expireAfterSeconds=_EVENT_TTL_DAYS * 86400)
        await db.site_events.create_index([("t", 1), ("day", 1)])
        await db.site_events.create_index([("t", 1), ("path", 1), ("dev", 1), ("day", 1)])
        await db.site_search_log.create_index("ts", expireAfterSeconds=_SEARCH_TTL_DAYS * 86400)
        await db.site_search_log.create_index([("day", 1), ("term", 1)])
        _idx_done = True
    except Exception as e:
        logger.warning(f"[analytics] indeks kurulamadı: {e}")


def _s(v, n: int) -> str:
    return str(v if v is not None else "")[:n]


def _num(v, lo: float, hi: float) -> Optional[float]:
    try:
        f = float(v)
    except Exception:
        return None
    if f != f or f < lo or f > hi:
        return None
    return f


def _dev(w: Optional[float]) -> str:
    if not w:
        return "desktop"
    return "mobile" if w < 768 else ("tablet" if w < 1024 else "desktop")


def _path(p) -> str:
    p = _s(p, 300).split("?")[0].split("#")[0].strip() or "/"
    if not p.startswith("/") or p.startswith("/admin"):
        return ""
    return p


def _tr_day(now: datetime) -> str:
    return (now + timedelta(hours=3)).date().isoformat()


@router.post("/collect")
@(limiter.limit("120/minute") if limiter else (lambda f: f))
async def collect(request: Request, payload: dict):
    """Vitrinden toplu davranış olayları (istemci yalnız analitik izni varsa gönderir)."""
    if not isinstance(payload, dict):
        return {"ok": False}
    sid = _s(payload.get("sid"), 64)
    if not _SID_RE.match(sid):
        return {"ok": False}
    evs = payload.get("events")
    if not isinstance(evs, list):
        return {"ok": False}
    await _ensure_indexes()
    now = datetime.now(timezone.utc)
    day = _tr_day(now)
    docs = []
    for e in evs[:_MAX_EVENTS]:
        if not isinstance(e, dict):
            continue
        t = e.get("t")
        path = _path(e.get("path"))
        if t not in _TYPES or not path:
            continue
        w = _num(e.get("w"), 200, 8000)
        d = {"t": t, "sid": sid, "path": path, "day": day, "ts": now, "dev": _dev(w)}
        pt = e.get("pt")
        d["pt"] = pt if pt in _PTYPES else "other"
        if e.get("pid"):
            d["pid"] = _s(e.get("pid"), 64)
        if e.get("cat"):
            d["cat"] = _s(e.get("cat"), 120)
        if t == "pv":
            h = _num(e.get("h"), 0, 200000)
            if h:
                d["h"] = int(h)
        elif t == "click":
            x, y = _num(e.get("x"), 0, 1), _num(e.get("y"), 0, 200000)
            if x is None or y is None:
                continue
            d.update({"x": round(x, 4), "y": int(y), "w": int(w or 0),
                      "sel": _s(e.get("sel"), 80), "txt": _s(e.get("txt"), 40)})
        elif t == "scroll":
            dep = _num(e.get("depth"), 0, 100)
            if dep is None:
                continue
            d["depth"] = int(dep)
        elif t == "time":
            ms = _num(e.get("ms"), 0, 3 * 3600 * 1000)
            if not ms:
                continue
            d["ms"] = int(ms)
        docs.append(d)
    if docs:
        try:
            await db.site_events.insert_many(docs, ordered=False)
        except Exception as ex:
            logger.warning(f"[analytics] olay yazılamadı: {ex}")
    return {"ok": True, "n": len(docs)}


def norm_term(q) -> str:
    q = re.sub(r"\s+", " ", _s(q, 80)).strip().lower()
    return q[:60]


@router.post("/search")
@(limiter.limit("60/minute") if limiter else (lambda f: f))
async def log_search(request: Request, payload: dict):
    """Aranan kelime (kimliksiz istatistik). src: submit|live|page; pid: sonuçtan tıklanan ürün."""
    if not isinstance(payload, dict):
        return {"ok": False}
    term = norm_term(payload.get("q"))
    if len(term) < 2:
        return {"ok": False}
    await _ensure_indexes()
    now = datetime.now(timezone.utc)
    doc = {"term": term, "day": _tr_day(now), "ts": now,
           "src": payload.get("src") if payload.get("src") in ("submit", "live", "page") else "live",
           "n": int(_num(payload.get("n"), 0, 100000) or 0)}
    if payload.get("pid"):
        doc["pid"] = _s(payload.get("pid"), 64)
    await db.site_search_log.insert_one(doc)
    return {"ok": True}


async def popular_terms(days: int = 30, limit: int = 8) -> list:
    """Gerçek popüler aramalar (sonuç veren kelimeler) — vitrindeki 'Popüler Aramalar' için."""
    since = (datetime.now(timezone.utc) + timedelta(hours=3) - timedelta(days=days)).date().isoformat()
    rows = await db.site_search_log.aggregate([
        {"$match": {"day": {"$gte": since}, "n": {"$gt": 0}}},
        {"$group": {"_id": "$term", "count": {"$sum": 1}}},
        {"$match": {"count": {"$gte": 3}}},
        {"$sort": {"count": -1}}, {"$limit": limit}]).to_list(limit)
    return [{"term": r["_id"], "count": r["count"]} for r in rows]


# ───────────────────────────────────── PANEL ─────────────────────────────────────
def _since(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=3) - timedelta(days=max(1, days) - 1)).date().isoformat()


async def _product_names(pids: list) -> dict:
    out = {}
    if pids:
        async for p in db.products.find({"id": {"$in": pids}},
                                        {"_id": 0, "id": 1, "name": 1, "slug": 1, "images": 1}):
            img = (p.get("images") or [None])[0]
            out[p["id"]] = {"name": p.get("name"), "slug": p.get("slug"),
                            "image": img if isinstance(img, str) else (img or {}).get("url") if isinstance(img, dict) else None}
    return out


@admin_router.get("/summary")
async def behavior_summary(days: int = Query(7, ge=1, le=180),
                           current_user: dict = Depends(require_permission("reports.sales"))):
    since = _since(days)
    m = {"day": {"$gte": since}}
    pv = await db.site_events.count_documents({**m, "t": "pv"})
    sessions = len(await db.site_events.distinct("sid", {**m, "t": "pv"}))
    tm = await db.site_events.aggregate([{"$match": {**m, "t": "time"}},
                                         {"$group": {"_id": None, "ms": {"$sum": "$ms"}}}]).to_list(1)
    total_ms = int(tm[0]["ms"]) if tm else 0
    clicks = await db.site_events.count_documents({**m, "t": "click"})
    dev = await db.site_events.aggregate([{"$match": {**m, "t": "pv"}},
                                          {"$group": {"_id": "$dev", "n": {"$sum": 1}}}]).to_list(10)

    # Ürünler: görüntüleme, tekil oturum, ortalama süre, sepete ekleme
    prod = await db.site_events.aggregate([
        {"$match": {**m, "pid": {"$exists": True}, "t": {"$in": ["pv", "time", "atc", "click"]}}},
        {"$group": {"_id": "$pid",
                    "views": {"$sum": {"$cond": [{"$and": [{"$eq": ["$t", "pv"]}, {"$eq": ["$pt", "product"]}]}, 1, 0]}},
                    "list_clicks": {"$sum": {"$cond": [{"$and": [{"$eq": ["$t", "click"]}, {"$ne": ["$pt", "product"]}]}, 1, 0]}},
                    "atc": {"$sum": {"$cond": [{"$eq": ["$t", "atc"]}, 1, 0]}},
                    "ms": {"$sum": {"$cond": [{"$and": [{"$eq": ["$t", "time"]}, {"$eq": ["$pt", "product"]}]}, "$ms", 0]}}}},
        {"$sort": {"views": -1}}, {"$limit": 200}]).to_list(200)
    # Tekil oturum (ürün sayfası görüntüleyen)
    _sess = {}
    async for r in db.site_events.aggregate([
            {"$match": {**m, "t": "pv", "pt": "product", "pid": {"$in": [p["_id"] for p in prod]}}},
            {"$group": {"_id": {"p": "$pid", "s": "$sid"}}},
            {"$group": {"_id": "$_id.p", "n": {"$sum": 1}}}]):
        _sess[r["_id"]] = r["n"]
    for p in prod:
        p["sessions"] = _sess.get(p["_id"], 0)
    names = await _product_names([p["_id"] for p in prod])
    # Satın alma (aynı dönem, iptal olmayan site siparişleri) → görüntülemeden satışa dönüşüm
    _start = datetime.fromisoformat(since).replace(tzinfo=timezone.utc) - timedelta(hours=3)
    buys = {}
    async for r in db.orders.aggregate([
            {"$match": {"created_at": {"$gte": _start.isoformat()}, "platform": {"$in": [None, "", "site", "web"]},
                        "status": {"$nin": ["cancelled", "cancel_refunded", "awaiting_payment", "payment_failed", "pending"]}}},
            {"$unwind": "$items"},
            {"$group": {"_id": "$items.product_id", "qty": {"$sum": {"$ifNull": ["$items.quantity", 1]}}}}]):
        if r.get("_id"):
            buys[str(r["_id"])] = int(r["qty"])
    products = []
    for p in prod:
        nm = names.get(p["_id"]) or {}
        products.append({"pid": p["_id"], "name": nm.get("name") or p["_id"], "slug": nm.get("slug"),
                         "image": nm.get("image"), "views": p["views"], "sessions": p["sessions"],
                         "list_clicks": p["list_clicks"], "atc": p["atc"],
                         "avg_sec": round(p["ms"] / 1000 / p["views"], 1) if p["views"] else 0,
                         "bought": buys.get(p["_id"], 0)})

    cats = await db.site_events.aggregate([
        {"$match": {**m, "t": {"$in": ["pv", "time"]}, "pt": "category", "cat": {"$exists": True}}},
        {"$group": {"_id": "$cat", "views": {"$sum": {"$cond": [{"$eq": ["$t", "pv"]}, 1, 0]}},
                    "ms": {"$sum": {"$cond": [{"$eq": ["$t", "time"]}, "$ms", 0]}},
                    "sids": {"$addToSet": "$sid"}}},
        {"$project": {"views": 1, "ms": 1, "sessions": {"$size": "$sids"}}},
        {"$sort": {"views": -1}}, {"$limit": 100}]).to_list(100)
    categories = [{"cat": c["_id"], "views": c["views"], "sessions": c["sessions"],
                   "avg_sec": round(c["ms"] / 1000 / c["views"], 1) if c["views"] else 0} for c in cats]

    pages = await db.site_events.aggregate([
        {"$match": {**m, "t": {"$in": ["pv", "time", "scroll"]}}},
        {"$group": {"_id": "$pt", "views": {"$sum": {"$cond": [{"$eq": ["$t", "pv"]}, 1, 0]}},
                    "ms": {"$sum": {"$cond": [{"$eq": ["$t", "time"]}, "$ms", 0]}},
                    "depth_sum": {"$sum": {"$cond": [{"$eq": ["$t", "scroll"]}, "$depth", 0]}},
                    "depth_n": {"$sum": {"$cond": [{"$eq": ["$t", "scroll"]}, 1, 0]}}}},
        {"$sort": {"ms": -1}}]).to_list(20)
    page_types = [{"pt": p["_id"], "views": p["views"], "total_min": round(p["ms"] / 60000, 1),
                   "avg_sec": round(p["ms"] / 1000 / p["views"], 1) if p["views"] else 0,
                   "avg_scroll": round(p["depth_sum"] / p["depth_n"]) if p["depth_n"] else None} for p in pages]

    terms = await db.site_search_log.aggregate([
        {"$match": {"day": {"$gte": since}}},
        {"$group": {"_id": "$term", "count": {"$sum": 1},
                    "zero": {"$sum": {"$cond": [{"$eq": ["$n", 0]}, 1, 0]}},
                    "avg_n": {"$avg": "$n"},
                    "clicked": {"$sum": {"$cond": [{"$ifNull": ["$pid", False]}, 1, 0]}}}},
        {"$sort": {"count": -1}}, {"$limit": 300}]).to_list(300)
    searches = [{"term": t["_id"], "count": t["count"], "zero": t["zero"],
                 "avg_results": round(t["avg_n"] or 0, 1), "clicked": t["clicked"]} for t in terms]
    return {"days": days, "since": since,
            "totals": {"pageviews": pv, "sessions": sessions, "clicks": clicks,
                       "avg_session_sec": round(total_ms / 1000 / sessions, 1) if sessions else 0,
                       "searches": sum(s["count"] for s in searches),
                       "devices": {d["_id"]: d["n"] for d in dev}},
            "products": products, "categories": categories, "page_types": page_types,
            "searches": searches, "zero_result_searches": [s for s in searches if s["zero"] == s["count"]][:100]}


@admin_router.get("/pages")
async def behavior_pages(days: int = Query(30, ge=1, le=180),
                         current_user: dict = Depends(require_permission("reports.sales"))):
    """Isı haritası için en çok görüntülenen sayfalar."""
    rows = await db.site_events.aggregate([
        {"$match": {"day": {"$gte": _since(days)}, "t": "pv"}},
        {"$group": {"_id": "$path", "views": {"$sum": 1}, "pt": {"$first": "$pt"}}},
        {"$sort": {"views": -1}}, {"$limit": 100}]).to_list(100)
    return {"pages": [{"path": r["_id"], "views": r["views"], "pt": r.get("pt")} for r in rows]}


@admin_router.get("/heatmap")
async def behavior_heatmap(path: str, device: str = Query("desktop"), days: int = Query(30, ge=1, le=180),
                           current_user: dict = Depends(require_permission("reports.sales"))):
    p = _path(path)
    dev = device if device in ("desktop", "mobile", "tablet") else "desktop"
    m = {"day": {"$gte": _since(days)}, "path": p, "dev": dev}
    pts = await db.site_events.aggregate([
        {"$match": {**m, "t": "click"}}, {"$sample": {"size": 20000}},
        {"$project": {"_id": 0, "x": 1, "y": 1}}]).to_list(20000)
    views = await db.site_events.count_documents({**m, "t": "pv"})
    hs = [d.get("h") for d in await db.site_events.find({**m, "t": "pv", "h": {"$gt": 0}}, {"_id": 0, "h": 1}).limit(500).to_list(500)]
    hs = sorted(h for h in hs if h)
    depth = await db.site_events.aggregate([
        {"$match": {**m, "t": "scroll"}},
        {"$bucket": {"groupBy": "$depth", "boundaries": [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 101],
                     "default": "x", "output": {"n": {"$sum": 1}}}}]).to_list(20)
    n_scroll = sum(d["n"] for d in depth if d["_id"] != "x")
    # "%X'e ulaşan oturum oranı" (kümülatif)
    reach, acc = [], n_scroll
    for b in [0, 10, 20, 30, 40, 50, 60, 70, 80, 90]:
        reach.append({"depth": b, "pct": round(100 * acc / n_scroll, 1) if n_scroll else 0})
        acc -= next((d["n"] for d in depth if d["_id"] == b), 0)
    els = await db.site_events.aggregate([
        {"$match": {**m, "t": "click"}},
        {"$group": {"_id": {"sel": "$sel", "txt": "$txt"}, "n": {"$sum": 1}}},
        {"$sort": {"n": -1}}, {"$limit": 30}]).to_list(30)
    return {"path": p, "device": dev, "views": views, "clicks": pts,
            "doc_height": hs[len(hs) // 2] if hs else None, "scroll_reach": reach,
            "top_elements": [{"sel": e["_id"].get("sel"), "txt": e["_id"].get("txt"), "n": e["n"]} for e in els]}
