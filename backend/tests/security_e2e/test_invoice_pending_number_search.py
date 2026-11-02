"""Panel 'Fatura No' filtresi BEKLEYEN (BL-035, Doğan'da doğrulanamamış) numarayı da bulur."""
from datetime import datetime, timezone

import pytest

from pay_harness import pay_staff
from routes import deps
from tests.security_e2e.test_pii_tax_id import _shim_convert_to_date

pytestmark = pytest.mark.asyncio


async def test_invoice_filter_matches_pending_number(pay_http, pay_env, monkeypatch):
    _shim_convert_to_date(monkeypatch)
    now = datetime.now(timezone.utc).isoformat()
    base = {"status": "confirmed", "payment_status": "paid", "created_at": now, "items": [], "total": 100}
    await deps.db.orders.insert_many([
        {**base, "id": "o-pend", "order_number": "W90001",
         "invoice_pending": {"uuid": "u1", "number": "FCT2026000099991", "type": "e-arsiv"},
         "invoice_needs_review": True},
        {**base, "id": "o-issued", "order_number": "W90002", "invoice_issued": True,
         "invoice_number": "FCT2026000099992"},
        {**base, "id": "o-other", "order_number": "W90003"},
    ])
    _, ahdr = await pay_staff(super_admin=True)
    for q, want in (("99991", {"o-pend"}), ("FCT2026000099992", {"o-issued"}), ("FCT20260000999", {"o-pend", "o-issued"})):
        r = await pay_http.get("/api/orders", params={"invoice_number": q, "show_hidden": 1}, headers=ahdr)
        assert r.status_code == 200, r.text
        assert {o["id"] for o in r.json()["orders"]} == want, q
