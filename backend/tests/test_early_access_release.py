import asyncio
from mongomock_motor import AsyncMongoMockClient


def test_release_pushes_once_after_end(monkeypatch):
    import routes.deps as deps
    import routes.products as rp
    import scheduler
    import routes.integrations as ri
    import routes.integrations_common as ic
    db = AsyncMongoMockClient()["ea"]
    monkeypatch.setattr(deps, "db", db)
    monkeypatch.setattr(rp, "db", db)
    pushed = []

    async def fake_sync(prods, *a, **k):
        pushed.append(sorted(p["id"] for p in prods))

    async def fake_cfg():
        return {"is_active": True}

    async def fake_log(*a, **k):
        return None
    monkeypatch.setattr(ri, "_sync_inventory_to_trendyol", fake_sync)
    monkeypatch.setattr(ri, "get_trendyol_config", fake_cfg)
    monkeypatch.setattr(ic, "log_integration_event", fake_log)
    import routes.category_mapping as cm

    async def no_hb():
        return None, "yok"
    monkeypatch.setattr(cm, "_get_hb_client", no_hb)

    async def run():
        await db.categories.insert_many([
            {"id": "ws", "name": "Winter Shape", "members_only": True, "members_only_end": "2026-11-04T23:59"},
            {"id": "ws2", "name": "Gelecek", "members_only": True, "members_only_end": "2099-01-01T00:00"},
        ])
        await db.products.insert_many([
            {"id": "a", "is_active": True, "category_ids": ["ws"]},
            {"id": "b", "is_active": True, "category_ids": ["ws2"]},
        ])
        from datetime import datetime, timezone
        monkeypatch.setattr(scheduler, "datetime", type("D", (), {"now": staticmethod(lambda tz=None: datetime(2026, 11, 5, tzinfo=timezone.utc))}))
        await scheduler._release_early_access_to_marketplaces()
        await scheduler._release_early_access_to_marketplaces()   # ikinci kez göndermez
        assert pushed == [["a"]]
        c = await db.categories.find_one({"id": "ws"})
        assert c["members_only_released_for"] == "2026-11-04T23:59"
    asyncio.run(run())
