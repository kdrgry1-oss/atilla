"""Pazaryeri (Trendyol) siparişinde kalem fiyatı ZATEN indirimli net; iade/pusula indirimi
ikinci kez düşmemeli (11660952469: şort 1227,71 / ceket 2920,75 → pusula 4148,46 = fatura).
Site siparişinde (price = liste) donmuş kalem indirimi düşülmeye devam eder."""
from conftest import kv_run
from routes import orders as O

U = {"id": "u1", "email": "a@x", "role": "super_admin"}


def _ty_items():
    # map_trendyol_order biçimi: unit_price=liste, price=net, discount_amount=liste-net (birim)
    return [
        {"name": "Elbise", "product_id": "8684483528859", "quantity": 1, "unit_price": 1862.50, "price": 1844.04, "discount_amount": 18.46},
        {"name": "Şort", "product_id": "8684483523182", "quantity": 1, "unit_price": 1240.00, "price": 1227.71, "discount_amount": 12.29},
        {"name": "Ceket", "product_id": "8684483523151", "quantity": 1, "unit_price": 2950.00, "price": 2920.75, "discount_amount": 29.25},
    ]


def test_return_item_disc_helper():
    ty = _ty_items()
    assert O._return_item_disc(ty[1]) == 0.0
    site = {"unit_price": 1000.0, "price": 1000.0, "discount_amount": 100.0, "quantity": 1}
    assert O._return_item_disc(site) == 100.0


def test_partial_gp_matches_invoice_for_trendyol_order(kv_db):
    kv_run(kv_db.orders.insert_one({"id": "o1", "order_number": "11660952469", "platform": "trendyol",
                                    "total": 5992.50, "subtotal": 5992.50, "discount": 0, "shipping_cost": 0,
                                    "items": [{**i, "barcode": i["product_id"], "product_name": i["name"]} for i in _ty_items()]}))
    kv_run(kv_db.customer_returns.insert_one({"id": "r1", "order_id": "o1", "order_number": "11660952469",
                                              "items": _ty_items(), "status": "approved"}))
    res = kv_run(O.site_return_gider_pusulasi("r1", {"item_indexes": [1, 2]}, preview=True, current_user=U))
    gp = res.get("gider_pusulasi") or res
    nets = [round(g["net_price"] * g["quantity"], 2) for g in gp["items"] if g.get("name") in ("Şort", "Ceket")]
    assert nets == [1227.71, 2920.75]
    assert abs(gp["totals"]["net"] - 4148.46) < 0.01
    assert abs(O._selected_return_net(_ty_items(), [1, 2], {"items": _ty_items(), "subtotal": 5992.50, "total": 5992.50}) - 4148.46) < 0.01
