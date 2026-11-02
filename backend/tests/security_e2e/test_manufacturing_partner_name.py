"""İmalat: imalatçı değişince listedeki ad (partner_name) da güncellenir; eski kayıtlarda bayat
ad seçili imalatçıdan (supplier_id) düzeltilir (Liva Triko Şal: listede Erol, detayda Oğuzhan)."""
from conftest import kv_run
from routes import manufacturing as M

ADMIN = {"id": "u1", "email": "a@x", "role": "super_admin"}


def _seed(db):
    kv_run(db.manufacturing_suppliers.insert_many([{"id": "s1", "name": "Erol"}, {"id": "s2", "name": "Oğuzhan"}]))
    kv_run(db.manufacturing.insert_one({"id": "m1", "code": "IMLT-1", "order_no": "000059",
                                        "product_name": "Liva Triko Şal", "supplier_id": "s1",
                                        "partner_name": "Erol", "current_stage": "kesim",
                                        "created_at": "2026-10-01"}))


def test_update_supplier_updates_list_name(kv_db):
    _seed(kv_db)
    kv_run(M.update_manufacturing("m1", {"supplier_id": "s2"}, current_user=ADMIN))
    rec = kv_run(kv_db.manufacturing.find_one({"id": "m1"}, {"_id": 0}))
    assert rec["supplier_id"] == "s2" and rec["partner_name"] == "Oğuzhan"


def test_stale_name_healed_in_list_and_detail(kv_db):
    _seed(kv_db)
    kv_run(kv_db.manufacturing.update_one({"id": "m1"}, {"$set": {"supplier_id": "s2"}}))  # eski hata durumu
    items = kv_run(M._sync_partner_names(kv_run(kv_db.manufacturing.find({}, {"_id": 0}).to_list(10))))
    assert items[0]["partner_name"] == "Oğuzhan"
    assert kv_run(kv_db.manufacturing.find_one({"id": "m1"}))["partner_name"] == "Oğuzhan"


def test_unknown_or_empty_supplier_rejected(kv_db):
    import pytest
    from fastapi import HTTPException
    _seed(kv_db)
    for sid in ("", "yok"):
        with pytest.raises(HTTPException):
            kv_run(M.update_manufacturing("m1", {"supplier_id": sid}, current_user=ADMIN))
    assert kv_run(kv_db.manufacturing.find_one({"id": "m1"}))["partner_name"] == "Erol"
