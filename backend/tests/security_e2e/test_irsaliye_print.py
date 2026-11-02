"""Sevk irsaliyesi: ürün/beden/barkod/fiyat; eksik barkod ürün kartından; yetki ve XSS kaçışı."""
import pytest

from pay_harness import pay_staff
from routes import deps

pytestmark = pytest.mark.asyncio


async def test_irsaliye_print(pay_http, pay_env):
    db = deps.db
    await db.products.insert_one({"id": "p1", "stock_code": "STK-1", "variants": [
        {"id": "v1", "size": "M", "barcode": "8680001112223", "stock_code": "STK-1-M", "color": "Siyah"}]})
    await db.orders.insert_one({
        "id": "o1", "order_number": "W99001", "status": "confirmed", "total": 1749.0, "shipping_cost": 49.0,
        "created_at": "2026-10-19T10:00:00+00:00", "cargo_tracking_number": "7330001",
        "shipping_address": {"full_name": "<script>x</script>Ayşe", "address": "Cad. 1", "city": "İstanbul",
                             "district": "Kadıköy", "phone": "5550000000"},
        "items": [{"product_id": "p1", "variant_id": "v1", "name": "Pamuklu Etek, Uzun", "quantity": 2, "price": 850.0},
                  {"name": "Gömlek", "quantity": 1, "price": 0, "size": "S", "barcode": "8680009998887"}]})
    _, hdr = await pay_staff(["orders.view"])   # siparişleri gören tüm personel
    r = await pay_http.get("/api/orders/o1/irsaliye/print", headers=hdr)
    assert r.status_code == 200, r.text
    h = r.text
    assert "SEVK İRSALİYESİ" in h and "IRS-W99001" in h
    assert "8680001112223" in h and "STK-1-M" in h and "<td class='c sz'>M</td>" in h and "Siyah" in h   # karttan tamamlandı
    assert "8680009998887" in h and "<td class='c sz'>S</td>" in h
    assert "Pamuklu Etek, Uzun" in h                       # ürün adındaki virgül bozulmaz
    assert "1.700,00" in h and "1.749,00 ₺" in h and "850,00" in h and "size: A5 portrait" in h
    assert "<script>x</script>" not in h and "&lt;script&gt;" in h
    _, nohdr = await pay_staff(["dashboard.view"])
    assert (await pay_http.get("/api/orders/o1/irsaliye/print", headers=nohdr)).status_code == 403
    assert (await pay_http.get("/api/orders/yok/irsaliye/print", headers=hdr)).status_code == 404
