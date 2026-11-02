"""Tek seferlik bekleyen-fatura temizliği: yalnız KESİLMEMİŞ + bekleyen/inceleme/kilitli siparişler."""
import pytest

import scheduler
from routes import deps

pytestmark = pytest.mark.asyncio


async def test_cleanup_scope_and_idempotent(pay_env):
    db = deps.db
    await db.orders.insert_many([
        {"id": "a", "order_number": "W1", "invoice_pending": {"uuid": "u", "number": "FCT1"},
         "invoice_needs_review": True, "invoice_last_error": "x"},
        {"id": "b", "order_number": "W2", "invoice_in_progress": True},
        {"id": "c", "order_number": "W3", "invoice_issued": True, "invoice_number": "FCT3",
         "invoice_needs_review": True},
        {"id": "d", "order_number": "W4"},
    ])
    await scheduler._bekleyen_fatura_temizle_once()
    a = await db.orders.find_one({"id": "a"})
    assert "invoice_pending" not in a and "invoice_needs_review" not in a and "invoice_last_error" not in a
    assert a["invoice_pending_history"][0]["number"] == "FCT1"
    b = await db.orders.find_one({"id": "b"})
    assert "invoice_in_progress" not in b
    c = await db.orders.find_one({"id": "c"})
    assert c["invoice_number"] == "FCT3" and c["invoice_issued"] is True   # kesilmiş faturaya dokunulmaz
    d = await db.orders.find_one({"id": "d"})
    assert "invoice_issued" not in d
    st = await db.settings.find_one({"id": "bekleyen_fatura_temizle_once_v1"})
    assert st["done"] and {x["order"] for x in st["temizlenen"]} == {"W1", "W2"}
    await db.orders.update_one({"id": "d"}, {"$set": {"invoice_needs_review": True}})
    await scheduler._bekleyen_fatura_temizle_once()   # ikinci kez çalışmaz
    assert (await db.orders.find_one({"id": "d"}))["invoice_needs_review"] is True
