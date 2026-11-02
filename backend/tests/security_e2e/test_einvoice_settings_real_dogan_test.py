"""Ayarlar → E-Arşiv 'Bağlantıyı Test Et' Doğan için sahte-yeşil değil, gerçek testi çağırmalı."""
import asyncio

from fastapi import HTTPException

import routes.integrations_dogan as idg
from routes import provider_settings as ps


def _cfg():
    schema = ps.PROVIDERS_BY_KIND["einvoice"]["dogan-edonusum"]
    return {f["key"]: "x" for f in schema["fields"]}


def test_dogan_settings_test_delegates_to_real_test(monkeypatch):
    async def fake(current_user=None):
        return {"success": False, "message": "ULAŞILAMIYOR", "network": {"egress_ip": "1.2.3.4"}}
    monkeypatch.setattr(idg, "test_dogan_connection", fake)
    res = asyncio.run(ps.test_connection("einvoice", {"provider": "dogan-edonusum", "config": _cfg()},
                                         current_user={"email": "a@b"}))
    assert res["success"] is False and "ULAŞILAMIYOR" in res["message"]
    assert not res.get("mock")


def test_dogan_settings_test_missing_settings(monkeypatch):
    async def fake(current_user=None):
        raise HTTPException(status_code=400, detail="Doğan e-Dönüşüm ayarları eksik")
    monkeypatch.setattr(idg, "test_dogan_connection", fake)
    res = asyncio.run(ps.test_connection("einvoice", {"provider": "dogan-edonusum", "config": _cfg()},
                                         current_user={"email": "a@b"}))
    assert res["success"] is False and "eksik" in res["message"]
