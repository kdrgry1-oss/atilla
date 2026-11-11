"""Trendyol 'Ürünleriniz' Excel'i ↔ sistem varyantı eşleştirmesi (barkod + kategori)."""
import asyncio
import io

import mongomock_motor
import pandas as pd

import trendyol_excel_match as tm

COLS = ["Barkod", "Model Kodu", "Ürün Rengi", "Beden", "Boyut/Ebat", "Marka", "Kategori İsmi", "Tedarikçi Stok Kodu",
        "Ürün Adı", "Piyasa Satış Fiyatı (KDV Dahil)", "Trendyolda Satılacak Fiyat (KDV Dahil)", "Ürün Stok Adedi",
        "Durum", "Durum Açıklaması", "Trendyol.com Linki"]


def _xlsx(rows):
    df = pd.DataFrame([{c: r.get(c) for c in COLS} for r in rows], columns=COLS)
    b = io.BytesIO()
    df.to_excel(b, index=False, sheet_name="Ürünler")
    return b.getvalue()


ROWS = [
    {"Barkod": "TY17", "Beden": "17", "Tedarikçi Stok Kodu": "LR1", "Ürün Adı": "Yüzük", "Kategori İsmi": "Gümüş Yüzük",
     "Trendyolda Satılacak Fiyat (KDV Dahil)": "150", "Ürün Stok Adedi": "3", "Model Kodu": "M1"},
    {"Barkod": "TY18", "Beden": "18", "Tedarikçi Stok Kodu": "LR1", "Ürün Adı": "Yüzük", "Kategori İsmi": "Gümüş Yüzük",
     "Trendyolda Satılacak Fiyat (KDV Dahil)": "150", "Ürün Stok Adedi": "0", "Model Kodu": "M1"},
    # aynı kod+beden için ARŞİVLİ çift ilan → seçilmemeli
    {"Barkod": "TY17ARS", "Beden": "17", "Tedarikçi Stok Kodu": "LR1", "Ürün Adı": "Yüzük",
     "Durum": "Ürün arşivlendi", "Ürün Stok Adedi": "50"},
    {"Barkod": "TYK", "Beden": "Tek Ebat", "Tedarikçi Stok Kodu": "inureyR99", "Ürün Adı": "Kolye",
     "Kategori İsmi": "Gümüş Kolye"},
    {"Barkod": "TYN", "Beden": "Standart", "Tedarikçi Stok Kodu": None, "Ürün Adı": "Çizgili Kol Düğmesi",
     "Kategori İsmi": "Kol Düğmesi"},
    {"Barkod": "TYX", "Beden": "Tek Ebat", "Tedarikçi Stok Kodu": "YOK1", "Ürün Adı": "Elle eklenen ürün"},
]


def run(c):
    return asyncio.run(c)


def test_match_barcodes_and_categories():
    db = mongomock_motor.AsyncMongoMockClient()["t"]
    run(db.categories.insert_one({"id": "c1", "name": "Yüzükler"}))
    run(db.categories.insert_one({"id": "c2", "name": "Kolyeler", "trendyol_category_name": "Elle Seçilen"}))
    run(db.products.insert_many([
        {"id": "1", "name": "Gümüş Yüzük", "price": 100, "category_id": "c1",
         "variants": [{"id": "a", "size": "17", "stock_code": "LR1"}, {"id": "b", "size": "18", "stock_code": "LR1"}]},
        {"id": "2", "name": "Kolye", "price": 100, "category_id": "c2",
         "variants": [{"id": "c", "size": None, "stock_code": "R99"}]},
        {"id": "3", "name": "Çizgili Kol Düğmesi", "price": 100,
         "variants": [{"id": "d", "size": None, "stock_code": "CFL1"}]},
        {"id": "4", "name": "Başka", "price": 100,
         "variants": [{"id": "e", "size": None, "stock_code": "Z9", "barcode": "ELLE-GIRILMIS"}]},
    ]))
    r = run(tm.run_match(db, _xlsx(ROWS)))
    st = r["stats"]
    assert st["matched_variants"] == 4 and st["by_kod+beden"] == 3 and st["by_ad+beden"] == 1
    p1 = run(db.products.find_one({"id": "1"}))
    assert {v["size"]: v["barcode"] for v in p1["variants"]} == {"17": "TY17", "18": "TY18"}   # arşivli seçilmedi
    assert p1["variants"][0]["trendyol_listing"]["matched_by"] == "kod+beden"
    assert p1["trendyol_category_name"] == "Gümüş Yüzük" and p1["trendyol_model_code"] == "M1"
    assert run(db.products.find_one({"id": "2"}))["variants"][0]["barcode"] == "TYK"           # inure öneki
    assert run(db.products.find_one({"id": "3"}))["variants"][0]["barcode"] == "TYN"           # ad ile
    assert run(db.products.find_one({"id": "4"}))["variants"][0]["barcode"] == "ELLE-GIRILMIS"  # dokunulmadı
    assert run(db.categories.find_one({"id": "c1"}))["trendyol_category_name"] == "Gümüş Yüzük"
    assert run(db.categories.find_one({"id": "c2"}))["trendyol_category_name"] == "Elle Seçilen"  # korunur
    assert r["suggested_markup_pct"] == 50.0
    um = {d["barcode"] for d in run(db.trendyol_excel_unmatched.find({}).to_list(None))}
    assert um == {"TY17ARS", "TYX"}
    # ikinci yükleme aynı sonucu verir, barkod çiftlenmez
    r2 = run(tm.run_match(db, _xlsx(ROWS)))
    assert r2["stats"]["matched_variants"] == 4
    bars = [v["barcode"] for p in run(db.products.find({}).to_list(None)) for v in p["variants"]]
    assert len(bars) == len(set(bars))
