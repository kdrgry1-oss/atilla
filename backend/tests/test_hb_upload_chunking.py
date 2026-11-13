"""Hepsiburada stok/fiyat gönderimi: tek istekte en çok 5000 kalem (HB kod 1137) → parçalı gönderim."""
import asyncio

from routes import integrations_hepsiburada as hb


class _Client:
    def __init__(self):
        self.price_calls, self.stock_calls = [], []

    def update_prices(self, items):
        self.price_calls.append(len(items))
        return {"id": f"p{len(self.price_calls)}"}

    def update_stocks(self, items):
        self.stock_calls.append(len(items))
        return {"id": f"s{len(self.stock_calls)}"}


def test_large_push_is_chunked():
    c = _Client()
    items = [{"merchantSku": str(i), "price": 10.0, "availableStock": 1} for i in range(9001)]
    out = asyncio.run(hb._hb_push_stock_price(c, items))
    assert c.price_calls == [4000, 4000, 1001] and c.stock_calls == [4000, 4000, 1001]
    assert max(c.price_calls) <= 5000
    assert out["price_upload_id"] == "p1" and out["stock_upload_ids"] == ["s1", "s2", "s3"] and not out["errors"]


def test_small_push_keeps_contract():
    c = _Client()
    out = asyncio.run(hb._hb_push_stock_price(c, [{"merchantSku": "1", "price": 5.0, "availableStock": 2}]))
    assert out == {"price_upload_id": "p1", "stock_upload_id": "s1", "errors": []}
