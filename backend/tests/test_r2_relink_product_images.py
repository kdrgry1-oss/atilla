import asyncio
from mongomock_motor import AsyncMongoMockClient


def test_relink_product_images_to_r2():
    import scheduler
    db = AsyncMongoMockClient()["rl"]

    async def run():
        await db.files.insert_many([
            {"storage_path": "a.webp", "r2_url": "https://cdn.x/uploads/a.webp"},
            {"storage_path": "b.webp"},  # henüz taşınmadı → dokunulmaz
        ])
        await db.products.insert_one({"id": "p", "images": [
            "https://cdn.x/uploads/kapak.webp", "/api/upload/files/a.webp",
            "https://api.x/api/upload/files/b.webp",
            {"url": "/api/upload/files/a.webp", "is_size_table": True}]})
        n = await scheduler._relink_product_images_to_r2(db)
        p = await db.products.find_one({"id": "p"})
        assert n == 1
        assert p["images"] == ["https://cdn.x/uploads/kapak.webp", "https://cdn.x/uploads/a.webp",
                               "https://api.x/api/upload/files/b.webp",
                               {"url": "https://cdn.x/uploads/a.webp", "is_size_table": True}]
    asyncio.run(run())
