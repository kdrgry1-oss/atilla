import asyncio, base64
from mongomock_motor import AsyncMongoMockClient


def test_audit_repairs_broken_from_db(monkeypatch):
    import scheduler, security_fetch
    from services import r2_storage as r2
    db = AsyncMongoMockClient()["ia"]
    store = {}

    class R:
        def __init__(self, ok):
            self.status_code = 200 if ok else 404
            self.headers = {"content-type": "image/webp" if ok else "text/html"}
            self.content = b""
        @property
        def content_type(self):
            return self.headers["content-type"]

    async def fake_fetch(url, **k):
        return R(url.startswith("https://cdn.ok/") or url in store.get("_pub", set()))
    monkeypatch.setattr(security_fetch, "safe_fetch", fake_fetch)
    monkeypatch.setattr(r2, "is_enabled", lambda: True)
    monkeypatch.setattr(r2, "put_object", lambda k, d, ct: store.__setitem__(k, d) or f"https://cdn.ok/{k}")
    monkeypatch.setattr(r2, "get_object", lambda k: (store.get(k), "image/webp"))
    monkeypatch.setattr(r2, "public_url", lambda k: f"https://cdn.ok/{k}")

    async def fake_log(*a, **k):
        return None
    import routes.integrations_common as ic
    monkeypatch.setattr(ic, "log_integration_event", fake_log)

    async def run():
        await db.files.insert_one({"storage_path": "b.webp", "data_b64": base64.b64encode(b"xyz").decode(),
                                   "content_type": "image/webp"})
        await db.products.insert_one({"id": "p", "urun_karti_id": "3043", "name": "Rosso",
                                      "images": ["https://cdn.ok/uploads/a.webp", "https://api.x/api/upload/files/b.webp",
                                                 "https://cdn.bad/uploads/c.webp"]})
        res = await scheduler._audit_repair_product_images(db)
        p = await db.products.find_one({"id": "p"})
        assert res["checked"] == 3 and res["broken"] == 2 and res["repaired"] == 1 and len(res["lost"]) == 1
        assert p["images"][1] == "https://cdn.ok/uploads/b.webp" and p["images"][2] == "https://cdn.bad/uploads/c.webp"
    asyncio.run(run())
