"""Menü Yönetimi: kolon ve link için 'İtalik' seçeneği kaydedilir ve vitrine döner."""
from conftest import kv_ip


def test_italic_flag_round_trips(kv_client, kv_users):
    sa = kv_users.staff(super_admin=True)
    tabs = [{"id": "koleksiyonlar", "label": "KOLEKSİYONLAR", "type": "mega", "link": "/koleksiyonlar",
             "columns": [{"title": "Winter Shape 05.11", "link": "/winter-shape", "italic": True, "items": []},
                         {"title": "Downtown Code", "link": "/downtown", "italic": False,
                          "items": [{"name": "Ceket", "link": "/ceket", "italic": True}]}]}]
    r = kv_client.put("/api/page-blocks/header-menu", json={"tabs": tabs}, headers={**kv_users.hdr(sa), **kv_ip()})
    assert r.status_code == 200, r.text
    got = kv_client.get("/api/page-blocks/header-menu", headers=kv_ip()).json()
    cols = next(t for t in got["tabs"] if t.get("id") == "koleksiyonlar")["columns"]
    assert cols[0]["italic"] is True and cols[1]["italic"] is False
    assert cols[1]["items"][0]["italic"] is True
