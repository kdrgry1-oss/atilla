"""Ticimax URUNID kimliğiyle katalog onarımı."""
import asyncio

import mongomock_motor

import ticimax_identity as ti


def run(c):
    return asyncio.run(c)


def test_apply_identity_and_linked_sync():
    db = mongomock_motor.AsyncMongoMockClient()["t"]
    run(db.products.insert_many([
        {"id": "x1", "source": "ticimax_xml", "urun_karti_id": "C1", "variants": [
            {"id": "a", "urun_id": "101", "size": "8", "barcode": "TY102", "stock": 5},     # karışmış barkod
            {"id": "b", "urun_id": "102", "size": "9", "barcode": None, "stock": 2},
            {"id": "c", "urun_id": "103", "size": "10", "barcode": "TY999", "stock": 1}]},   # Ticimax dışı barkod: korunur
        {"id": "e1", "source": "trendyol_excel", "price": 50, "variants": [
            {"id": "d", "barcode": "TY101", "stock": 0, "price": 50, "size": None},          # XML varyantının mükerreri
            {"id": "e", "barcode": "TY201", "stock": 0, "price": 50, "size": None}]},        # XML'de olmayan kart → bağlanır
        {"id": "e2", "source": "trendyol_excel", "price": 10, "variants": [
            {"id": "f", "barcode": "TY101X", "stock": 7, "price": 10}]},                     # Ticimax'ta yok → stok 0
        {"id": "e3", "source": "trendyol_excel", "price": 10, "variants": [
            {"id": "g", "barcode": "TY102", "stock": 0, "price": 10}]},                      # tümü mükerrer → silinir
    ]))
    recs = [{"u": "101", "c": "C1", "k": "K1", "b": "K1-8", "s": 5, "p": 100, "a": 1, "t": "TY101"},
            {"u": "102", "c": "C1", "k": "K1", "b": "K1-9", "s": 2, "p": 100, "a": 1, "t": "TY102"},
            {"u": "103", "c": "C1", "k": "K1", "b": "K1-10", "s": 1, "p": 100, "a": 1, "t": None},
            {"u": "201", "c": "C2", "k": "K2", "b": "K2", "s": 40, "p": 300.5, "a": 1, "t": "TY201"}]
    d = run(ti.apply_identity(db, recs, dry_run=True))
    assert d["xml_barcode_fixed"] == 1 and d["xml_barcode_added"] == 1 and d["excel_linked"] == 1
    assert run(db.products.count_documents({})) == 4
    for _ in range(2):   # tekrar çalıştırılabilir
        r = run(ti.apply_identity(db, recs, dry_run=False))
    x1 = run(db.products.find_one({"id": "x1"}))
    assert [v["barcode"] for v in x1["variants"]] == ["TY101", "TY102", "TY999"]
    e1 = run(db.products.find_one({"id": "e1"}))
    assert len(e1["variants"]) == 1 and e1["variants"][0]["urun_id"] == "201"
    assert e1["variants"][0]["stock"] == 40 and e1["price"] == 300.5 and e1["stock"] == 40
    assert e1["variants"][0]["ticimax_barcode"] == "K2" and e1["ticimax_linked"] and e1["ticimax_card_id"] == "C2"
    assert run(db.products.find_one({"id": "e2"}))["variants"][0]["stock"] == 0
    assert run(db.products.find_one({"id": "e3"})) is None
    assert r["xml_barcode_ok"] == 2

    # XML senkronu bağlı varyantı günceller ve o kart için yeni ürün açtırmaz
    parsed = [{"card_id": "C2", "variants": [{"urun_id": "201", "stock": 33, "price": 310.0}]},
              {"card_id": "C9", "variants": [{"urun_id": "900", "stock": 1, "price": 5.0}]}]
    lk = run(ti.sync_linked_variants(db, parsed, lambda price, v: price))
    assert lk["linked_updated"] == 1 and lk["linked_cards"] == {"C2"}
    e1 = run(db.products.find_one({"id": "e1"}))
    assert e1["variants"][0]["stock"] == 33 and e1["variants"][0]["price"] == 310.0 and e1["stock"] == 33
