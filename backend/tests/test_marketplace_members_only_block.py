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
    mb._STATE.update({"keys": frozenset(), "members": frozenset(), "nonxml": frozenset(), "ts": 0.0})

    async def seed():
        await db.categories.insert_many([
            {"id": "ws", "name": "Winter Shape", "members_only": True, "members_only_end": end},
            {"id": "ceket", "name": "Ceket"},
        ])
        await db.products.insert_many([
            {"id": "p1", "source": "ticimax_xml", "category_id": "ceket", "category_ids": ["ceket", "ws"], "stock_code": "FCWS1",
             "variants": [{"barcode": "BC1", "stock_code": "FCWS1", "size": "S"}]},
            {"id": "p2", "source": "ticimax_xml", "category_id": "ceket", "category_ids": ["ceket"], "stock_code": "FCX2",
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



# ── XML dışı ürünler (İşletme Kuralı marketplace.only_xml_products, varsayılan AÇIK) ──────────────────
def _setup_xml(monkeypatch, rule_on=True):
    import routes.deps as deps
    import routes.products as rp
    import marketplace_block as mb
    db = AsyncMongoMockClient()["mbx"]
    monkeypatch.setattr(deps, "db", db)
    monkeypatch.setattr(rp, "db", db)
    rp.invalidate_members_only_cache()
    mb._STATE.update({"keys": frozenset(), "members": frozenset(), "nonxml": frozenset(), "ts": 0.0})

    async def seed():
        import business_rules as br
        if not rule_on:
            await br.save_rules(db, {"marketplace.only_xml_products": False})
        await db.products.insert_many([
            {"id": "x1", "source": "ticimax_xml", "stock_code": "LR1",
             "variants": [{"barcode": "TYX1", "stock_code": "LR1", "size": "17"}]},
            {"id": "m1", "source": "manual", "stock_code": "EL1",
             "variants": [{"barcode": "MAN1", "stock_code": "EL1"}]},
            # elle eklenmiş KOPYA: XML ürünüyle aynı stok kodu → XML ürünü engellenmemeli
            {"id": "m2", "stock_code": "LR1", "variants": [{"barcode": "MAN2", "stock_code": "LR1"}]},
        ])
        await mb.refresh_blocked(force=True)
    asyncio.run(seed())
    return mb


def test_nonxml_products_never_sent(monkeypatch):
    mb = _setup_xml(monkeypatch)
    keep, skipped = mb.filter_items([{"barcode": "TYX1", "quantity": 1}, {"barcode": "MAN1", "quantity": 5},
                                     {"barcode": "MAN2", "quantity": 2}])
    assert skipped == 2 and keep == [{"barcode": "TYX1", "quantity": 1}]
    assert not mb.item_is_blocked({"stockCode": "LR1", "quantity": 1})        # ortak kod XML ürününe ait
    # ürün oluşturma/güncelleme: XML dışı kalem 0 stokla bile GÖNDERİLMEZ (çıkarılır)
    out, zeroed = mb.zero_stock_items([{"barcode": "TYX1", "quantity": 3}, {"barcode": "MAN1", "quantity": 9}])
    assert out == [{"barcode": "TYX1", "quantity": 3}] and zeroed == 0


def test_trendyol_price_inventory_skips_nonxml(monkeypatch):
    mb = _setup_xml(monkeypatch)
    from trendyol_client import TrendyolClient
    import httpx
    called = []

    class _Boom:
        def __init__(self, *a, **k):
            called.append(1)
    monkeypatch.setattr(httpx, "AsyncClient", _Boom)
    c = TrendyolClient.__new__(TrendyolClient)
    c.base_url, c.supplier_id = "https://x", "1"
    r = asyncio.run(c.update_price_and_inventory([{"barcode": "MAN1", "quantity": 4, "salePrice": 10}]))
    assert r["skipped_members_only"] == 1 and not called


def test_rule_off_allows_nonxml(monkeypatch):
    mb = _setup_xml(monkeypatch, rule_on=False)
    assert mb.filter_items([{"barcode": "MAN1"}])[1] == 0


def test_fail_closed_before_first_refresh(monkeypatch):
    import marketplace_block as mb
    mb._STATE.update({"keys": frozenset(), "members": frozenset(), "nonxml": frozenset(), "ts": 0.0})
    assert mb.filter_items([{"barcode": "ANY"}], "hb.stocks") == ([], 1)
    assert mb.item_is_blocked({"merchantSku": "ANY"})
