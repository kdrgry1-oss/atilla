from services import r2_storage as r2


def test_explain_error_messages_have_no_secret_and_are_specific():
    class E(Exception):
        def __init__(self, code):
            super().__init__(f"An error occurred ({code})")
            self.response = {"Error": {"Code": code}}
    assert "anahtarı geçersiz" in r2.explain_error(E("InvalidAccessKeyId"))
    assert "YAZMA izni" in r2.explain_error(E("AccessDenied"))
    assert "bulunamadı" in r2.explain_error(E("NoSuchBucket"))
    assert "YAZMA izni" in r2.explain_error(Exception("An error occurred (403) when calling the HeadBucket operation: Forbidden"))


def test_client_uses_when_required_checksums(monkeypatch):
    for k, v in {"R2_ENDPOINT": "https://x.r2.cloudflarestorage.com", "R2_ACCESS_KEY_ID": "a",
                 "R2_SECRET_ACCESS_KEY": "b"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(r2, "_client", None)
    c = r2._get_client()
    assert c.meta.config.request_checksum_calculation == "when_required"
    monkeypatch.setattr(r2, "_client", None)
