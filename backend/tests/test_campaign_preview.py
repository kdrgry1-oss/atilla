import asyncio
from mongomock_motor import AsyncMongoMockClient


def test_campaign_preview_matches_badge_rules(monkeypatch):
    import routes.coupons as rc
    import routes.products as rp
    db = AsyncMongoMockClient()["cp"]
    monkeypatch.setattr(rc, "db", db)
    monkeypatch.setattr(rp, "db", db)

    async def run():
        await db.products.insert_many([
            {"id": "a", "name": "A", "price": 1000, "category_ids": ["ws"], "created_at": "2"},
            {"id": "b", "name": "B", "price": 1000, "sale_price": 800, "category_ids": ["ws"], "created_at": "1"},
            {"id": "c", "name": "C", "price": 1000, "category_ids": ["x"], "created_at": "3"},
        ])
        r = await rc.preview_campaign({"name": "W", "type": "percentage", "value": 20, "auto_apply": True,
                                       "categories": ["ws"]}, {"email": "a@b.c"})
        assert r["total"] == 2
        by = {i["card"]["id"]: i for i in r["items"]}
        assert by["a"]["applies"] and by["a"]["card"]["campaign_discount_percent"] == 20
        assert not by["b"]["applies"] and "İndirimli" in by["b"]["reason"]
        assert await db.coupons.count_documents({}) == 0   # hiçbir şey yazılmaz
        r = await rc.preview_campaign({"name": "W", "type": "percentage", "value": 20, "auto_apply": False,
                                       "categories": ["ws"]}, {"email": "a@b.c"})
        assert all(i["card"]["campaign_discount_percent"] == 0 for i in r["items"])
        assert any("Otomatik uygula" in n for n in r["notes"])
    asyncio.run(run())


def test_campaign_preview_includes_inactive_and_out_of_stock(monkeypatch):
    import routes.coupons as rc
    import routes.products as rp
    db = AsyncMongoMockClient()["cp2"]
    monkeypatch.setattr(rc, "db", db)
    monkeypatch.setattr(rp, "db", db)

    async def run():
        await db.products.insert_many([
            {"id": "p", "name": "P", "price": 1000, "is_active": False, "category_ids": [3381],
             "variants": [{"size": "S", "stock": 0}]},
            {"id": "d", "name": "D", "price": 1000, "is_deleted": True, "category_ids": ["3381"]},
        ])
        r = await rc.preview_campaign({"name": "W", "type": "percentage", "value": 20, "auto_apply": True,
                                       "categories": ["3381"]}, {"email": "a@b.c"})
        assert r["total"] == 1
        it = r["items"][0]
        assert it["card"]["is_active"] is True and it["card"]["variants"][0]["stock"] == 1
        assert it["state"] == "Pasif · Stok yok" and it["applies"]
        assert (await db.products.find_one({"id": "p"}))["is_active"] is False
    asyncio.run(run())
