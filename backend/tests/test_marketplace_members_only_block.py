import asyncio
from mongomock_motor import AsyncMongoMockClient


def _setup(monkeypatch, end=None):
    import routes.deps as deps
    import routes.products as rp
    import marketplace_block as mb
    db = AsyncMongoMockClient()["mb"]
    monkeypatch.setattr(deps, "db", db)
    monkeypatch.setattr(rp, "db", db)
    rp.invalidate_members_only_cache()
    mb._STATE.update({"keys": frozenset(), "ts": 0.0})

    async def seed():
        await db.categories.insert_many([
            {"id": "ws", "name": "Winter Shape", "members_only": True, "members_only_end": end},
            {"id": "ceket", "name": "Ceket"},
        ])
        await db.products.insert_many([
            {"id": "p1", "category_id": "ceket", "category_ids": ["ceket", "ws"], "stock_code": "FCWS1",
             "variants": [{"barcode": "BC1", "stock_code": "FCWS1", "size": "S"}]},
            {"id": "p2", "category_id": "ceket", "category_ids": ["ceket"], "stock_code": "FCX2",
             "variants": [{"barcode": "BC2", "stock_code": "FCX2", "size": "S"}]},
        ])
        await mb.refresh_blocked(force=True)
    asyncio.run(seed())
    return mb


def test_blocks_members_only_even_in_other_category(monkeypatch):
    mb = _setup(monkeypatch)
    keep, skipped = mb.filter_items([{"barcode": "BC1", "quantity": 0}, {"barcode": "BC2", "quantity": 3}])
    assert skipped == 1 and keep == [{"barcode": "BC2", "quantity": 3}]
    assert mb.item_is_blocked({"attributes": {"merchantSku": "FCWS1"}})


def test_trendyol_client_sends_nothing_for_blocked(monkeypatch):
    mb = _setup(monkeypatch)
    from trendyol_client import TrendyolClient
    import httpx
    called = []

    class _Boom:
        def __init__(self, *a, **k):
            called.append(1)
    monkeypatch.setattr(httpx, "AsyncClient", _Boom)
    c = TrendyolClient.__new__(TrendyolClient)
    c.base_url, c.supplier_id = "https://x", "1"
    r = asyncio.run(c.update_price_and_inventory([{"barcode": "BC1", "quantity": 0}]))
    assert r["skipped_members_only"] == 1 and not called
    r = asyncio.run(c.archive_products(["BC1"]))
    assert r["skipped_members_only"] == 1 and not called


def test_window_end_lifts_block(monkeypatch):
    mb = _setup(monkeypatch, end="2020-01-01T00:00")
    assert not mb.blocked_keys()
    assert mb.filter_items([{"barcode": "BC1"}])[1] == 0


def test_n11_and_hb_filter(monkeypatch):
    mb = _setup(monkeypatch)
    from n11_client import N11Client
    c = N11Client("k", "s")

    async def boom(*a, **k):
        raise AssertionError("istek gitmemeli")
    monkeypatch.setattr(c, "_request", boom)
    r = asyncio.run(c.price_stock_update([{"stockCode": "FCWS1", "quantity": 1}]))
    assert r["skipped_members_only"] == 1
    from hepsiburada_client import HepsiburadaClient
    h = HepsiburadaClient.__new__(HepsiburadaClient)
    h.merchant_id, h.listing_base = "m", "https://x"
    monkeypatch.setattr(h, "_request", lambda *a, **k: (_ for _ in ()).throw(AssertionError("gitmemeli")), raising=False)
    r = h.update_stocks([{"merchantSku": "FCWS1", "availableStock": 0}])
    assert r["skipped_members_only"] == 1


def test_trendyol_create_sends_members_only_with_zero_stock(monkeypatch):
    mb = _setup(monkeypatch)
    from trendyol_client import TrendyolClient
    import httpx
    sent = {}

    class _Resp:
        def raise_for_status(self):
            return None
        def json(self):
            return {"batchRequestId": "b1"}

    class _C:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, headers=None, json=None):
            sent["items"] = json["items"]
            return _Resp()
    monkeypatch.setattr(httpx, "AsyncClient", _C)
    c = TrendyolClient.__new__(TrendyolClient)
    c.base_url, c.supplier_id = "https://x", "1"
    c._get_headers = lambda: {}
    r = asyncio.run(c.create_products([{"barcode": "BC1", "quantity": 7, "salePrice": 100},
                                       {"barcode": "BC2", "quantity": 3, "salePrice": 100}]))
    assert r["batchRequestId"] == "b1"
    assert sent["items"][0]["quantity"] == 0 and sent["items"][0]["salePrice"] == 100
    assert sent["items"][1]["quantity"] == 3
