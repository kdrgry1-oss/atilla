"""Ana sayfa page-meta: ilk yayındaki hero görseli edge preload için döner (mobil LCP)."""
from conftest import kv_ip, kv_run


def test_home_page_meta_returns_hero_preload(kv_client, kv_db):
    kv_run(kv_db.page_blocks.insert_many([
        {"id": "b0", "page": "home", "type": "hero_slider", "is_active": False, "sort_order": 0,
         "images": ["https://cdn.example.com/pasif.webp"]},
        {"id": "b1", "page": "home", "type": "hero_slider", "is_active": True, "sort_order": 1,
         "images": ["https://cdn.example.com/cdn-cgi/image/w=1920/pagedesign/hero-1.webp", "https://cdn.example.com/x.webp"]},
    ]))
    r = kv_client.get("/api/seo/page-meta?path=/", headers=kv_ip())
    assert r.status_code == 200
    assert r.json().get("preload_images") == [{"href": "https://cdn.example.com/pagedesign/hero-1.webp"}]


def test_separate_mobile_and_desktop_heroes_and_unsafe_skipped(kv_client, kv_db):
    kv_run(kv_db.page_blocks.insert_many([
        {"id": "m", "page": "home", "type": "hero_slider", "is_active": True, "sort_order": 1,
         "show_desktop": False, "images": ["https://cdn.example.com/mobil.webp"]},
        {"id": "d", "page": "home", "type": "hero_slider", "is_active": True, "sort_order": 2,
         "show_mobile": False, "images": ["https://static.ticimax.cloud/a/b.jpg"]},
    ]))
    pre = kv_client.get("/api/seo/page-meta?path=/", headers=kv_ip()).json().get("preload_images")
    assert pre == [{"href": "https://cdn.example.com/mobil.webp", "media": "(max-width: 767px)"}]
