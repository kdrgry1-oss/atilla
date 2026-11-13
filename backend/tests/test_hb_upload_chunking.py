"""Hepsiburada stok/fiyat gönderimi: parça sınırı (HB kod 1137) ve toplu senkronda yalnız-değişenler."""
import asyncio

import mongomock_motor
import pytest

from routes import integrations_hepsiburada as hb


class _Client:
    def __init__(self, fail_stock=False):
        self.price_calls, self.stock_calls, self.fail_stock = [], [], fail_stock

    def update_prices(self, items):
        self.price_calls.append(len(items))
        return {"id": f"p{len(self.price_calls)}"}

    def update_stocks(self, items):
        if self.fail_stock:
            from hepsiburada_client import HepsiburadaError
            raise HepsiburadaError("HTTP 429")
        self.stock_calls.append(len(items))
        return {"id": f"s{len(self.stock_calls)}"}


@pytest.fixture
def mdb(monkeypatch):
    db = mongomock_motor.AsyncMongoMockClient()["t"]
    monkeypatch.setattr(hb, "db", db)
    return db


def _items(n, stock=1):
    return [{"merchantSku": str(i), "price": 10.0, "availableStock": stock} for i in range(n)]


def test_small_push_keeps_contract(mdb):
    c = _Client()
    out = asyncio.run(hb._hb_push_stock_price(c, [{"merchantSku": "1", "price": 5.0, "availableStock": 2}]))
    assert out == {"price_upload_id": "p1", "stock_upload_id": "s1", "errors": []}


def test_bulk_sync_sends_one_chunk_per_run_then_only_changes(mdb):
    c = _Client()
    items = _items(9001)
    sent = []
    for _ in range(4):
        out = asyncio.run(hb._hb_push_stock_price(c, items))
        sent.append(out.get("delta", {}).get("sent"))
        assert not out["errors"]
    assert sent == [4000, 4000, 1001, 0]                 # tur başına en çok bir parça; sonra gönderilecek yok
    assert c.price_calls == [4000, 4000, 1001] and max(c.price_calls) <= 5000
    items[5]["availableStock"] = 0                       # tek kalem değişti → yalnız o gider
    out = asyncio.run(hb._hb_push_stock_price(c, items))
    assert out["delta"]["sent"] == 1 and c.stock_calls[-1] == 1


def test_failed_upload_is_retried_next_run(mdb):
    items = _items(1500)
    out = asyncio.run(hb._hb_push_stock_price(_Client(fail_stock=True), items))
    assert out["errors"]
    c = _Client()
    out = asyncio.run(hb._hb_push_stock_price(c, items))  # kabul edilmeyen tur "gönderildi" sayılmadı
    assert out["delta"]["sent"] == 1500 and c.stock_calls == [1500]
