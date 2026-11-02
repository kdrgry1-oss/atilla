"""Ödeme dönüş/callback adresinde ayrıştırıcı farkı saldırıları (\\, @, kontrol karakteri) reddedilir."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from routes.payment import _safe_return_base, _safe_callback_url

REQ = SimpleNamespace(base_url="https://api.facette.com.tr/")
BAD = [
    "https://evil.example\\@www.facette.com.tr/odeme",
    "https://www.facette.com.tr@evil.example/odeme",
    "https://evil.example\\.facette.com.tr/odeme",
    "https://www.facette.com.tr/odeme\r\nLocation: https://evil.example",
    "https://www.facette.com.tr /odeme",
]


@pytest.mark.parametrize("url", BAD)
def test_return_url_parser_confusion_falls_back_to_default(url):
    assert _safe_return_base(url, REQ) == "https://api.facette.com.tr/odeme"


@pytest.mark.parametrize("url", BAD)
def test_callback_url_parser_confusion_rejected(url):
    with pytest.raises(HTTPException):
        _safe_callback_url(url, REQ)


def test_legit_urls_still_accepted(monkeypatch):
    monkeypatch.setenv("SITE_URL", "https://www.facette.com.tr")
    assert _safe_return_base("https://www.facette.com.tr/odeme", REQ) == "https://www.facette.com.tr/odeme"
    assert _safe_callback_url("https://api.facette.com.tr/api/payment/3ds/callback", REQ).endswith("/3ds/callback")


def test_foreign_brand_domain_not_allowed_without_config(monkeypatch):
    """Beyaz etiket: başka firmanın alan adı koda gömülü değil — yalnız kurulumun kendi adresleri."""
    for k in ("SITE_URL", "FRONTEND_URL", "PUBLIC_API_URL", "BACKEND_URL", "CORS_ORIGINS"):
        monkeypatch.delenv(k, raising=False)
    req = SimpleNamespace(base_url="https://api.yenimarka.com/")
    with pytest.raises(HTTPException):
        _safe_callback_url("https://api.facette.com.tr/api/payment/3ds/callback", req)
    monkeypatch.setenv("SITE_URL", "https://yenimarka.com")
    assert _safe_return_base("https://www.yenimarka.com/odeme", req) == "https://www.yenimarka.com/odeme"
