"""Trendyol 'tekrarlı ürün oluşturma' reddi YENİ (Trendyol'da olmayan) ürünü güncellemeye
sokmaz: barkod varlığı kontrol edilir, son gerçek oluşturma hatası gösterilir, kuyruğa alınır."""
from conftest import kv_run
from routes.integrations_trendyol import _trendyol_missing_barcodes, _explain_recurring_create


class _FakeClient:
    def __init__(self, existing, fail=False):
        self.existing, self.fail = set(existing), fail

    async def get_filtered_products(self, barcode=None, size=50, **kw):
        if self.fail:
            raise RuntimeError("api down")
        return {"content": [{"barcode": barcode}] if barcode in self.existing else [],
                "totalElements": 1 if barcode in self.existing else 0}


def test_missing_barcodes_detected_and_api_error_keeps_old_behaviour():
    items = [{"barcode": "111"}, {"barcode": "222"}]
    assert kv_run(_trendyol_missing_barcodes(_FakeClient({"111"}), items)) == {"222"}
    assert kv_run(_trendyol_missing_barcodes(_FakeClient(set(), fail=True), items)) == set()


def test_explain_shows_last_real_create_error_and_queues(kv_db):
    kv_run(kv_db.trendyol_sync_logs.insert_many([
        {"started_at": "2026-10-21T10:00:00", "failed_items": [
            {"barcode": "222", "reasons": [{"message": "Renk özelliği zorunludur"}]}]},
        {"started_at": "2026-10-21T10:05:00", "failed_items": [
            {"barcode": "222", "reasons": [{"message": "157840 tedarikçi id si ve 222 barkod ile ürün bulunamadı."}]}]},
    ]))
    out = kv_run(_explain_recurring_create(kv_db, {"222"}, "Tekrarlı ürün oluşturma isteği atılamaz"))
    assert len(out) == 1 and "Trendyol'da YOK" in out[0] and "Renk özelliği zorunludur" in out[0]
    assert kv_run(kv_db.trendyol_stuck_queue.find_one({"barcode": "222", "resolved": False})) is not None
