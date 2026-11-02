"""Gider pusulası no → kaynak (site/TY/HB/Amazon) + sipariş no; iptal arşivi de bulunur."""
import pytest

from pay_harness import pay_staff
from routes import deps

pytestmark = pytest.mark.asyncio


async def test_gp_locate_sources(pay_http, pay_env):
    db = deps.db
    await db.trendyol_claims.insert_many([
        {"claim_id": "ty1", "order_number": "11700000001", "platform": "trendyol"},
        {"claim_id": "hb1", "order_number": "4000000001", "platform": "hepsiburada"},
    ])
    await db.gider_pusulasi.insert_many([
        {"claim_id": "ty1", "order_number": "11700000001", "display_number": "087401"},
        {"claim_id": "hb1", "order_number": "4000000001", "display_number": "087402"},
        {"return_id": "r1", "order_number": "W12000", "display_number": "087403"},
    ])
    await db.gider_pusulasi_iptal.insert_one({"claim_id": "ty1", "order_number": "11700000009",
                                              "display_number": "087499"})
    _, hdr = await pay_staff(["returns.view"])
    async def loc(no):
        r = await pay_http.get("/api/integrations/trendyol/gp-locate", params={"no": no}, headers=hdr)
        assert r.status_code == 200, r.text
        return r.json()["results"]
    assert [(x["source"], x["order_number"]) for x in await loc("087401")] == [("trendyol", "11700000001")]
    assert [(x["source"], x["order_number"]) for x in await loc("#087402")] == [("hepsiburada", "4000000001")]
    assert [(x["source"], x["order_number"]) for x in await loc("087403")] == [("facette", "W12000")]
    c = await loc("087499")
    assert c[0]["cancelled"] is True and c[0]["order_number"] == "11700000009"
    assert await loc("999999") == []
    _, nohdr = await pay_staff(["dashboard.view"])
    r = await pay_http.get("/api/integrations/trendyol/gp-locate", params={"no": "087401"}, headers=nohdr)
    assert r.status_code == 403
