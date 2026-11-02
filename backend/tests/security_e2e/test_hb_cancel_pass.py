"""HB'de iptal edilen sipariş panelde de iptal olur (tam iptal), kısmi iptal sipariş iptal ETMEZ."""
import pytest

import scheduler
from routes import deps
import routes.integrations as I
import routes.orders as O

pytestmark = pytest.mark.asyncio


class _Cli:
    def __init__(self, rows): self.rows = rows
    def get_cancelled_orders(self, off, lim, b, e): return self.rows if off == 0 and b else []


async def test_hb_cancel_pass(pay_env, monkeypatch):
    db = deps.db
    await db.orders.insert_many([
        {"id": "h1", "order_number": "HB1", "platform": "hepsiburada", "status": "confirmed",
         "items": [{"quantity": 1}]},
        {"id": "h2", "order_number": "HB2", "platform": "hepsiburada", "status": "confirmed",
         "items": [{"quantity": 1}, {"quantity": 1}]},
        {"id": "h3", "order_number": "HB3", "platform": "hepsiburada", "status": "cancelled",
         "items": [{"quantity": 1}]},
    ])
    rows = [{"n": "HB1", "q": 1}, {"n": "HB2", "q": 1}, {"n": "HB3", "q": 1}]
    monkeypatch.setattr(I, "_hb_orders_from_response", lambda resp: resp)
    monkeypatch.setattr(I, "map_hepsiburada_order", lambda g: {"order_number": g["n"], "items": [{"quantity": g["q"]}]})
    restocked = []

    async def _rs(order, mt):
        restocked.append((order["id"], mt)); return []
    monkeypatch.setattr(O, "_restock_order_once", _rs)
    res = await scheduler._hb_cancel_pass(_Cli(rows), days=1)
    assert res["cancelled"] == ["HB1"]
    o1 = await db.orders.find_one({"id": "h1"})
    assert o1["status"] == "cancelled" and o1["cancel_source"] == "hepsiburada_cron"
    assert (await db.orders.find_one({"id": "h2"}))["status"] == "confirmed"   # kısmi iptal
    assert restocked == [("h1", "order_cancelled")]
    res2 = await scheduler._hb_cancel_pass(_Cli(rows), days=1)           # idempotent
    assert res2["cancelled"] == [] and len(restocked) == 1
