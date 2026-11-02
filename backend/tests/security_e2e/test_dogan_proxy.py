"""Doğan sabit-IP aracı sunucu (DOGAN_PROXY_URL): yalnız Doğan oturumu aracıdan geçer, şifre
gösterilmez, geçersiz değer doğrudan bağlantıya düşer."""
import dogan_client
from dogan_client import DoganClient, dogan_proxy_display, dogan_proxy_url


def test_no_proxy_by_default(monkeypatch):
    monkeypatch.delenv("DOGAN_PROXY_URL", raising=False)
    assert dogan_proxy_url() == ""
    c = DoganClient(username="u", password="p", is_test=False)
    assert not c.transport.session.proxies


def test_proxy_applied_to_dogan_session_only(monkeypatch):
    monkeypatch.setenv("DOGAN_PROXY_URL", "http://facette:gizli123@203.0.113.7:8443")
    c = DoganClient(username="u", password="p", is_test=False)
    assert c.transport.session.proxies["https"] == "http://facette:gizli123@203.0.113.7:8443"
    assert dogan_proxy_display() == "203.0.113.7:8443"
    assert "gizli123" not in dogan_proxy_display()


def test_invalid_proxy_falls_back_to_direct(monkeypatch):
    for bad in ("203.0.113.7:8443", "ftp://x@h:1", "http://hostonly"):
        monkeypatch.setenv("DOGAN_PROXY_URL", bad)
        assert dogan_proxy_url() == ""
        assert dogan_proxy_display() == ""
    c = DoganClient(username="u", password="p", is_test=False)
    assert not c.transport.session.proxies
    assert dogan_client  # modül yüklendi


def test_env_state_and_quotes(monkeypatch):
    from dogan_client import dogan_proxy_env_state
    monkeypatch.delenv("DOGAN_PROXY_URL", raising=False)
    assert dogan_proxy_env_state() == "yok"
    monkeypatch.setenv("DOGAN_PROXY_URL", "bozuk")
    assert dogan_proxy_env_state() == "gecersiz"
    monkeypatch.setenv("DOGAN_PROXY_URL", ' "http://a:b@203.0.113.7:8443" ')
    assert dogan_proxy_env_state() == "ok"
    assert dogan_proxy_url() == "http://a:b@203.0.113.7:8443"
