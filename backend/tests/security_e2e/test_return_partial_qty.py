"""İade onayında 2+ adetli kalemin yalnız bir kısmı (adet seçimi) onaylanabilir: iade kaydı
satırı [seçilen, kalan] olarak bölünür, onaylı kalem adedi yalnız seçilen kadardır."""
import pytest
from conftest import kv_ip, kv_run
from routes.orders import _split_return_items_for_qty

ITEMS = [
    {"name": "Gomlek Beyaz", "size": "M", "color": "", "quantity": 1, "price": 1270, "product_id": "B1"},
    {"name": "Gomlek Antrasit", "size": "M", "color": "", "quantity": 2, "price": 1270, "product_id": "A2",
     "discount_amount": 100},
]


def test_split_and_merge_back():
    out = _split_return_items_for_qty(ITEMS, [{"barcode": "A2", "qty": 1}])
    assert [(i["product_id"], i["quantity"]) for i in out] == [("B1", 1), ("A2", 1), ("A2", 1)]
    assert [i.get("discount_amount") for i in out[1:]] == [50, 50]
    # Düzenlemede tam adet seçilirse satırlar birleşir; değişmeyen seçim None döner.
    back = _split_return_items_for_qty(out, [{"barcode": "A2", "qty": 2}])
    assert [(i["product_id"], i["quantity"]) for i in back] == [("B1", 1), ("A2", 2)]
    assert _split_return_items_for_qty(ITEMS, [{"barcode": "A2", "qty": 2}]) is None
    with pytest.raises(ValueError):
        _split_return_items_for_qty(ITEMS, [{"barcode": "A2", "qty": 3}])


def test_approve_one_of_two_units(kv_client, kv_users, kv_db):
    kv_run(kv_db.orders.insert_one({
        "id": "o-q", "order_number": "W1", "status": "return_requested", "subtotal": 3810, "total": 3909,
        "shipping_cost": 99, "payment_status": "paid",
        "items": [{"product_name": "Gomlek Beyaz", "size": "M", "quantity": 1, "price": 1270, "barcode": "B1"},
                  {"product_name": "Gomlek Antrasit", "size": "M", "quantity": 2, "price": 1270, "barcode": "A2"}]}))
    kv_run(kv_db.customer_returns.insert_one({
        "id": "r-q", "order_id": "o-q", "order_number": "W1", "status": "created",
        "items": [{k: v for k, v in it.items() if k != "discount_amount"} for it in ITEMS]}))
    sa = kv_users.staff(super_admin=True)
    r = kv_client.post("/api/orders/returns/r-q/approve", json={
        "fault": "store", "returned_net": 1270, "item_indexes": [1],
        "selected_items": [{"barcode": "A2", "name": "Gomlek Antrasit", "size": "M", "qty": 1}]},
        headers={**kv_users.hdr(sa), **kv_ip()})
    assert r.status_code == 200, r.text
    rec = kv_run(kv_db.customer_returns.find_one({"id": "r-q"}))
    assert [(i["product_id"], i["quantity"]) for i in rec["items"]] == [("B1", 1), ("A2", 1), ("A2", 1)]
    assert rec["approved_item_indexes"] == [1]
    assert rec["approved_items"][0]["quantity"] == 1 and rec["approved_items"][0]["barcode"] == "A2"
    assert abs(r.json()["refund_amount"] - 1270) < 1


def test_gider_pusulasi_preview_one_of_two_units(kv_client, kv_users, kv_db):
    kv_run(kv_db.orders.insert_one({
        "id": "o-g", "order_number": "W2", "status": "return_requested", "subtotal": 3810, "total": 3909,
        "shipping_cost": 99, "payment_status": "paid",
        "items": [{"product_name": "Gomlek Beyaz", "size": "M", "quantity": 1, "price": 1270, "barcode": "B1"},
                  {"product_name": "Gomlek Antrasit", "size": "M", "quantity": 2, "price": 1270, "barcode": "A2"}]}))
    kv_run(kv_db.customer_returns.insert_one({
        "id": "r-g", "order_id": "o-g", "order_number": "W2", "status": "created",
        "items": [{k: v for k, v in it.items() if k != "discount_amount"} for it in ITEMS]}))
    sa = kv_users.staff(super_admin=True)
    r = kv_client.post("/api/orders/returns/r-g/gider-pusulasi?preview=true", json={
        "item_indexes": [1], "selected_items": [{"barcode": "A2", "name": "Gomlek Antrasit", "size": "M", "qty": 1}]},
        headers={**kv_users.hdr(sa), **kv_ip()})
    assert r.status_code == 200, r.text
    gp = r.json()["gider_pusulasi"]
    assert sum(int(i.get("quantity") or i.get("qty") or 0) for i in gp["items"]) == 1, gp["items"]
    assert abs(gp["totals"]["gross"] - 1270) < 1, gp["totals"]
    # Önizleme iade kaydını kalıcı bölmez.
    assert len(kv_run(kv_db.customer_returns.find_one({"id": "r-g"}))["items"]) == 2


def test_gider_pusulasi_spreads_order_discount_evenly_then_adds_vade(kv_client, kv_users, kv_db):
    """İade kuralı: sipariş indirimi TÜM kalemlere aynı oranla, vade farkı indirimli tutara sonra.
    4.100 TL, %7 indirim (287 TL), 4 eşit kalem (1.025); 1 kalem iade → 1.025 × 0,93 = 953,25 + vade."""
    o_items = [{"product_name": f"Urun {k}", "size": "M", "quantity": 1, "price": 1025, "barcode": f"E{k}",
                "discount_amount": (287 if k == 0 else 0)} for k in range(4)]   # kampanya tek kaleme yazılmış olsa da
    kv_run(kv_db.orders.insert_one({"id": "o-e", "order_number": "W4", "status": "return_approved",
                                    "subtotal": 4100, "discount": 287, "shipping_cost": 0, "total": 3813,
                                    "payment_status": "paid", "items": o_items}))
    kv_run(kv_db.customer_returns.insert_one({"id": "r-e", "order_id": "o-e", "order_number": "W4", "status": "approved",
        "items": [{"name": i["product_name"], "size": "M", "color": "", "quantity": 1, "price": 1025, "unit_price": 1025,
                   "product_id": i["barcode"], "discount_amount": i["discount_amount"]} for i in o_items]}))
    sa = kv_users.staff(super_admin=True)
    r = kv_client.post("/api/orders/returns/r-e/gider-pusulasi?preview=true", json={
        "item_indexes": [3], "selected_items": [{"barcode": "E3", "name": "Urun 3", "size": "M"}]},
        headers={**kv_users.hdr(sa), **kv_ip()})
    assert r.status_code == 200, r.text
    assert abs(r.json()["gider_pusulasi"]["totals"]["net"] - 953.25) < 0.5, r.json()["gider_pusulasi"]["totals"]
