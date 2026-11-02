"""Amazon doğrulama: varyantsız (tek beden) ürün de taranır; bulunamayan kodların nedeni döner."""
from conftest import kv_run


def test_variantless_product_found_and_inactive_reported(kv_db, monkeypatch):
    import routes.amazon_spapi as amz

    async def _tok():
        return ("t", "e", "MP1")

    async def _pt(p):
        return ("COAT", {}, [])

    async def _sch(pt):
        return {"required": []}

    async def _mk():
        return 0
    monkeypatch.setattr(amz, "get_valid_access_token", _tok)
    monkeypatch.setattr(amz, "_resolve_amazon_pt_and_defaults", _pt)
    monkeypatch.setattr(amz, "_amazon_product_type_schema", _sch)
    monkeypatch.setattr(amz, "_amazon_markup", _mk)
    kv_run(kv_db.products.insert_many([
        {"id": "a1", "name": "Tek Beden Şal", "is_active": True, "price": 500, "stock": 3,
         "stock_code": "FCFW359864", "barcode": "8680000000101", "variants": []},
        {"id": "a2", "name": "Pasif Ceket", "is_active": False, "price": 900,
         "stock_code": "FCFW678044", "variants": [{"barcode": "8680000000102", "size": "M"}]},
    ]))
    out = kv_run(amz.validate_products_for_amazon(
        {"stock_codes": ["FCFW359864", "FCFW678044", "FCFW975817"],
         "barcodes": ["FCFW359864", "FCFW678044", "FCFW975817"]}, {}))
    # Kodla seçilen PASİF ürün de aktarılır (kullanıcı kararı); bulunamayan kodun nedeni döner.
    assert out["total"] == 2 and out["valid_count"] == 2, out
    reasons = {u["code"]: u["reason"] for u in out["unmatched_codes"]}
    assert "yok" in reasons["FCFW975817"] and set(reasons) == {"FCFW975817"}


def test_inactive_copy_with_active_twin_is_dropped(kv_db):
    import routes.amazon_spapi as amz
    kv_run(kv_db.products.insert_many([
        {"id": "b1", "name": "Ceket", "is_active": True, "variants": [{"barcode": "8680000000201"}]},
        {"id": "b2", "name": "Ceket (Kopya)", "is_active": False, "variants": [{"barcode": "8680000000202"}]},
        {"id": "b3", "name": "Ceket eski", "is_active": False, "variants": [{"barcode": "8680000000201"}]},
        {"id": "b4", "name": "Yeni Pasif", "is_active": False, "variants": [{"barcode": "8680000000204"}]},
    ]))
    prods = kv_run(kv_db.products.find({}, {"_id": 0}).to_list(None))
    kept = {p["id"] for p in kv_run(amz._amz_drop_inactive_copies(prods))}
    assert kept == {"b1", "b4"}


def test_fabric_type_from_attributes_or_name():
    from routes.amazon_helpers import amazon_fabric_for
    assert amazon_fabric_for({"name": "Liva Triko Şal"}) == "Triko"
    assert amazon_fabric_for({"name": "Ceket", "technical_details": {"Materyal": "%100 Pamuk"}}) == "%100 Pamuk"
    assert amazon_fabric_for({"name": "Ceket", "description": "Kumaş: %80 Viskon %20 Elastan"}).startswith("%80 Viskon")
    assert amazon_fabric_for({"name": "Kotlin Elbise"}) == ""     # 'kot' kelime içinde → eşleşmez
