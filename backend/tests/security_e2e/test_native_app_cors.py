"""Native (Capacitor) uygulama kaynakları CORS'ta izinli olmalı.

iOS WebView kaynağı capacitor://localhost (veya ios.scheme "Facette" → Facette://localhost),
Android https://localhost. Bunlar whitelist'te yoksa uygulamadan HER istek — giriş dahil —
400 "Disallowed CORS origin" alır ve kullanıcı "giriş başarısız" görür.
Rastgele web kaynağı yine reddedilir.
"""
import pytest

pytestmark = pytest.mark.usefixtures("id_env")


@pytest.mark.parametrize("origin", ["capacitor://localhost", "Facette://localhost", "https://localhost"])
def test_native_origin_preflight_allowed(id_client, origin):
    r = id_client.options("/api/auth/login", headers={
        "Origin": origin, "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type,x-app-version,x-app-platform,x-app-target"})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == origin


def test_foreign_origin_still_rejected(id_client):
    r = id_client.options("/api/auth/login", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert r.status_code == 400
