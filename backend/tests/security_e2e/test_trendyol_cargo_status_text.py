"""Trendyol siparişi: paket (kargo) durumu Türkçe metin olarak siparişe yazılır (liste takip no altı)."""
from routes.integrations_trendyol import map_trendyol_order


def _o(st):
    return {"orderNumber": "1", "status": st, "shipmentPackageStatus": st, "lines": [],
            "cargoTrackingNumber": "7330037968249551"}


def test_package_status_text():
    assert map_trendyol_order(_o("Delivered"))["marketplace_cargo_status_text"] == "Teslim Edildi"
    assert map_trendyol_order(_o("Shipped"))["marketplace_cargo_status_text"] == "Kargoya Verildi"
    assert map_trendyol_order(_o("UnDelivered"))["marketplace_package_status"] == "UnDelivered"
    assert map_trendyol_order(_o("YeniDurum"))["marketplace_cargo_status_text"] == "YeniDurum"


def test_status_advances_forward_only():
    from routes.integrations_trendyol import ty_cargo_status_advance as adv, ty_cargo_status_set as st
    assert adv("confirmed", "Shipped") == "shipped"
    assert adv("confirmed", "Delivered") == "delivered"
    assert adv("shipped", "Delivered") == "delivered"
    assert adv("delivered", "Shipped") is None          # geri çekilmez
    assert adv("cancelled", "Delivered") is None        # iptal/iade ezilmez
    assert adv("returned", "Delivered") is None
    assert adv("confirmed", "Picking") is None          # kargo öncesi: değişmez
    assert adv("confirmed", "UnDelivered") is None
    s = st({"status": "confirmed"}, {"marketplace_package_status": "Delivered"})
    assert s["status"] == "delivered" and s["delivered_at"] and s["shipped_at"]
    assert st({"status": "delivered"}, {"marketplace_package_status": "Delivered"}) == {}
