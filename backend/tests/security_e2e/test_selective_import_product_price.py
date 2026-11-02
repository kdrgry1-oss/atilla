"""Seçmeli Excel fiyat güncellemesi: varyant fiyatları + ÜRÜN düzeyi fiyat da güncellenir
(vitrin kartı ürün fiyatını okur; yalnız varyant güncellenince eski fiyat görünüyordu)."""
import io
import pandas as pd
from fastapi import UploadFile
from conftest import kv_run
from routes import products as P


def _xlsx(rows):
    b = io.BytesIO()
    pd.DataFrame(rows).to_excel(b, index=False)
    b.seek(0)
    return UploadFile(b, filename="f.xlsx")


def test_price_update_syncs_product_level(kv_db):
    kv_run(kv_db.products.insert_many([
        {"id": "p1", "name": "Etek", "price": 1690.0, "sale_price": 1690.0, "variants": [
            {"barcode": "b1", "size": "S", "price": 1690.0, "sale_price": 1690.0},
            {"barcode": "b2", "size": "M", "price": 1690.0, "sale_price": 1690.0}]},
        {"id": "p2", "name": "Diğer", "price": 500.0, "sale_price": 450.0, "variants": [
            {"barcode": "x1", "price": 500.0, "sale_price": 450.0}]},
    ]))
    f = _xlsx([{"Barkod": "b1", "Piyasa Fiyatı": 1790, "Satış Fiyatı": 1790},
               {"Barkod": "b2", "Piyasa Fiyatı": 1790, "Satış Fiyatı": 1790}])
    r = kv_run(P._selective_import(f, ["Piyasa Fiyatı", "Satış Fiyatı"], set(), {"email": "a@x"}, None))
    assert r["stats"]["updated_rows"] == 2
    p1 = kv_run(kv_db.products.find_one({"id": "p1"}, {"_id": 0}))
    assert p1["price"] == 1790 and p1["sale_price"] == 1790
    assert all(v["price"] == 1790 and v["sale_price"] == 1790 for v in p1["variants"])
    p2 = kv_run(kv_db.products.find_one({"id": "p2"}, {"_id": 0}))
    assert p2["price"] == 500 and p2["sale_price"] == 450      # dosyada olmayan ürüne dokunulmaz
