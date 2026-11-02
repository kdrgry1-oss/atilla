"""Yanlış kesilen gider pusulası iptali: yalnız muhasebe/admin; arşive taşınır, bağlı işaretler kalkar."""
import pytest

from pay_harness import pay_staff
from routes import deps

pytestmark = pytest.mark.asyncio


async def _seed():
    db = deps.db
    await db.gider_pusulasi.insert_one({"claim_id": "c1", "order_number": "11700000001",
                                        "number": 7, "display_number": "087400", "totals": {"net": 100}})
    await db.trendyol_claims.insert_one({"claim_id": "c1", "has_gider_pusulasi": True,
                                         "gider_pusulasi_no": "087400"})
    await db.orders.insert_one({"id": "o1", "order_number": "11700000001", "gider_pusulasi_no": "087400"})


async def test_cancel_requires_permission(pay_http, pay_env):
    await _seed()
    _, hdr = await pay_staff(["returns.view", "returns.expense_note"])   # operasyon: oluşturabilir, iptal edemez
    r = await pay_http.post("/api/orders/returns/gider-pusulasi/cancel", headers=hdr,
                            json={"claim_id": "c1", "reason": "yanlış kişi"})
    assert r.status_code == 403
    assert await deps.db.gider_pusulasi.find_one({"claim_id": "c1"})


async def test_cancel_by_finance_archives_and_clears(pay_http, pay_env):
    await _seed()
    _, hdr = await pay_staff(["returns.view", "returns.expense_note_cancel"])
    r = await pay_http.post("/api/orders/returns/gider-pusulasi/cancel", headers=hdr,
                            json={"claim_id": "c1", "reason": ""})
    assert r.status_code == 400   # sebep zorunlu
    r = await pay_http.post("/api/orders/returns/gider-pusulasi/cancel", headers=hdr,
                            json={"claim_id": "c1", "reason": "yanlış kişiye kesildi"})
    assert r.status_code == 200, r.text
    db = deps.db
    assert not await db.gider_pusulasi.find_one({"claim_id": "c1"})
    arc = await db.gider_pusulasi_iptal.find_one({"claim_id": "c1"})
    assert arc["display_number"] == "087400" and arc["cancel_reason"] == "yanlış kişiye kesildi"
    c = await db.trendyol_claims.find_one({"claim_id": "c1"})
    assert "has_gider_pusulasi" not in c and "gider_pusulasi_no" not in c
    o = await db.orders.find_one({"id": "o1"})
    assert "gider_pusulasi_no" not in o
    r = await pay_http.post("/api/orders/returns/gider-pusulasi/cancel", headers=hdr,
                            json={"claim_id": "c1", "reason": "tekrar"})
    assert r.status_code == 404


async def test_finance_role_grant_once(pay_env):
    from routes.admin_rbac import grant_gp_cancel_to_finance_once
    db = deps.db
    await db.roles.insert_one({"id": "finance", "permissions": ["returns.view"]})
    await db.roles.insert_one({"id": "operations", "permissions": ["returns.view"]})
    assert (await grant_gp_cancel_to_finance_once())["modified"] == 1
    assert "returns.expense_note_cancel" in (await db.roles.find_one({"id": "finance"}))["permissions"]
    assert "returns.expense_note_cancel" not in (await db.roles.find_one({"id": "operations"}))["permissions"]
    await db.roles.update_one({"id": "finance"}, {"$pull": {"permissions": "returns.expense_note_cancel"}})
    assert (await grant_gp_cancel_to_finance_once())["skipped"]   # elle kaldırılırsa geri eklenmez
