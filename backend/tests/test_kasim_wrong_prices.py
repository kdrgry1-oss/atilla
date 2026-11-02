import asyncio
from mongomock_motor import AsyncMongoMockClient


def test_kasim_fix_only_exact_old_price(monkeypatch):
    import routes.deps as deps
    import server
    db = AsyncMongoMockClient()["kf"]
    monkeypatch.setattr(deps, "db", db)

    async def run():
        await db.products.insert_many([
            {"id": "a", "name": "Fular Acı Kahve", "price": 1390, "variants": [{"barcode": "8684483520082", "price": 690}]},
            {"id": "b", "name": "Fular Kırmızı", "price": 1200, "variants": [{"barcode": "8684483519741", "price": 690}]},
        ])
        await server.fix_kasim_wrong_prices_once()
        assert (await db.products.find_one({"id": "a"}))["price"] == 690
        assert (await db.products.find_one({"id": "b"}))["price"] == 1200   # kullanıcı başka değer girmiş → dokunma
    asyncio.run(run())
