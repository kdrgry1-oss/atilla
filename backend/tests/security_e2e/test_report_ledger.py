"""Rapor veri havuzu: raporla AYNI sayıları üretir; denetçi uyuşmayı/uyuşmazlığı doğru işaretler."""
from datetime import datetime, timedelta, timezone

import pytest

from routes import deps
from routes import report_ledger as RL
from tests.security_e2e.test_pii_tax_id import _shim_convert_to_date

pytestmark = pytest.mark.asyncio


def _iso(days_ago=0, hours=10):
    d = (datetime.now(timezone.utc) + timedelta(hours=3)).date() - timedelta(days=days_ago)
    return datetime(d.year, d.month, d.day, hours - 3, 0, tzinfo=timezone.utc).isoformat()


async def _seed():
    db = deps.db
    await db.orders.insert_many([
        # site: 2 ürün, ödenmiş, aktif
        {"id": "s1", "order_number": "W1", "platform": "site", "status": "confirmed", "payment_status": "paid",
         "total": 300.0, "created_at": _iso(1), "items": [
             {"barcode": "A", "name": "Etek", "quantity": 1, "price": 100},
             {"barcode": "B", "name": "Gömlek", "quantity": 1, "price": 200}]},
        # trendyol: iptal (bugün iptal edildi)
        {"id": "t1", "order_number": "T1", "platform": "trendyol", "status": "cancelled",
         "total": 150.0, "marketplace_order_date": _iso(2), "created_at": _iso(2),
         "cancelled_at": _iso(0), "items": [{"barcode": "A", "name": "Etek", "quantity": 1, "price": 150}]},
        # trendyol: iade (numaralı pusula, bugün)
        {"id": "t2", "order_number": "T2", "platform": "trendyol", "status": "returned",
         "total": 200.0, "marketplace_order_date": _iso(3), "created_at": _iso(3),
         "items": [{"barcode": "B", "name": "Gömlek", "quantity": 2, "price": 100}]},
        # ödenmemiş site iptali — satış değil
        {"id": "s2", "order_number": "W2", "platform": "site", "status": "cancelled",
         "payment_method": "credit_card", "payment_status": "failed", "total": 999.0,
         "created_at": _iso(1), "cancelled_at": _iso(1), "items": [{"barcode": "Z", "quantity": 1, "price": 999}]},
    ])
    await db.gider_pusulasi.insert_one({"order_number": "T2", "number": 5, "display_number": "087001",
                                        "source": "trendyol", "created_at": _iso(0),
                                        "totals": {"net": 100.0},
                                        "items": [{"barcode": "B", "name": "Gömlek", "quantity": 1,
                                                   "net_price": 100.0}]})


async def test_ledger_matches_reports_and_flags_drift(pay_env, monkeypatch):
    _shim_convert_to_date(monkeypatch)
    # mongomock $trim desteklemez; tohum verisinde mükerrer sipariş yok → tekilleştirme aşaması boş.
    import routes.report_dedup as RD
    import routes.reports as R
    monkeypatch.setattr(RD, "canonical_order_stages", lambda: [])
    monkeypatch.setattr(R, "canonical_order_stages", lambda: [])
    await _seed()
    res = await RL.build_ledger()
    assert res["rows"] > 0
    t, n = RL._window(7)
    L = await RL.ledger_sums(t, n)
    assert L["o_units"] == 5 and L["orders"] == 3          # ödenmemiş iptal hariç
    assert L["ce_units"] == 1 and round(L["ce_rev"], 2) == 150.0
    assert L["re_units"] == 1 and round(L["re_rev"], 2) == 100.0
    # ürün kırılımı: iade edilen ürün pusuladaki barkod (B)
    rows = await deps.db.report_ledger_daily.find({"kind": "product", "re_units": {"$gt": 0}}).to_list(10)
    assert [r["barcode"] for r in rows] == ["B"]

    doc = await RL.reconcile()
    cell = next(c for c in doc["cells"] if c["pencere"] == "7g" and c["kaynak"] == "tumu")
    bad = [x for x in cell["alanlar"] if not x["ok"]]
    assert cell["ok"], bad

    # havuz bozulursa (ör. eksik senkron) denetçi FARK işaretler
    await deps.db.report_ledger_daily.update_one({"kind": "product", "barcode": "A", "ch": "site"},
                                                 {"$inc": {"o_units": 7}})
    doc2 = await RL.reconcile()
    cell2 = next(c for c in doc2["cells"] if c["pencere"] == "7g" and c["kaynak"] == "tumu")
    assert not cell2["ok"] and any(x["alan"].startswith("Ciro") and not x["ok"] for x in cell2["alanlar"])


async def test_late_paid_havale_refreshes_its_order_day_without_duplicates(pay_env, monkeypatch):
    """Havale 3 gün sonra onaylanır: ciro SİPARİŞ GÜNÜNE yazılır ve 30 dk'lık tazeleme o günü
    (2 günlük pencere dışında) da günceller; tek gün tazelemesi başka günlerde çift kayıt üretmez."""
    _shim_convert_to_date(monkeypatch)
    import routes.report_dedup as RD
    import routes.reports as R
    monkeypatch.setattr(RD, "canonical_order_stages", lambda: [])
    monkeypatch.setattr(R, "canonical_order_stages", lambda: [])
    await _seed()
    await RL.build_ledger()
    t, n = RL._window(7)
    before = await RL.ledger_sums(t, n)
    await deps.db.orders.insert_one({
        "id": "h1", "order_number": "W9", "platform": "site", "status": "confirmed",
        "payment_method": "bank_transfer", "payment_status": "paid", "paid_at": _iso(0),
        "total": 400.0, "created_at": _iso(4),
        "items": [{"barcode": "C", "name": "Ceket", "quantity": 1, "price": 400}]})
    res = await RL.run_ledger_and_reconcile(full=False)
    assert res["late_paid_days"] == [RL._tr_day(_iso(4))]
    after = await RL.ledger_sums(t, n)
    assert after["o_units"] == before["o_units"] + 1 and after["orders"] == before["orders"] + 1
    assert after["ce_units"] == before["ce_units"] and after["re_units"] == before["re_units"]
    row = await deps.db.report_ledger_daily.find_one({"kind": "product", "barcode": "C"})
    assert row["day"] == RL._tr_day(_iso(4))
