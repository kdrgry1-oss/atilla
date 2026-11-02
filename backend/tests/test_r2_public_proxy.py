import asyncio
import pytest
from fastapi import HTTPException


def test_r2_proxy_only_public_images(monkeypatch):
    import routes.upload as up
    monkeypatch.setattr(up.r2, "is_enabled", lambda: True)
    monkeypatch.setattr(up.r2, "get_object", lambda k: (b"img", "image/webp") if k == "uploads/a.webp" else (None, None))
    r = asyncio.run(up.r2_public_proxy("uploads/a.webp"))
    assert r.status_code == 200 and r.body == b"img" and "immutable" in r.headers["cache-control"]
    for bad in ("archive/dekont.pdf", "uploads/../x.webp", "uploads/x.pdf", "uploads/yok.webp", "fatura/a.jpg"):
        with pytest.raises(HTTPException):
            asyncio.run(up.r2_public_proxy(bad))
