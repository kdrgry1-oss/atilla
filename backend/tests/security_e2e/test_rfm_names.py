"""RFM: ad, sipariş adresindeki ad/soyad alanlarından gelir; e-postasız siparişler tek bir boş
'müşteri'de toplanıp listeye (VIP olarak) girmez."""
from datetime import datetime, timezone

from conftest import kv_ip, kv_run


def _order(oid, email, total, **sa):
    return {"id": oid, "order_number": oid, "status": "delivered", "payment_status": "paid",
            "total": total, "created_at": datetime.now(timezone.utc).isoformat(),
            "customer_email": email, "shipping_address": sa}


def test_rfm_names_from_first_last_and_no_empty_email(kv_client, kv_users, kv_db):
    kv_run(kv_db.orders.insert_many([
        _order("O1", "ayse@example.com", 500, first_name="Ayşe", last_name="Yılmaz"),
        _order("O2", "", 9000, first_name="Pazar", last_name="Yeri"),
        _order("O3", None, 9000),
        _order("O4", "ali@example.com", 300, full_name="Ali Veli"),
    ]))
    st = kv_users.staff(perms=["customers.view"])
    r = kv_client.get("/api/analytics-extra/rfm?lookback_days=365", headers={**kv_users.hdr(st), **kv_ip()})
    assert r.status_code == 200, r.text
    items = {i["email"]: i for i in r.json()["items"]}
    assert set(items) == {"ayse@example.com", "ali@example.com"}
    assert items["ayse@example.com"]["name"] == "Ayşe Yılmaz"
    assert items["ali@example.com"]["name"] == "Ali Veli"
