"""Davranış analizi: olay toplama doğrulaması, arama kaydı, panel raporu ve ısı haritası."""
import pytest

from pay_harness import pay_staff
from routes import deps

pytestmark = pytest.mark.asyncio


async def test_collect_search_and_reports(pay_http, pay_env):
    await deps.db.products.insert_one({"id": "p1", "name": "Pamuklu Etek", "slug": "pamuklu-etek"})
    sid = "sabcdef0123456789"
    evs = [
        {"t": "pv", "path": "/pamuklu-etek", "pt": "product", "pid": "p1", "w": 1440, "h": 3200},
        {"t": "click", "path": "/pamuklu-etek", "pt": "product", "pid": "p1", "w": 1440, "x": 0.5, "y": 800,
         "sel": "button[add-to-cart]", "txt": "Sepete Ekle"},
        {"t": "time", "path": "/pamuklu-etek", "pt": "product", "pid": "p1", "w": 1440, "ms": 42000},
        {"t": "scroll", "path": "/pamuklu-etek", "pt": "product", "pid": "p1", "w": 1440, "depth": 65},
        {"t": "atc", "path": "/pamuklu-etek", "pt": "product", "pid": "p1", "w": 1440},
        {"t": "pv", "path": "/kategori/etek", "pt": "category", "cat": "Etek", "w": 390, "h": 5000},
        {"t": "pv", "path": "/admin/siparisler", "pt": "other", "w": 1440},                    # panel → atılır
        {"t": "click", "path": "/x", "x": 5, "y": 1, "w": 1440},                              # geçersiz x → atılır
        {"t": "evil", "path": "/x"},                                                          # bilinmeyen tür
    ]
    r = await pay_http.post("/api/analytics/collect", json={"sid": sid, "events": evs})
    assert r.status_code == 200 and r.json()["n"] == 6
    assert (await pay_http.post("/api/analytics/collect", json={"sid": "x", "events": evs})).json()["ok"] is False
    for q, n, pid in (("Etek ", 12, None), ("etek", 12, "p1"), ("kimono xyz", 0, None), ("e", 3, None)):
        await pay_http.post("/api/analytics/search", json={"q": q, "n": n, "src": "live", "pid": pid})
    assert await deps.db.site_search_log.count_documents({}) == 3                        # 1 harfli atlandı
    doc = await deps.db.site_events.find_one({"t": "click"})
    assert "ip" not in doc and "user_id" not in doc

    _, hdr = await pay_staff(["reports.sales"])
    _r = await pay_http.get("/api/admin/behavior/summary", params={"days": 7}, headers=hdr)
    assert _r.status_code == 200, _r.text
    s = _r.json()
    assert s["totals"]["pageviews"] == 2 and s["totals"]["sessions"] == 1
    p = s["products"][0]
    assert p["pid"] == "p1" and p["name"] == "Pamuklu Etek" and p["views"] == 1 and p["atc"] == 1 and p["avg_sec"] == 42.0
    assert s["categories"][0]["cat"] == "Etek"
    terms = {t["term"]: t for t in s["searches"]}
    assert terms["etek"]["count"] == 2 and terms["etek"]["clicked"] == 1
    assert [z["term"] for z in s["zero_result_searches"]] == ["kimono xyz"]

    h = (await pay_http.get("/api/admin/behavior/heatmap", params={"path": "/pamuklu-etek", "device": "desktop"},
                            headers=hdr)).json()
    assert h["views"] == 1 and h["clicks"] == [{"x": 0.5, "y": 800}] and h["doc_height"] == 3200
    assert h["top_elements"][0]["txt"] == "Sepete Ekle"
    assert h["scroll_reach"][6]["pct"] == 100.0 and h["scroll_reach"][7]["pct"] == 0.0   # %60'a ulaştı, %70'e değil

    _, nohdr = await pay_staff(["orders.view"])
    assert (await pay_http.get("/api/admin/behavior/summary", headers=nohdr)).status_code == 403
