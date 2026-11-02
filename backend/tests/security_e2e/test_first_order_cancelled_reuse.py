"""Hoş geldin (ilk sipariş) kodu: önceki siparişi İPTAL edilen müşteri kodu yeniden kullanabilir
(W12556). Teslim edilip iade edilen sipariş ise önceki sipariş sayılmaya devam eder."""
from conftest import kv_run
from routes.coupons import _evaluate_single

FIRST = {"id": "c-hos", "code": "HOSGELDIN10", "is_active": True, "type": "percent", "value": 10,
         "first_order_only": True}


def _user(db):
    kv_run(db.users.insert_one({"id": "u1", "email": "musteri@example.com", "email_verified": True}))


def test_cancelled_paid_order_does_not_block(kv_db):
    _user(kv_db)
    kv_run(kv_db.orders.insert_one({"id": "o1", "user_id": "u1", "payment_status": "paid",
                                    "status": "cancelled", "customer_email": "musteri@example.com"}))
    kv_run(kv_db.orders.insert_one({"id": "o2", "user_id": "u1", "payment_status": "refunded",
                                    "status": "cancel_refunded"}))
    r = kv_run(_evaluate_single(FIRST, 1000.0, [], "u1", "musteri@example.com"))
    assert r["valid"] is True and r["discount"] == 100


def test_delivered_or_returned_order_still_blocks(kv_db):
    _user(kv_db)
    kv_run(kv_db.orders.insert_one({"id": "o3", "user_id": "u1", "payment_status": "paid", "status": "returned"}))
    r = kv_run(_evaluate_single(FIRST, 1000.0, [], "u1", "musteri@example.com"))
    assert r["valid"] is False and "ilk" in r["reason"].lower()
