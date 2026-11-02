import asyncio
from mongomock_motor import AsyncMongoMockClient


def test_winter_shape_from_3042(monkeypatch):
    import routes.deps as deps
    import routes.products as rp
    import server
    db = AsyncMongoMockClient()["t"]
    monkeypatch.setattr(deps, "db", db)
    monkeypatch.setattr(rp, "db", db)

    async def run():
        await db.categories.insert_many([
            {"id": "root", "name": "Kadın", "slug": "kadin"},
            {"id": "ws", "name": "Winter  Shape", "slug": "winter-shape", "parent_id": "root"},
        ])
        await db.products.insert_many([
            {"id": "a", "urun_karti_id": "3041", "category_ids": ["x"], "category_id": "x"},
            {"id": "b", "urun_karti_id": "3042", "category_ids": ["x"], "category_id": "x", "is_new": False},
            {"id": "c", "urun_karti_id": "3048", "category_ids": ["ws", "root"]},
            {"id": "d", "urun_karti_id": "abc"},
        ])
        await server.add_winter_shape_from_card_3042_once()
        a = await db.products.find_one({"id": "a"})
        b = await db.products.find_one({"id": "b"})
        assert "ws" not in a["category_ids"]
        assert set(b["category_ids"]) == {"x", "ws", "root"}
        assert b["category_id"] == "x" and b["is_new"] is False
        flag = await db.settings.find_one({"id": "migrations.winter_shape_3042_v1"})
        assert flag["done"] and flag["added"] == 1 and flag["already"] == 1
    asyncio.run(run())


def test_winter_shape_missing_then_named_variant(monkeypatch):
    import routes.deps as deps
    import routes.products as rp
    import server
    db = AsyncMongoMockClient()["t2"]
    monkeypatch.setattr(deps, "db", db)
    monkeypatch.setattr(rp, "db", db)

    async def run():
        await db.products.insert_one({"id": "b", "urun_karti_id": "3043", "category_ids": []})
        assert await server.add_winter_shape_from_card_3042_once() is False
        assert (await db.products.find_one({"id": "b"}))["category_ids"] == []
        await db.categories.insert_one({"id": "ws", "name": "WINTER SHAPE 2026", "slug": "ws26", "is_active": False})
        assert await server.add_winter_shape_from_card_3042_once() is True
        assert "ws" in (await db.products.find_one({"id": "b"}))["category_ids"]
    asyncio.run(run())
