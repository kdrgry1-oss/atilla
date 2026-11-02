"""Panelde değişen entegrasyon şifresi işlemlerin okuduğu ayara da yansır (MNG/Doğan tek kaynak)."""
import pytest

from pay_harness import pay_staff
from routes import deps

pytestmark = pytest.mark.asyncio


async def test_mng_password_change_reaches_barcode_settings(pay_http, pay_env):
    from routes.orders import _get_mng_settings
    db = deps.db
    await db.settings.insert_one({"id": "mng_kargo", "username": "eski", "password": "ESKI-SIFRE"})
    # aktif kargo firması MNG DEĞİL (ör. DHL seçili) — yine de MNG kartındaki yeni şifre kullanılmalı
    await db.providers_config.insert_one({"kind": "cargo", "active_provider": "dhl", "providers": {}})
    _, hdr = await pay_staff(super_admin=True)
    r = await pay_http.post("/api/provider-settings/cargo/config", headers=hdr, json={
        "active_provider": "dhl",
        "providers": {"mng": {"customer_number": "C1", "username": "yeni", "password": "YENI-SIFRE", "env": "prod"}}})
    assert r.status_code == 200, r.text
    s = await _get_mng_settings()
    assert s["username"] == "yeni" and s["password"] == "YENI-SIFRE"
    legacy = await db.settings.find_one({"id": "mng_kargo"})
    assert legacy["username"] == "yeni" and legacy["password"] != "ESKI-SIFRE"
    assert "YENI-SIFRE" not in str(legacy.get("password"))   # şifreli saklanır


async def test_dogan_password_change_mirrors_to_invoice_settings(pay_http, pay_env):
    db = deps.db
    await db.settings.insert_one({"id": "dogan_edonusum", "enabled": True, "username": "u-eski",
                                  "password": "p-eski", "vkn": "1"})
    _, hdr = await pay_staff(super_admin=True)
    body = {"active_provider": "dogan-edonusum", "providers": {"dogan-edonusum": {
        "earchive_username": "u-yeni", "earchive_password": "p-yeni"}}}
    r = await pay_http.post("/api/provider-settings/einvoice/config", headers=hdr, json=body)
    assert r.status_code == 200, r.text
    d = await db.settings.find_one({"id": "dogan_edonusum"})
    assert d["username"] == "u-yeni" and d["password"] != "p-eski" and d["enabled"] is True and d["vkn"] == "1"
    from security.crypto import decrypt
    assert decrypt(d["password"]) == "p-yeni"
    # maskeli şifreyle tekrar kaydetmek mevcut şifreyi DEĞİŞTİRMEZ
    body2 = {"active_provider": "dogan-edonusum", "providers": {"dogan-edonusum": {
        "earchive_username": "u-yeni", "earchive_password": "********"}}}
    r = await pay_http.post("/api/provider-settings/einvoice/config", headers=hdr, json=body2)
    d2 = await db.settings.find_one({"id": "dogan_edonusum"})
    assert decrypt(d2["password"]) == "p-yeni"
