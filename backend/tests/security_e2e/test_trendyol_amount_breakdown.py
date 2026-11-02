"""Tutar Analizi (salt-okunur): Trendyol kapalıyken de iade + fatura satırlarını döndürür, hiçbir
kayda yazmaz; fatura satırları faturadaki dağıtımla (_apply_invoice_discounts) aynı."""
from conftest import kv_run
from routes import integrations_trendyol as T

U = {"id": "u1", "email": "a@x", "role": "super_admin"}


def test_breakdown_returns_invoice_lines_and_claims(kv_db, monkeypatch):
    async def _cfg():
        return {"is_active": False}
    monkeypatch.setattr(T, "get_trendyol_config", _cfg)
    kv_run(kv_db.orders.insert_one({"order_number": "1166", "total": 270.0, "items": [
        {"barcode": "a", "product_name": "A", "quantity": 1, "price": 100.0},
        {"barcode": "b", "product_name": "B", "quantity": 1, "price": 200.0}]}))
    kv_run(kv_db.trendyol_claims.insert_one({"claim_id": "c1", "order_number": "1166", "refund_amount": 90,
                                             "items": [{"claim_item_id": "1", "barcode": "a", "price": 90, "quantity": 1}]}))
    before = kv_run(kv_db.orders.find_one({"order_number": "1166"}, {"_id": 0}))
    r = kv_run(T.trendyol_order_amount_breakdown(order_number="1166", current_user=U))
    assert r["trendyol"]["error"]
    assert r["claims"][0]["refund_amount"] == 90
    lines = {l["barcode"]: l for l in r["invoice"]["lines"]}
    assert lines["a"]["item_price"] == 100 and abs(sum(l["invoice_line_total"] for l in lines.values()) - 270) < 0.01
    assert kv_run(kv_db.orders.find_one({"order_number": "1166"}, {"_id": 0})) == before
