"""Ödenip sonra İPTAL edilen siparişte kullanılan ilk-sipariş / kişi-başı kupon tekrar kullanılabilir;
iptal edilmemiş (ödenmiş) siparişteki kullanım hakkı tüketmeye devam eder."""
from conftest import kv_run


def _seed(kv_db, status):
    kv_run(kv_db.users.insert_one({"id": "u-c1", "email": "musteri@example.com"}))
    kv_run(kv_db.coupons.insert_one({"id": "cp1", "code": "HOSGELDIN10", "type": "percent", "value": 10,
                                     "is_active": True, "first_order_only": True, "usage_limit_per_user": 1}))
    kv_run(kv_db.orders.insert_one({"id": "o-c1", "user_id": "u-c1", "status": status, "payment_status": "paid",
                                    "coupon_code": "HOSGELDIN10", "email": "musteri@example.com",
                                    "created_at": "2026-10-30T10:00:00+00:00"}))
    kv_run(kv_db.coupon_redemptions.insert_one({"coupon_id": "cp1", "order_id": "o-c1", "user_id": "u-c1"}))


def test_cancelled_paid_order_frees_coupon(kv_db):
    from routes.coupons import _evaluate_single
    _seed(kv_db, "cancelled")
    c = kv_run(kv_db.coupons.find_one({"id": "cp1"}, {"_id": 0}))
    r = kv_run(_evaluate_single(c, 1000.0, [], user_id="u-c1", email="musteri@example.com"))
    assert r["valid"] is True, r


def test_active_paid_order_still_consumes_coupon(kv_db):
    from routes.coupons import _evaluate_single
    _seed(kv_db, "confirmed")
    c = kv_run(kv_db.coupons.find_one({"id": "cp1"}, {"_id": 0}))
    r = kv_run(_evaluate_single(c, 1000.0, [], user_id="u-c1", email="musteri@example.com"))
    assert r["valid"] is False
