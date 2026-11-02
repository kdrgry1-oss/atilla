"""Doğan bağlantı testi: sunucuya ulaşılamıyorsa giriş denenmeden ağ teşhisi (DNS/443/çıkış IP) döner."""
import socket
from conftest import kv_run
from routes import integrations_dogan as D


def test_unreachable_host_reports_network(kv_db, monkeypatch):
    kv_run(kv_db.settings.insert_one({"id": "dogan_edonusum", "username": "u", "password": "p", "is_test": False}))

    def _boom(*a, **k):
        raise OSError("timed out")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("10.0.0.1", 443))])
    monkeypatch.setattr(socket, "create_connection", _boom)

    import dogan_client
    called = {"login": False}
    monkeypatch.setattr(dogan_client.DoganClient, "test_connection",
                        lambda self: called.update(login=True) or {"success": True})

    class _Resp:
        text = "203.0.113.7"

    class _HC:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url): return _Resp()
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _HC)

    r = kv_run(D.test_dogan_connection(current_user={"id": "a"}))
    assert r["success"] is False and "ULAŞILAMIYOR" in r["message"]
    assert r["network"]["egress_ip"] == "203.0.113.7"
    assert r["network"]["probes"][0]["host"] == "connector.doganedonusum.com"
    assert called["login"] is False
