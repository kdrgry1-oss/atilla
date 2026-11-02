"""W12767: ödemesi tamamlanmamış (3DS yarıda) kart siparişinde müşteri hesabından iptal isteyince
durum 'cancel_requested' olur. Bu sipariş de ödenmemiş kart süpürgesine girmeli (değişmez 4):
'Ödeme Alınamadı' kapanır + stok bir kez iade. ÖDENMİŞ kart iptal talebine dokunulmaz."""
import pytest

from pay_harness import now_iso, order_payload, pay_find, pay_seed_product, pay_set, pay_stock

pytestmark = pytest.mark.asyncio


async def test_unpaid_card_cancel_request_is_swept_and_restocked(pay_http, pay_env):
    import scheduler
    await pay_seed_product("p1", stock=5)
    r = await pay_http.post("/api/orders", json=order_payload("credit_card"))
    assert r.status_code == 200, r.text
    oid = r.json()["order_id"]
    o = await pay_find("orders", {"id": oid})
    assert o["status"] == "awaiting_payment" and o["payment_status"] != "paid"
    assert await pay_stock() == 4
    await pay_set("orders", {"id": oid}, {"status": "cancel_requested", "cancel_source": "customer",
                                          "created_at": now_iso(hours=-4)})
    await scheduler.auto_cancel_unpaid_card_orders()
    await scheduler.auto_cancel_unpaid_card_orders()
    cur = await pay_find("orders", {"id": oid})
    assert cur["status"] == "payment_failed" and cur["payment_status"] == "expired"
    assert await pay_stock() == 5


async def test_paid_card_cancel_request_untouched(pay_http, pay_env):
    import scheduler
    await pay_seed_product("p1", stock=5)
    r = await pay_http.post("/api/orders", json=order_payload("credit_card"))
    oid = r.json()["order_id"]
    await pay_set("orders", {"id": oid}, {"status": "cancel_requested", "payment_status": "paid",
                                          "created_at": now_iso(hours=-4)})
    await scheduler.auto_cancel_unpaid_card_orders()
    cur = await pay_find("orders", {"id": oid})
    assert cur["status"] == "cancel_requested" and cur["payment_status"] == "paid"
    assert await pay_stock() == 4
