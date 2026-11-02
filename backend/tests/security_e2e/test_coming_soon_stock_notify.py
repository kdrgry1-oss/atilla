"""'Yakında Stokta': panel bayrağı vitrine açılır; 'Stoğa Gelince Haber Ver' kaydı (bedensiz,
ürün düzeyi) ürün stoğa girince 'stock_back_in_stock' e-postasıyla bir kez bildirilir."""
from conftest import kv_ip, kv_run


def test_coming_soon_visible_and_restock_mail_once(kv_client, kv_db, monkeypatch):
    kv_run(kv_db.products.insert_one({
        "id": "p-cs", "slug": "yakinda-urun", "name": "Yakında Ürün", "price": 100, "is_active": True,
        "coming_soon": True, "stock": 0,
        "variants": [{"id": "1001", "size": "S", "stock": 0}, {"id": "1002", "size": "M", "stock": 0}]}))
    r = kv_client.get("/api/products/p-cs", headers=kv_ip())
    assert r.status_code == 200 and r.json().get("coming_soon") is True

    r = kv_client.post("/api/stock-notify", json={"product_id": "p-cs", "size": "", "email": "musteri@example.com"},
                       headers=kv_ip())
    assert r.status_code == 200, r.text

    sent = []

    async def fake_send(db, key, **kw):
        sent.append((key, kw.get("to_email"), (kw.get("variables") or {}).get("product_name")))
        return {"success": True}

    import notification_service
    monkeypatch.setattr(notification_service, "send_notification", fake_send)
    import scheduler
    kv_run(scheduler._notify_back_in_stock())          # hâlâ stoksuz → mail yok
    assert sent == []
    kv_run(kv_db.products.update_one({"id": "p-cs"}, {"$set": {"variants.1.stock": 3, "stock": 3}}))
    kv_run(scheduler._notify_back_in_stock())
    kv_run(scheduler._notify_back_in_stock())          # ikinci tur tekrar göndermez
    assert sent == [("stock_back_in_stock", "musteri@example.com", "Yakında Ürün")]
